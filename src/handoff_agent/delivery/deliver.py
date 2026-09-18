"""Del dossier guardado a la tarjeta en el Slack de Handoff.

Corre justo después del research, con el dossier fresco. Nunca tumba el
research: si Slack falla, la investigación ya está pagada y guardada, y la
entrega se anota como fallida para reintentarla a mano o desde el panel.
"""

from __future__ import annotations

import logging

from .. import db, guards, ledger
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
    try:
        guards.check_kill_switch()
        guards.check_monthly_budget()
    except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
        # Las paradas del sistema cortan la entrega igual que el research,
        # pero el research ya corrió, está pagado y guardado: la parada no
        # puede propagarse desde aquí, solo impedir que salga la tarjeta.
        logger.warning("entrega detenida: %s", exc)
        ledger.record_action(
            "entrega_detenida",
            {"motivo": f"{type(exc).__name__}: {exc}"},
            prospect_id=prospect_id,
        )
        return None

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

    # Reserva atómica antes de publicar: el índice único deliveries_one_card
    # (prospect_id, dossier_version where kind = 'slack') decide quién entrega.
    # El conflict target repite el predicado del índice parcial para que
    # Postgres pueda inferirlo. Si no se insertó nada, alguien ya entregó esta
    # versión o está entregándola ahora mismo: no hay nada que hacer aquí.
    reservation = db.fetch_one(
        "insert into deliveries (prospect_id, kind, band, dossier_version, channel_id, "
        "message_ts) values (%s, 'slack', %s, %s, null, null) "
        "on conflict (prospect_id, dossier_version) where kind = 'slack' do nothing "
        "returning id",
        (prospect_id, band, dossier["version"]),
    )
    if reservation is None:
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
        # tumbarlo. Se libera la reserva para que un intento posterior sí
        # pueda entregar esta versión.
        logger.exception("no se pudo publicar la tarjeta")

        # Intenta liberar la reserva. Si el delete también falla (blip de
        # base de datos), eso nunca puede propagarse: la entrega ya falló,
        # y la reserva se queda atascada. Se registra para limpiar a mano.
        try:
            db.execute("delete from deliveries where id = %s", (reservation["id"],))
        except Exception as delete_exc:
            logger.exception("no se pudo liberar la reserva de entrega %s", reservation["id"])
            ledger.record_action(
                "entrega_reserva_atascada",
                {
                    "motivo": f"{type(delete_exc).__name__}: {delete_exc}",
                    "delivery_id": str(reservation["id"]),
                    "dossier_version": dossier["version"],
                },
                prospect_id=prospect_id,
            )

        # Siempre registra el fallo de publicación, haya o no fallado el delete.
        ledger.record_action(
            "entrega_fallida",
            {"motivo": f"{type(exc).__name__}: {exc}"},
            prospect_id=prospect_id,
        )
        return None

    try:
        db.execute(
            "update deliveries set channel_id = %s, message_ts = %s where id = %s",
            (channel, ts, reservation["id"]),
        )
    except Exception:
        # La tarjeta ya salió a Slack: no hay forma de deshacerla ni de
        # reintentar sin arriesgar un duplicado. La reserva se queda sin
        # channel_id/message_ts, pero sigue viva y sigue bloqueando una
        # segunda tarjeta para esta misma versión -- por eso esto se registra
        # y no se traga en silencio.
        logger.exception(
            "la tarjeta se publicó (canal=%s, ts=%s) pero no se pudo anotar la entrega %s",
            channel,
            ts,
            reservation["id"],
        )

    return band
