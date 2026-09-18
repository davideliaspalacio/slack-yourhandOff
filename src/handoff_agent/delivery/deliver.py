"""Del dossier guardado a la tarjeta en el Slack de Handoff.

Corre justo después del research, con el dossier fresco. Nunca tumba el
research: si Slack falla, la investigación ya está pagada y guardada, y la
entrega se anota como fallida para reintentarla a mano o desde el panel.
"""

from __future__ import annotations

import logging

from .. import db, ledger
from . import bands, card, slack_writer

logger = logging.getLogger(__name__)
NO_ALERT_STATES = ("descartado",)


def _last_message(slack_user_id: str) -> dict | None:
    """El mensaje más reciente de la persona que aún sirve de cita.

    Para cuando esto corre, ingest/resolver.py ya movió el mensaje de
    'nuevo' a 'pendiente_scoring' (o 'archivado'): filtrar por 'nuevo', como
    hacía una versión anterior de este módulo, nunca encontraría nada que
    citar. Solo se descarta 'ignorado' (mensajes sin user_id válido, que
    nunca representaron a esta persona).
    """
    return db.fetch_one(
        "select channel_id, ts, text from slack_messages "
        "where user_id = %s and status <> 'ignorado' order by ts::numeric desc limit 1",
        (slack_user_id,),
    )


def deliver_for(prospect_id: str, reader) -> str | None:
    person = db.fetch_one("select * from prospects where id = %s", (prospect_id,))
    if person is None or person["state"] in NO_ALERT_STATES:
        return None

    dossier = db.fetch_one(
        "select version, content from dossiers where prospect_id = %s "
        "order by version desc limit 1",
        (prospect_id,),
    )
    if dossier is None:
        return None

    band = bands.band_for(dossier["content"])
    if band is None:
        return None
    if band == "baja":
        # Banda baja no interrumpe: la recoge el email diario leyendo dossiers.
        return band

    already = db.fetch_one(
        "select id from deliveries where prospect_id = %s and dossier_version = %s "
        "and kind = 'slack'",
        (prospect_id, dossier["version"]),
    )
    if already:
        return None

    message = _last_message(person["slack_user_id"])
    permalink = reader.permalink(message["channel_id"], message["ts"]) if message else None
    blocks = card.build(person, dossier["content"], band, message, permalink)
    summary = f"Señal {band}: {person['full_name'] or person['slack_user_id']}"

    try:
        channel, ts = slack_writer.post_card(blocks, summary)
    except Exception as exc:
        # logger.exception ya evita que ruff lo marque como "except" ciego, y
        # el research ya está pagado y guardado: un fallo de Slack no puede
        # tumbarlo.
        logger.exception("no se pudo publicar la tarjeta")
        ledger.record_action(
            "entrega_fallida",
            {"motivo": f"{type(exc).__name__}: {exc}"},
            prospect_id=prospect_id,
        )
        return None

    db.execute(
        "insert into deliveries (prospect_id, kind, band, dossier_version, channel_id, "
        "message_ts) values (%s, 'slack', %s, %s, %s, %s)",
        (prospect_id, band, dossier["version"], channel, ts),
    )
    return band
