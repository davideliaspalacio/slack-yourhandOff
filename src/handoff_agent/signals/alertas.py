"""Publica en Slack las señales detectadas en los mensajes, con freno.

`publicar_pendientes` es el paso que el bucle del worker corre en cada vuelta:
pasa los mensajes nuevos por el detector (`procesar_pendientes`) y publica una
alerta por señal, respetando cuatro frenos y las paradas del sistema:

- Edad máxima (`alertas_edad_maxima_horas`, 24): un mensaje más viejo no genera
  alerta. Es lo que evita inundar el canal con el histórico en el primer
  arranque; esos mensajes quedan `omitida` con motivo `too_old`.
- Tope diario (`alertas_por_dia`, 5), contado en la zona horaria de
  `zona_horaria` igual que el SMS. Una señal que no cabe hoy sigue pendiente y
  se publica mañana si todavía no ha caducado por edad.
- Una alerta por persona y periodo (`alertas_dias_misma_persona`, 7), por
  `slack_user_id`: así cuenta también para quien aún no es un prospect.
- Nunca a un descartado. A diferencia de `sms.maybe_send`, un *contactado* sí
  recibe alerta (una necesidad nueva de alguien ya contactado es justo el
  momento de volver), así que aquí no se usa `NO_ALERT_STATES`.

Dónde queda anotado: en las columnas `alerta_*` de `message_signals` (migración
0022), no en `deliveries`. Allí `prospect_id` y `band` son obligatorios y una
señal puede venir de alguien que aún no es prospect; en cambio la fila de la
señal responde sola a "¿se publicó ya?": `alerta_estado` es `publicada` (con
`alerta_channel_id`/`alerta_ts`), `omitida` (con `alerta_motivo`), `reservada`
(en curso, o atascada tras un fallo al anotar) o nulo (pendiente).

Igual que en `delivery/deliver.py`, se reserva antes de publicar: así un segundo
worker, o un reintento tras un fallo al anotar el resultado, no duplica la
alerta. Un fallo al publicar libera la reserva y se reintenta en la vuelta
siguiente, hasta `MAX_INTENTOS` veces; después queda `omitida`.

Nada de esto puede propagarse: las señales son un extra, no pueden tumbar el
bucle ni el research.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import db, db_config, guards, ledger
from ..config import load_settings
from ..delivery import alerta_card, slack_writer
from . import procesar

logger = logging.getLogger(__name__)

MAX_INTENTOS = 3
ACCION_PARADA = "alertas_parada"
ACCION_PUBLICADA = "alerta_publicada"
ACCION_FALLIDA = "alerta_fallida"
# Motivos de omisión (alerta_motivo): decisiones finales, se quedan en la fila.
MOTIVO_ANTIGUA = "too_old"
MOTIVO_DESCARTADO = "discarded"
MOTIVO_MISMA_PERSONA = "same_person_window"
# Las personas en modo de pruebas (`prospects.sin_tarjeta`) tampoco reciben
# alerta, igual que no sale su tarjeta de research.
MOTIVO_SIN_TARJETA = "test_mode"
MOTIVO_FALLO = "publish_failed"


def _parada(exc: Exception) -> None:
    logger.warning("alertas detenidas: %s", exc)
    try:
        ledger.record_action(ACCION_PARADA, {}, {"motivo": f"{type(exc).__name__}: {exc}"})
    except Exception:
        logger.exception("no se pudo registrar la parada de las alertas")


def _inicio_del_dia(ahora: datetime) -> datetime:
    zona = ZoneInfo(db_config.value("zona_horaria", "America/New_York"))
    return ahora.astimezone(zona).replace(hour=0, minute=0, second=0, microsecond=0)


def _publicadas_desde(desde: datetime) -> int:
    return db.fetch_one(
        "select count(*) as n from message_signals "
        "where alerta_estado in ('reservada', 'publicada') and alerta_at >= %s",
        (desde,),
    )["n"]


def _misma_persona_reciente(slack_user_id: str, desde: datetime) -> bool:
    return (
        db.fetch_one(
            "select 1 as hay from message_signals ms "
            "join slack_messages sm on sm.channel_id = ms.channel_id and sm.ts = ms.ts "
            "where sm.user_id = %s and ms.alerta_estado in ('reservada', 'publicada') "
            "and ms.alerta_at >= %s limit 1",
            (slack_user_id, desde),
        )
        is not None
    )


def _omitir(senal_id: str, motivo: str) -> None:
    db.execute(
        "update message_signals set alerta_estado = 'omitida', alerta_motivo = %s, "
        "alerta_at = now() where id = %s and alerta_estado is null",
        (motivo, senal_id),
    )


def _caducar(ahora: datetime, horas: int) -> None:
    """Marca como omitidas las señales pendientes de mensajes demasiado viejos."""
    limite = (ahora - timedelta(hours=horas)).timestamp()
    db.execute(
        "update message_signals set alerta_estado = 'omitida', alerta_motivo = %s, "
        "alerta_at = now() "
        "where detectada and alerta_estado is null and ts::numeric < %s",
        (MOTIVO_ANTIGUA, limite),
    )


def _pendientes() -> list[dict]:
    """Señales detectadas sin decisión: las de más confianza primero, y a igual
    confianza las más recientes. El join con slack_messages da quién escribió."""
    return db.fetch_all(
        """
        select ms.id::text as id, ms.channel_id, ms.ts, ms.tipo, ms.cita,
               ms.alerta_intentos, sm.user_id as slack_user_id
        from message_signals ms
        join slack_messages sm on sm.channel_id = ms.channel_id and sm.ts = ms.ts
        where ms.detectada and ms.alerta_estado is null and sm.user_id is not null
        order by case ms.confianza when 'alta' then 0 when 'media' then 1 else 2 end,
                 ms.ts::numeric desc
        """
    )


def _reservar(senal: dict) -> bool:
    return (
        db.fetch_one(
            "update message_signals set alerta_estado = 'reservada', alerta_at = now(), "
            "alerta_intentos = alerta_intentos + 1 "
            "where id = %s and alerta_estado is null returning id",
            (senal["id"],),
        )
        is not None
    )


def _liberar(senal: dict) -> None:
    """Tras un fallo al publicar: de vuelta a pendiente, o definitiva si ya
    se agotaron los intentos (la reserva ya contó el de ahora)."""
    if senal["alerta_intentos"] + 1 >= MAX_INTENTOS:
        db.execute(
            "update message_signals set alerta_estado = 'omitida', alerta_motivo = %s "
            "where id = %s and alerta_estado = 'reservada'",
            (MOTIVO_FALLO, senal["id"]),
        )
    else:
        db.execute(
            "update message_signals set alerta_estado = null, alerta_at = null "
            "where id = %s and alerta_estado = 'reservada'",
            (senal["id"],),
        )


def _permalink(reader, senal: dict) -> str | None:
    """El enlace real al mensaje original, que vive en el Slack del Founders
    Club: solo el lector (su credencial) puede pedirlo, el bot de Handoff no ve
    ese workspace. Sin lector, o si falla, la alerta sale sin el botón."""
    if reader is None:
        return None
    try:
        return reader.permalink(senal["channel_id"], senal["ts"])
    except Exception:
        logger.exception("alertas: no se pudo pedir el permalink de %s", senal["ts"])
        return None


def _registrar(action: str, payload: dict, prospect_id: str | None = None) -> None:
    try:
        ledger.record_action(action, payload, prospect_id=prospect_id)
    except Exception:
        logger.exception("alertas: no se pudo registrar %s en el ledger", action)


def _publicar_una(senal: dict, persona: dict | None, reader) -> bool:
    """Publica una alerta ya reservada. True si salió a Slack."""
    prospect_id = str(persona["id"]) if persona else None
    try:
        dossier = None
        if persona:
            fila = db.fetch_one(
                "select content from dossiers where prospect_id = %s order by version desc limit 1",
                (persona["id"],),
            )
            dossier = fila["content"] if fila else None
        bloques = alerta_card.build(senal, persona, dossier, _permalink(reader, senal))
        canal, ts = slack_writer.post_card(bloques, alerta_card.summary(senal, persona))
    except Exception as exc:
        logger.exception("alertas: no se pudo publicar la alerta de %s", senal["ts"])
        try:
            _liberar(senal)
        except Exception:
            logger.exception("alertas: no se pudo liberar la reserva de %s", senal["id"])
        _registrar(
            ACCION_FALLIDA,
            {"motivo": f"{type(exc).__name__}: {exc}", "senal_id": senal["id"]},
            prospect_id,
        )
        return False

    try:
        db.execute(
            "update message_signals set alerta_estado = 'publicada', alerta_channel_id = %s, "
            "alerta_ts = %s where id = %s",
            (canal, ts, senal["id"]),
        )
    except Exception:
        # Ya salió a Slack: la fila se queda 'reservada' (cuenta para los topes
        # y bloquea una segunda alerta) en vez de arriesgar un duplicado.
        logger.exception(
            "alertas: publicada (canal=%s, ts=%s) pero no se pudo anotar la señal %s",
            canal,
            ts,
            senal["id"],
        )
    _registrar(
        ACCION_PUBLICADA,
        {"senal_id": senal["id"], "tipo": senal["tipo"], "canal": canal, "ts": ts},
        prospect_id,
    )
    return True


def _publicar(reader, ahora: datetime) -> int:
    if not db_config.value("senales_activas", True):
        return 0
    try:
        guards.check_kill_switch()
        guards.check_monthly_budget()
    except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
        _parada(exc)
        return 0

    try:
        procesar.procesar_pendientes()
    except Exception:
        # Un fallo del detector no impide publicar lo que ya estaba detectado.
        logger.exception("alertas: el detector falló; se publica lo pendiente")

    _caducar(ahora, db_config.value("alertas_edad_maxima_horas", 24))

    settings = load_settings()
    if not (settings.handoff_bot_token and settings.handoff_channel_id):
        # Sin bot o sin canal no hay dónde publicar: ni intento ni fallo
        # registrado por cada señal. Las pendientes caducan por edad.
        logger.info("alertas sin configurar (faltan HANDOFF_SLACK_*), se omite")
        return 0

    hoy = _publicadas_desde(_inicio_del_dia(ahora))
    cupo = db_config.value("alertas_por_dia", 5) - hoy
    if cupo <= 0:
        logger.info("alertas: tope diario alcanzado")
        return 0
    ventana = ahora - timedelta(days=db_config.value("alertas_dias_misma_persona", 7))

    publicadas = 0
    for senal in _pendientes():
        if publicadas >= cupo:
            break
        persona = db.fetch_one(
            "select * from prospects where slack_user_id = %s", (senal["slack_user_id"],)
        )
        if persona and persona["state"] == "descartado":
            _omitir(senal["id"], MOTIVO_DESCARTADO)
            continue
        if persona and persona["sin_tarjeta"]:
            _omitir(senal["id"], MOTIVO_SIN_TARJETA)
            continue
        if _misma_persona_reciente(senal["slack_user_id"], ventana):
            _omitir(senal["id"], MOTIVO_MISMA_PERSONA)
            continue
        if not _reservar(senal):
            continue
        if _publicar_una(senal, persona, reader):
            publicadas += 1
        else:
            # Slack probablemente está caído: no se insiste con las demás.
            break
    return publicadas


def publicar_pendientes(reader=None, now: datetime | None = None) -> int:
    """Detecta y publica; devuelve cuántas alertas salieron. Nunca lanza.

    `reader` (el lector del Founders Club) solo sirve para pedir el permalink
    del mensaje original; sin él las alertas salen sin ese botón."""
    try:
        return _publicar(reader, now or datetime.now(UTC))
    except Exception:
        logger.exception("alertas: fallo inesperado; se reintenta en la vuelta siguiente")
        return 0
