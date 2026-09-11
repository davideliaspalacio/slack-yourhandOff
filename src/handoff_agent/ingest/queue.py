"""Research queue in Postgres.

One open job per person (a partial unique index), claimed with
FOR UPDATE SKIP LOCKED so several workers never take the same job. System
stops (kill switch, monthly cap) hand the job back untouched: they are not the
person's fault and must not burn a retry.
"""

from __future__ import annotations

import json

from .. import db

MAX_ATTEMPTS = 3
REASONS = ("mensaje", "miembro_nuevo", "manual")
DEFAULT_HOURLY_LIMIT = 20


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


def complete(job_id, outcome: dict) -> None:
    db.execute(
        "update research_jobs set status = 'hecho', outcome = %s, finished_at = now(), "
        "last_error = null where id = %s",
        (json.dumps(outcome), job_id),
    )


def fail(job_id, error: str) -> str:
    """Retry later with a growing wait, or give up after MAX_ATTEMPTS."""
    row = db.fetch_one(
        """
        update research_jobs set
            status      = case when attempts >= %s then 'fallido' else 'pendiente' end,
            not_before  = case when attempts >= %s then null
                               else now() + attempts * interval '15 minutes' end,
            finished_at = case when attempts >= %s then now() else null end,
            last_error  = %s
        where id = %s
        returning status
        """,
        (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, error, job_id),
    )
    return row["status"]


def give_up(job_id, error: str) -> None:
    db.execute(
        "update research_jobs set status = 'fallido', finished_at = now(), last_error = %s "
        "where id = %s",
        (error, job_id),
    )


def release(job_id) -> None:
    """Hand a job back after a system stop, without counting the attempt."""
    db.execute(
        "update research_jobs set status = 'pendiente', started_at = null, "
        "attempts = greatest(attempts - 1, 0) where id = %s",
        (job_id,),
    )


def finished_last_hour() -> int:
    row = db.fetch_one(
        "select count(*) as n from research_jobs "
        "where status in ('hecho', 'fallido') and finished_at > now() - interval '1 hour'"
    )
    return row["n"]


def hourly_limit() -> int:
    row = db.fetch_one("select value from config where key = 'research_por_hora'")
    return int(row["value"]) if row else DEFAULT_HOURLY_LIMIT


def status_counts() -> dict[str, int]:
    rows = db.fetch_all("select status, count(*) as n from research_jobs group by status")
    return {row["status"]: row["n"] for row in rows}


def recent_failures(limit: int = 5) -> list[dict]:
    return db.fetch_all(
        "select slack_user_id, last_error, finished_at from research_jobs "
        "where status = 'fallido' order by finished_at desc limit %s",
        (limit,),
    )
