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
    """Store a new dossier version for the person. Returns the version number."""
    row = db.fetch_one(
        """
        insert into dossiers (prospect_id, version, content, sources)
        values (
            %s,
            (select coalesce(max(version), 0) + 1 from dossiers where prospect_id = %s),
            %s, %s
        )
        returning version
        """,
        (prospect_id, prospect_id, json.dumps(content), json.dumps(sources)),
    )
    return row["version"]


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
