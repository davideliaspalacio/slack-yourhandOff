"""What to do with each message the watcher stored.

The person, not the message, is the unit of work: an unknown author, or one
whose last research came out incomplete, gets a research job; a discarded
author's message is archived; everyone else's message waits for scoring
(Plan 3). The queue's one-open-job-per-person rule absorbs repeats.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import db
from . import queue

logger = logging.getLogger(__name__)

RESEARCH_AGAIN = ("nuevo", "incompleto")
BATCH = 500


@dataclass
class ResolveResult:
    enqueued: int = 0
    archived: int = 0
    to_scoring: int = 0
    failed: int = 0


def _not_before(user_id: str):
    """Cuándo se puede reclamar el research disparado por un mensaje: el ts del
    último mensaje de la persona más la espera configurada. Así el research lee
    el hilo entero y no solo el primer mensaje. Si ese instante ya pasó (el
    lector llegó tarde), la tarea se puede reclamar enseguida."""
    row = db.fetch_one(
        "select to_timestamp(max(ts::numeric)) + %s * interval '1 second' as listo "
        "from slack_messages where user_id = %s and status <> 'ignorado'",
        (queue.message_wait_seconds(), user_id),
    )
    return row["listo"] if row else None


def resolve_pending(limit: int = BATCH) -> ResolveResult:
    result = ResolveResult()
    # Two concurrent runs are safe because queue.enqueue de-duplicates open jobs
    # and the status updates are idempotent, though the counters could double-count.
    rows = db.fetch_all(
        "select id, user_id from slack_messages where status = 'nuevo' "
        "order by ts::numeric limit %s",
        (limit,),
    )
    for row in rows:
        try:
            # Messages with NULL or empty user_id are marked ignorado, not researched
            if not row["user_id"]:
                db.execute(
                    "update slack_messages set status = %s where id = %s",
                    ("ignorado", row["id"]),
                )
                continue

            person = db.fetch_one(
                "select state from prospects where slack_user_id = %s",
                (row["user_id"],),
            )
            if person is not None and person["state"] == "descartado":
                status = "archivado"
                result.archived += 1
            else:
                # Si ya hay una tarea abierta, enqueue no crea otra: los mensajes
                # que lleguen mientras espera los lee el research al ejecutarse.
                if (person is None or person["state"] in RESEARCH_AGAIN) and queue.enqueue(
                    row["user_id"], "mensaje", not_before=_not_before(row["user_id"])
                ):
                    result.enqueued += 1
                status = "pendiente_scoring"
                result.to_scoring += 1
            db.execute(
                "update slack_messages set status = %s where id = %s",
                (status, row["id"]),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Error processing message id=%s: %s", row["id"], type(e).__name__)
            result.failed += 1
    return result
