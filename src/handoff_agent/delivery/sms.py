"""SMS solo para lo más caliente, con tope duro y horario.

Tres alertas malas seguidas y Anthony deja de abrirlas. El SMS lleva una línea
y el permalink real de la tarjeta de Slack (nunca el dossier): el contexto ya
está ahí. Las paradas del sistema (kill switch, tope mensual) cortan el envío
igual que cortan el research y la publicación de la tarjeta -- ver
delivery/deliver.py, que sigue el mismo patrón.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import db, db_config, guards, ledger
from ..config import load_settings
from . import slack_writer

logger = logging.getLogger(__name__)


def _send(to: str, body: str) -> str:
    # Import perezoso: nada carga el paquete de Twilio solo por importar este
    # módulo, igual que openai/slack_sdk se cargan en sus propios puntos de uso.
    from twilio.rest import Client

    settings = load_settings()
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return client.messages.create(to=to, from_=settings.twilio_from, body=body).sid


def _within_hours(now: datetime) -> bool:
    zone = ZoneInfo(db_config.value("zona_horaria", "America/New_York"))
    hour = now.astimezone(zone).hour
    return db_config.value("sms_hora_inicio", 7) <= hour < db_config.value("sms_hora_fin", 21)


def _sent_today(now: datetime) -> int:
    zone = ZoneInfo(db_config.value("zona_horaria", "America/New_York"))
    start_of_day = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return db.fetch_one(
        "select count(*) as n from deliveries where kind = 'sms' and created_at >= %s",
        (start_of_day,),
    )["n"]


def _card_link(prospect_id: str) -> str | None:
    """El permalink de la última tarjeta de Slack de esta persona, si hay una."""
    card = db.fetch_one(
        "select channel_id, message_ts from deliveries "
        "where prospect_id = %s and kind = 'slack' order by created_at desc limit 1",
        (prospect_id,),
    )
    if card is None or not card["channel_id"] or not card["message_ts"]:
        return None
    try:
        return slack_writer.card_permalink(card["channel_id"], card["message_ts"])
    except slack_writer.SlackWriteRefused:
        # Sin token de bot no hay permalink que pedir, pero eso no puede
        # impedir que salga el SMS: sale sin enlace.
        logger.warning("no se pudo pedir el permalink: falta el token del bot de Handoff")
        return None


def maybe_send(prospect_id: str, band: str, now: datetime | None = None) -> bool:
    try:
        guards.check_kill_switch()
        guards.check_monthly_budget()
    except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
        # Igual que deliver_for: las paradas del sistema cortan el envío sin
        # perder nada que ya se pagó, y sin propagarse hacia el runner.
        logger.warning("sms detenido: %s", exc)
        ledger.record_action(
            "sms_detenido",
            {"motivo": f"{type(exc).__name__}: {exc}"},
            prospect_id=prospect_id,
        )
        return False

    if band != "alta":
        return False

    now = now or datetime.now(tz=ZoneInfo("UTC"))
    if not _within_hours(now):
        logger.info("sms fuera de horario, se omite")
        return False
    if _sent_today(now) >= db_config.value("sms_por_dia", 3):
        logger.info("sms: tope diario alcanzado")
        return False

    person = db.fetch_one("select * from prospects where id = %s", (prospect_id,))
    link = _card_link(prospect_id)

    body = f"Señal alta: {person['full_name'] or person['slack_user_id']}"
    if person["company_name"]:
        body += f" ({person['company_name']})"
    if link:
        body += f" {link}"

    settings = load_settings()
    try:
        sid = _send(settings.twilio_to, body)
    except Exception as exc:  # noqa: BLE001 - un SMS caído no tumba el pipeline
        # Nunca se registra str(exc) ni la petición: un error de Twilio puede
        # traer de vuelta el número de destino o el cuerpo enviado, y no hay
        # forma de garantizar que nunca lleve el token de autenticación. Solo
        # se guarda el tipo de excepción, tanto en el log como en el ledger.
        logger.error("no se pudo enviar el SMS (%s)", type(exc).__name__)
        ledger.record_action(
            "sms_fallido",
            {"motivo": type(exc).__name__},
            prospect_id=prospect_id,
        )
        return False

    db.execute(
        "insert into deliveries (prospect_id, kind, band, external_id) values (%s, 'sms', %s, %s)",
        (prospect_id, band, sid),
    )
    ledger.record_cost_event(
        "twilio_sms", settings.price_twilio_per_sms, "SMS de señal alta", prospect_id
    )
    return True
