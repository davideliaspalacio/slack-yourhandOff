"""Everything the agent knows about a person before it decides anything.

The person, not the message, is the unit of work: research is paid for once and
every later message is scored against the dossier this returns.
"""

from __future__ import annotations

import json

from .. import db

RECENT_ACTIONS_LIMIT = 20


def upsert_prospect(slack_user_id: str, full_name: str | None = None) -> dict:
    """Return the person's row, creating it on first sight.

    Never overwrites a known name with NULL: a later sighting with less
    information must not erase what an earlier one established.
    """
    return db.fetch_one(
        """
        insert into prospects (slack_user_id, full_name)
        values (%s, %s)
        on conflict (slack_user_id) do update
            set full_name = coalesce(excluded.full_name, prospects.full_name),
                updated_at = now()
        returning *
        """,
        (slack_user_id, full_name),
    )


def save_dossier(prospect_id: str, content: dict, sources: list) -> int:
    """Store a new dossier version for the person. Returns the version number.

    `sources` are `{"url", "kind", "title"}` records (Gathered.source_records).
    The six rows from the first live test hold bare URL strings instead, so
    anything reading `dossiers.sources` must accept both shapes.

    The advisory lock is not optional. Under READ COMMITTED, two concurrent
    saves both read the same max(version) and the second one dies on the unique
    constraint — throwing away a dossier that cost real money to research. Both
    spec triggers can land on the same person at once (they wrote a message and
    joined as a new member), and so can the panel's re-investigar button.

    The lock is taken in its own statement rather than in a CTE: PostgreSQL does
    not guarantee when a CTE is evaluated, so folding it into the insert leaves
    the race exactly as it was.
    """
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (str(prospect_id),))
        cur.execute(
            """
            insert into dossiers (prospect_id, version, content, sources)
            select %s, coalesce(max(version), 0) + 1, %s, %s
            from dossiers
            where prospect_id = %s
            returning version
            """,
            (prospect_id, json.dumps(content), json.dumps(sources), prospect_id),
        )
        return cur.fetchone()["version"]


def historial_prospecto(slack_user_id: str) -> dict:
    """What we already know: the person, their latest dossier, recent actions."""
    prospect = db.fetch_one("select * from prospects where slack_user_id = %s", (slack_user_id,))
    if prospect is None:
        return {"prospect": None, "dossier": None, "actions": []}

    dossier = db.fetch_one(
        "select * from dossiers where prospect_id = %s order by version desc limit 1",
        (prospect["id"],),
    )
    actions = db.fetch_all(
        "select action, payload, result, created_at from agent_actions "
        "where prospect_id = %s order by created_at desc limit %s",
        (prospect["id"], RECENT_ACTIONS_LIMIT),
    )
    return {"prospect": prospect, "dossier": dossier, "actions": actions}
