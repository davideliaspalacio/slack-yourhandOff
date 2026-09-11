"""What to do with each message the watcher stored.

The person, not the message, is the unit of work: an unknown author, or one
whose last research came out incomplete, gets a research job; a discarded
author's message is archived; everyone else's message waits for scoring
(Plan 3). The queue's one-open-job-per-person rule absorbs repeats.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import db
from . import queue

RESEARCH_AGAIN = ("nuevo", "incompleto")
BATCH = 500


@dataclass
class ResolveResult:
    enqueued: int = 0
    archived: int = 0
    to_scoring: int = 0


def resolve_pending(limit: int = BATCH) -> ResolveResult:
    result = ResolveResult()
    rows = db.fetch_all(
        "select id, user_id from slack_messages where status = 'nuevo' "
        "order by ts::numeric limit %s",
        (limit,),
    )
    for row in rows:
        person = db.fetch_one(
            "select state from prospects where slack_user_id = %s", (row["user_id"],)
        )
        if person is not None and person["state"] == "descartado":
            status = "archivado"
            result.archived += 1
        else:
            if (person is None or person["state"] in RESEARCH_AGAIN) and queue.enqueue(
                row["user_id"], "mensaje"
            ):
                result.enqueued += 1
            status = "pendiente_scoring"
            result.to_scoring += 1
        db.execute("update slack_messages set status = %s where id = %s", (status, row["id"]))
    return result
