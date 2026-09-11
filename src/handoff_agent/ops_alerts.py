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

    try:
        url = load_settings().alert_webhook_url
    except Exception as exc:  # noqa: BLE001 - la alerta no puede fallar
        logger.error("no se pudo cargar settings para la alerta: %s", type(exc).__name__)
        return
    if not url:
        return
    try:
        # Suppress httpx debug logging to prevent the webhook URL from appearing in logs
        httpx_logger = logging.getLogger("httpx")
        old_level = httpx_logger.level
        httpx_logger.setLevel(logging.WARNING)
        try:
            httpx.post(
                url,
                json={"text": f":rotating_light: *{kind}*: {message}"},
                timeout=WEBHOOK_TIMEOUT_SECONDS,
            ).raise_for_status()
        finally:
            httpx_logger.setLevel(old_level)
    except httpx.HTTPStatusError as exc:
        # The webhook URL is the credential; it must never reach the logs.
        logger.error("HTTP %s", exc.response.status_code)
    except Exception as exc:  # noqa: BLE001 - la alerta no puede fallar
        # The webhook URL is the credential; it must never reach the logs.
        logger.error(type(exc).__name__)
