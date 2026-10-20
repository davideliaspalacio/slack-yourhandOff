"""Pasa los mensajes de Slack por el detector y deja constancia de cada uno.

Toma los mensajes que aún no tienen fila en `message_signals` y que caben en la
edad máxima de una alerta (`alertas_edad_maxima_horas`), los más recientes
primero. Los más viejos ni se leen ni se pagan: de nada serviría clasificar un
mensaje cuya alerta caducaría al instante (`alertas._caducar`), y en el primer
arranque con el histórico entero serían miles de llamadas para publicar cero
alertas. No se marcan: la consulta los ignora para siempre. Por cada mensaje escribe siempre una fila (detectada o no): así no se
reprocesa ni se paga dos veces. Devuelve solo las detectadas, con lo necesario
para publicar la alerta. No publica nada ni aplica los topes de alertas
(`alertas_por_dia`, `alertas_dias_misma_persona`): eso es del paso siguiente.

Quedan fuera los mensajes del dueño de la credencial y de bots (el watcher ya
los marca `ignorado`), los de personas en estado `descartado` y los sin texto.
Las personas contactadas sí entran: una necesidad nueva de alguien ya
contactado es justo el momento de volver.

Las respuestas de un hilo (migración 0032: `thread_ts` no nulo y distinto de
`ts`) pasan por el mismo camino, pero el detector recibe además lo anterior del
hilo: el mensaje principal y las respuestas previas, sin los `ignorado`. Las
`archivado` de un hilo son contexto viejo y nunca se clasifican, aunque cayeran
dentro de la edad máxima. La fila de la señal guarda su `thread_ts` para que
`alertas.py` agrupe en un solo aviso todo lo que sale de un mismo hilo.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .. import db, db_config, guards, ledger
from . import detector

logger = logging.getLogger(__name__)

BATCH = 20
ACCION_PARADA = "senales_parada"


def edad_maxima_horas() -> int:
    """Hasta qué edad un mensaje puede dar una alerta. Lo lee también
    `alertas.py`, para que detectar y caducar usen siempre el mismo corte."""
    return db_config.value("alertas_edad_maxima_horas", 24)


def _pendientes(limit: int) -> list[dict]:
    desde = (datetime.now(UTC) - timedelta(hours=edad_maxima_horas())).timestamp()
    return db.fetch_all(
        """
        select sm.channel_id, sm.ts, sm.user_id, sm.text, p.id::text as prospect_id,
               case when sm.thread_ts is not null and sm.thread_ts <> sm.ts
                    then sm.thread_ts end as thread_ts
        from slack_messages sm
        left join prospects p on p.slack_user_id = sm.user_id
        where sm.status <> 'ignorado'
          and not (sm.status = 'archivado' and sm.thread_ts is not null)
          and sm.user_id is not null
          and btrim(coalesce(sm.text, '')) <> ''
          and (p.state is null or p.state <> 'descartado')
          and sm.ts::numeric >= %s
          and not exists (
              select 1 from message_signals ms
              where ms.channel_id = sm.channel_id and ms.ts = sm.ts
          )
        order by sm.ts::numeric desc
        limit %s
        """,
        (desde, limit),
    )


def _hilo(mensaje: dict) -> list[dict]:
    """Lo anterior del hilo de una respuesta, del más antiguo al más reciente,
    como lo espera `detector.consultar`.

    El principal se busca por su `ts` y no por su `thread_ts`: si el watcher lo
    guardó antes de que tuviera respuestas, su `thread_ts` está a null. Entran
    las filas `archivado` (son justo el contexto).

    De las `ignorado`, entran las que escribió una persona (con autor y sin subtipo de
    sistema): en la práctica, los mensajes del propio Anthony, que el watcher marca
    `ignorado` para no investigarse a sí mismo. Como contexto hacen falta: si Anthony
    abre un hilo preguntando "¿quién necesita ayuda con soporte?", las respuestas son lo
    más valioso de todo el club, y sin su mensaje el modelo no sabe a qué responden.
    Siguen fuera los bots y los avisos de sistema (uniones, mensajes borrados), que
    tienen subtipo. Sin tope aquí: el detector recorta a lo que cabe."""
    filas = db.fetch_all(
        """
        select ts, user_id, text from slack_messages
        where channel_id = %s
          and (ts = %s or (thread_ts = %s and ts::numeric < %s::numeric))
          and (status <> 'ignorado' or (user_id is not null and subtype is null))
          and btrim(coalesce(text, '')) <> ''
        order by ts::numeric
        """,
        (mensaje["channel_id"], mensaje["thread_ts"], mensaje["thread_ts"], mensaje["ts"]),
    )
    hilo = []
    for fila in filas:
        mismo = fila["user_id"] is not None and fila["user_id"] == mensaje["user_id"]
        if fila["ts"] == mensaje["thread_ts"]:
            entrada = {"quien": "original_post", "texto": fila["text"]}
            if mismo:
                # Extensión del contrato: el detector solo la usa para decirle al
                # modelo que la necesidad del principal es de quien responde.
                entrada["mismo_autor"] = True
        else:
            entrada = {"quien": "same_person" if mismo else "another_member", "texto": fila["text"]}
        hilo.append(entrada)
    return hilo


def _guardar(mensaje: dict, senal: detector.Senal) -> None:
    db.execute(
        """
        insert into message_signals
            (prospect_id, channel_id, ts, detectada, tipo, cita, confianza, thread_ts)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (channel_id, ts) do nothing
        """,
        (
            mensaje["prospect_id"],
            mensaje["channel_id"],
            mensaje["ts"],
            senal.detectada,
            senal.tipo if senal.detectada else None,
            senal.cita if senal.detectada else None,
            senal.confianza if senal.detectada else None,
            mensaje["thread_ts"],
        ),
    )


def _parada(motivo: Exception) -> None:
    logger.warning("señales: parada del sistema (%s); no se procesa", type(motivo).__name__)
    ledger.record_action(
        action=ACCION_PARADA,
        payload={},
        result={"motivo": type(motivo).__name__, "detalle": str(motivo)},
    )


def procesar_pendientes(limit: int = BATCH) -> list[dict]:
    """Clasifica hasta `limit` mensajes pendientes y devuelve las señales
    detectadas: dicts con prospect_id (None si quien escribe aún no es un
    prospect), slack_user_id, channel_id, ts, tipo, cita y confianza, y además
    thread_ts si el mensaje es una respuesta en un hilo."""
    if not db_config.value("senales_activas", True):
        return []
    try:
        guards.check_kill_switch()
        guards.check_monthly_budget()
    except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
        _parada(exc)
        return []

    detectadas: list[dict] = []
    for mensaje in _pendientes(limit):
        try:
            if mensaje["thread_ts"]:
                senal = detector.consultar(
                    mensaje["text"], mensaje["prospect_id"], hilo=_hilo(mensaje)
                )
            else:
                senal = detector.consultar(mensaje["text"], mensaje["prospect_id"])
        except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
            # Parada a mitad de lote: este mensaje no se leyó, así que no se
            # marca; lo recogerá la primera vuelta tras la parada.
            _parada(exc)
            break
        try:
            _guardar(mensaje, senal)
        except Exception:
            logger.exception("señales: no se pudo guardar la fila de %s", mensaje["ts"])
            continue
        if senal.detectada:
            detectada = {
                "prospect_id": mensaje["prospect_id"],
                "slack_user_id": mensaje["user_id"],
                "channel_id": mensaje["channel_id"],
                "ts": mensaje["ts"],
                "tipo": senal.tipo,
                "cita": senal.cita,
                "confianza": senal.confianza,
            }
            if mensaje["thread_ts"]:
                # Solo en las respuestas: lo de un mensaje principal no cambia.
                detectada["thread_ts"] = mensaje["thread_ts"]
            detectadas.append(detectada)
    return detectadas
