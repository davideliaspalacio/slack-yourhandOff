"""The long-running worker: read Slack once per poll interval, drain the
research queue in between.

A dead token or a system stop raises an operational alert and the loop keeps
running: the token may be renewed and the cap may be raised without anyone
restarting the process.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .. import ops_alerts
from ..research.worker import SYSTEM_STOPS
from ..slack_client import SlackAuthFailed
from . import queue
from .research_runner import run_next_job
from .resolver import resolve_pending
from .watcher import watch_tick

logger = logging.getLogger(__name__)
IDLE_SECONDS = 30
SYSTEM_STOP_PAUSE_SECONDS = 600


def _reclaim_stale() -> None:
    """Recover jobs abandoned by a worker killed mid-research (every Railway
    redeploy). Left en_curso, the one-open-job-per-person index would block
    that person forever."""
    reclaimed = queue.reclaim_stale()
    if reclaimed:
        logger.info("recuperadas %d tareas abandonadas por un worker anterior", reclaimed)


def run_loop(
    reader,
    *,
    channels: list[str],
    lookback_hours: float,
    poll_seconds: int,
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_cycles: int | None = None,
) -> int:
    next_watch = clock()
    cycles = 0
    _reclaim_stale()
    while not should_stop():
        if clock() >= next_watch:
            next_watch = clock() + poll_seconds
            _reclaim_stale()
            try:
                tick = watch_tick(reader, channels, lookback_hours)
                resolved = resolve_pending()
                logger.info(
                    "slack: %d mensajes nuevos, %d miembros nuevos; %d encolados",
                    tick.messages_new,
                    tick.members_new,
                    resolved.enqueued,
                )
                for error in tick.errors:
                    logger.warning("slack: %s", error)
            except SlackAuthFailed as exc:
                ops_alerts.alert("slack_auth", str(exc))

        try:
            result = run_next_job(reader)
        except SYSTEM_STOPS as exc:
            ops_alerts.alert("parada_del_sistema", str(exc))
            sleep(SYSTEM_STOP_PAUSE_SECONDS)
        except SlackAuthFailed as exc:
            ops_alerts.alert("slack_auth", str(exc))
            sleep(IDLE_SECONDS)
        else:
            if result is None or result.status == "limitado":
                sleep(IDLE_SECONDS)
            elif result.status != "hecho":
                logger.info(
                    "research %s: %s %s", result.slack_user_id, result.status, result.detail
                )

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
    return cycles
