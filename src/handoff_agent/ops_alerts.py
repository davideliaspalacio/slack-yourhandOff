"""Operational alerts: problems that need a person, never swallowed.

Each alert goes to the log at CRITICAL, to the agent_actions ledger and — if
HANDOFF_ALERT_WEBHOOK_URL is set — to an incoming webhook in Handoff's own
Slack. Never to the Founders Club. Failing to deliver an alert is logged, not
raised: an alert must not take down the process it is reporting on.
"""

from __future__ import annotations

import logging

import httpx

from . import ledger
from .config import load_settings

logger = logging.getLogger(__name__)
WEBHOOK_TIMEOUT_SECONDS = 10


def alert(kind: str, message: str) -> None:
    logger.critical("ALERTA %s: %s", kind, message)
    try:
        ledger.record_action("alerta_operativa", {"tipo": kind, "mensaje": message})
    except Exception:
        logger.exception("no se pudo registrar la alerta en agent_actions")

    url = load_settings().alert_webhook_url
    if not url:
        return
    try:
        httpx.post(
            url,
            json={"text": f":rotating_light: *{kind}*: {message}"},
            timeout=WEBHOOK_TIMEOUT_SECONDS,
        ).raise_for_status()
    except httpx.HTTPError:
        logger.exception("no se pudo enviar la alerta al webhook de Handoff")
