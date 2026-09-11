"""Hourly read of the Founders Club Slack: new messages and new members.

No backfill, on purpose: the first read of a channel looks back
SLACK_LOOKBACK_HOURS only, and the first member snapshot is a baseline.
Otherwise the first run would queue research on all 1,400 members, which was
ruled out. Later reads start right after the last stored message.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import db
from ..slack_client import SlackUnavailable
from . import queue

# Mensajes que no escribió una persona o que no dicen nada: nunca disparan research.
SYSTEM_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "bot_message",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "pinned_item",
        "unpinned_item",
        "tombstone",
        "message_deleted",
        "message_changed",
    }
)


@dataclass
class TickResult:
    messages_new: int = 0
    messages_ignored: int = 0
    members_new: int = 0
    errors: list[str] = field(default_factory=list)


def classify(message: dict, owner_id: str) -> str:
    user = message.get("user")
    if (
        not user
        or user == owner_id
        or message.get("bot_id")
        or message.get("subtype") in SYSTEM_SUBTYPES
    ):
        return "ignorado"
    return "nuevo"


def _oldest(channel: str, lookback_hours: float, now: Callable[[], float]) -> str:
    row = db.fetch_one(
        "select ts from slack_messages where channel_id = %s order by ts::numeric desc limit 1",
        (channel,),
    )
    if row:
        return row["ts"]
    return f"{now() - lookback_hours * 3600:.6f}"


def _store(channel: str, message: dict, status: str) -> bool:
    inserted = db.execute(
        """
        insert into slack_messages (channel_id, ts, user_id, text, subtype, thread_ts, status)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (channel_id, ts) do nothing
        """,
        (
            channel,
            message["ts"],
            message.get("user"),
            message.get("text"),
            message.get("subtype"),
            message.get("thread_ts"),
            status,
        ),
    )
    return inserted == 1


def _diff_members(reader, channel: str, owner_id: str, result: TickResult) -> None:
    current = reader.members(channel)
    previous = db.fetch_one(
        "select members from member_snapshots where channel_id = %s order by taken_at desc limit 1",
        (channel,),
    )
    db.execute(
        "insert into member_snapshots (channel_id, members) values (%s, %s)",
        (channel, sorted(current)),
    )
    if previous is None:
        return  # línea base: nunca se encola a todo el padrón
    for user_id in sorted(current - set(previous["members"]) - {owner_id}):
        if queue.enqueue(user_id, "miembro_nuevo"):
            result.members_new += 1


def watch_tick(
    reader,
    channels: list[str],
    lookback_hours: float,
    now: Callable[[], float] = time.time,
) -> TickResult:
    """Read every channel once. SlackAuthFailed propagates: it needs a person."""
    result = TickResult()
    owner_id = reader.owner_id()
    for channel in channels:
        try:
            for message in reader.history(channel, oldest=_oldest(channel, lookback_hours, now)):
                status = classify(message, owner_id)
                if _store(channel, message, status):
                    if status == "nuevo":
                        result.messages_new += 1
                    else:
                        result.messages_ignored += 1
            _diff_members(reader, channel, owner_id, result)
        except SlackUnavailable as exc:
            result.errors.append(f"{channel}: {exc}")
    return result
