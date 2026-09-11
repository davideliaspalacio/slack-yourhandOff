"""Research queue in Postgres.

One open job per person (a partial unique index), claimed with
FOR UPDATE SKIP LOCKED so several workers never take the same job. System
stops (kill switch, monthly cap) hand the job back untouched: they are not the
person's fault and must not burn a retry.

Every state transition below is guarded on the job still being en_curso, so a
late or duplicate call (a retried webhook, two workers finishing the same job)
cannot reopen a job that already finished or double-release one that already
went back to pendiente.

They are also guarded on started_at matching the row this worker actually
claimed. reclaim_stale() can hand a still-running job back to pendiente and
let a second worker claim it while the first worker is unaware and still
plodding along; without the started_at check, the first worker's late
complete/fail/give_up/release would land on the second worker's claim
instead of being the no-op it must be. Callers pass the full job dict
returned by claim_next(), not a bare id.
"""

from __future__ import annotations

import json
import logging

from .. import db

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
REASONS = ("mensaje", "miembro_nuevo", "manual")
DEFAULT_HOURLY_LIMIT = 20
STALE_AFTER_MINUTES = 30


def enqueue(slack_user_id: str, reason: str) -> bool:
    """Queue research for a person. False if they already have an open job."""
    if reason not in REASONS:
        raise ValueError(f"motivo desconocido: {reason!r}")
    inserted = db.execute(
        """
        insert into research_jobs (slack_user_id, reason) values (%s, %s)
        on conflict (slack_user_id) where status in ('pendiente', 'en_curso') do nothing
        """,
        (slack_user_id, reason),
    )
    return inserted == 1


def claim_next() -> dict | None:
    return db.fetch_one(
        """
        update research_jobs
        set status = 'en_curso', started_at = now(), attempts = attempts + 1
        where id = (
            select id from research_jobs
            where status = 'pendiente' and (not_before is null or not_before <= now())
            order by created_at
            for update skip locked
            limit 1
        )
        returning *
        """
    )


def complete(job: dict, outcome: dict) -> bool:
    """Mark a job done. False (no-op) if it was not en_curso, or is no longer
    the same claim (started_at moved on -- reclaim_stale gave it to someone
    else while this worker was still running it)."""
    updated = db.execute(
        "update research_jobs set status = 'hecho', outcome = %s, finished_at = now(), "
        "last_error = null where id = %s and status = 'en_curso' and started_at = %s",
        (json.dumps(outcome), job["id"], job["started_at"]),
    )
    if not updated:
        logger.warning(
            "complete: la tarea %s ya no era nuestra (no en_curso o reclamada por otro)", job["id"]
        )
    return bool(updated)


def fail(job: dict, error: str) -> str | None:
    """Retry later with a growing wait, or give up after MAX_ATTEMPTS.

    Returns the new status, or None (no-op, nothing changed) if the job does
    not exist, was not en_curso, or is no longer the same claim (started_at
    moved on) -- so a late or duplicate call cannot reopen a job that already
    finished, nor land on a claim that is not its own.
    """
    row = db.fetch_one(
        """
        update research_jobs set
            status      = case when attempts >= %s then 'fallido' else 'pendiente' end,
            not_before  = case when attempts >= %s then null
                               else now() + attempts * interval '15 minutes' end,
            finished_at = case when attempts >= %s then now() else null end,
            last_error  = %s
        where id = %s and status = 'en_curso' and started_at = %s
        returning status
        """,
        (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, error, job["id"], job["started_at"]),
    )
    if row is None:
        logger.warning(
            "fail: la tarea %s ya no era nuestra (no en_curso o reclamada por otro)", job["id"]
        )
        return None
    return row["status"]


def give_up(job: dict, error: str) -> bool:
    """Fail a job at once. False (no-op) if it was not en_curso, or is no
    longer the same claim (started_at moved on)."""
    updated = db.execute(
        "update research_jobs set status = 'fallido', finished_at = now(), last_error = %s "
        "where id = %s and status = 'en_curso' and started_at = %s",
        (error, job["id"], job["started_at"]),
    )
    if not updated:
        logger.warning(
            "give_up: la tarea %s ya no era nuestra (no en_curso o reclamada por otro)", job["id"]
        )
    return bool(updated)


def release(job: dict) -> bool:
    """Hand a job back after a system stop, without counting the attempt.

    False (no-op) if it was not en_curso, or is no longer the same claim
    (started_at moved on), so a second release cannot wipe a real attempt
    nor land on a claim that is not its own.
    """
    updated = db.execute(
        "update research_jobs set status = 'pendiente', started_at = null, "
        "attempts = greatest(attempts - 1, 0) "
        "where id = %s and status = 'en_curso' and started_at = %s",
        (job["id"], job["started_at"]),
    )
    if not updated:
        logger.warning(
            "release: la tarea %s ya no era nuestra (no en_curso o reclamada por otro)", job["id"]
        )
    return bool(updated)


def reclaim_stale(older_than_minutes: int = STALE_AFTER_MINUTES) -> int:
    """Give back jobs abandoned by a worker that died mid-research.

    Every Railway redeploy kills whatever worker is mid-job. Without this, its
    job stays en_curso forever and, because of the one-open-job index, that
    person can never be queued again. The attempt is kept on purpose -- the
    process may have died *because* of that job -- except once a job already
    used up all its attempts, in which case it is given up for good instead of
    coming back to loop forever. Returns how many jobs were touched.
    """
    return db.execute(
        """
        update research_jobs set
            status      = case when attempts >= %s then 'fallido' else 'pendiente' end,
            started_at  = case when attempts >= %s then started_at else null end,
            not_before  = case when attempts >= %s then not_before else null end,
            finished_at = case when attempts >= %s then now() else finished_at end,
            last_error  = case when attempts >= %s then 'abandonada: el worker no terminó'
                               else last_error end
        where status = 'en_curso' and started_at < now() - %s * interval '1 minute'
        """,
        (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, older_than_minutes),
    )


def finished_last_hour() -> int:
    row = db.fetch_one(
        "select count(*) as n from research_jobs "
        "where status in ('hecho', 'fallido') and finished_at > now() - interval '1 hour'"
    )
    return row["n"]


def hourly_limit() -> int:
    """Read the hourly throttle from config, falling back to the default.

    config.value is jsonb and psycopg decodes it straight to a Python object:
    true would come back as int(True) == 1, 20.7 would truncate to 20, and a
    string or null would raise. Only a real, non-boolean, non-negative int is
    trusted; anything else logs a warning and uses DEFAULT_HOURLY_LIMIT.
    """
    row = db.fetch_one("select value from config where key = 'research_por_hora'")
    if row is None:
        return DEFAULT_HOURLY_LIMIT
    value = row["value"]
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    logger.warning("hourly_limit: valor de config inválido: %r", value)
    return DEFAULT_HOURLY_LIMIT


def status_counts() -> dict[str, int]:
    rows = db.fetch_all("select status, count(*) as n from research_jobs group by status")
    return {row["status"]: row["n"] for row in rows}


def recent_failures(limit: int = 5) -> list[dict]:
    return db.fetch_all(
        "select slack_user_id, last_error, finished_at from research_jobs "
        "where status = 'fallido' order by finished_at desc limit %s",
        (limit,),
    )
