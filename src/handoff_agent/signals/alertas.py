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

Las señales que salen de respuestas en un hilo (`message_signals.thread_ts`,
migración 0032) tienen reglas más estrictas, porque en un hilo casi todo lo que
se contesta no es una necesidad y un aviso por cada "+1" sería ruido:

- Solo avisan con `confianza = 'alta'`; con `media` o `baja` quedan `omitida`
  con motivo `thread_low_confidence`.
- Las pendientes de un mismo hilo salen en UNA alerta, con cada persona y su
  cita, hasta `alerta_card.MAX_PERSONAS_HILO`; las que no caben esperan a la
  vuelta siguiente. Esa alerta cuenta una sola vez contra `alertas_por_dia`, y
  `alertas_dias_misma_persona` sigue aplicándose a cada persona.
- Una alerta por persona y hilo: quien ya salió en una alerta de ese hilo, o el
  autor del principal si ya hubo alerta por el principal, no vuelve a salir.
- Se reservan todas las señales del grupo antes de publicar, y todas quedan con
  el mismo `alerta_channel_id`/`alerta_ts`.
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
# Hilos: una respuesta sin confianza alta no avisa, y una persona sale una sola
# vez por hilo (aunque responda dos veces, o sea la autora del principal).
MOTIVO_HILO_CONFIANZA = "thread_low_confidence"
MOTIVO_HILO_REPETIDA = "thread_same_person"


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
    """Alertas (no señales) salidas o en curso desde `desde`. Las señales de un
    hilo publicadas juntas comparten `alerta_ts`; mientras están reservadas aún
    no lo tienen, pero comparten `alerta_at` (las reservó una sola sentencia, y
    `now()` es el mismo para todas). Así un aviso de tres personas cuenta uno."""
    return db.fetch_one(
        """
        select count(distinct case
            when alerta_ts is not null then 'p:' || coalesce(alerta_channel_id, '') || ':' || alerta_ts
            when thread_ts is not null then 'h:' || channel_id || ':' || thread_ts || ':' || alerta_at::text
            else 's:' || id::text
        end) as n
        from message_signals
        where alerta_estado in ('reservada', 'publicada') and alerta_at >= %s
        """,
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


def _omitir_hilos_dudosos() -> None:
    """Una respuesta en un hilo solo avisa con confianza alta. Es una decisión
    final, como la edad: no depende del cupo de hoy, así que no espera a él."""
    db.execute(
        "update message_signals set alerta_estado = 'omitida', alerta_motivo = %s, "
        "alerta_at = now() "
        "where detectada and alerta_estado is null and thread_ts is not null "
        "and confianza is distinct from 'alta'",
        (MOTIVO_HILO_CONFIANZA,),
    )


def _pendientes() -> list[dict]:
    """Señales detectadas sin decisión: las de más confianza primero, y a igual
    confianza las más recientes. El join con slack_messages da quién escribió."""
    return db.fetch_all(
        """
        select ms.id::text as id, ms.channel_id, ms.ts, ms.thread_ts, ms.tipo, ms.cita,
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


def _persona_y_motivo(senal: dict, ventana: datetime) -> tuple[dict | None, str | None]:
    """La fila de `prospects` de quien escribió y, si no debe recibir alerta, el
    motivo. Los mismos frenos para un mensaje principal y para una respuesta."""
    persona = db.fetch_one(
        "select * from prospects where slack_user_id = %s", (senal["slack_user_id"],)
    )
    if persona and persona["state"] == "descartado":
        return persona, MOTIVO_DESCARTADO
    if persona and persona["sin_tarjeta"]:
        return persona, MOTIVO_SIN_TARJETA
    if _misma_persona_reciente(senal["slack_user_id"], ventana):
        return persona, MOTIVO_MISMA_PERSONA
    return persona, None


def _publicar_suelta(senal: dict, reader, ventana: datetime) -> bool | None:
    """Un mensaje principal: una alerta por señal, como siempre. None si no se
    publicó nada (omitida, o reservada por otro), True/False según Slack."""
    persona, motivo = _persona_y_motivo(senal, ventana)
    if motivo:
        _omitir(senal["id"], motivo)
        return None
    if not _reservar(senal):
        return None
    return _publicar_una(senal, persona, reader)


def _ya_avisada_en_hilo(slack_user_id: str, channel_id: str, thread_ts: str) -> bool:
    """¿Esta persona ya salió en una alerta de este hilo, o en la del principal?
    El `ms.ts = thread_ts` es la alerta del principal: por el join con su autor,
    solo cuenta para quien lo escribió."""
    return (
        db.fetch_one(
            "select 1 as hay from message_signals ms "
            "join slack_messages sm on sm.channel_id = ms.channel_id and sm.ts = ms.ts "
            "where ms.channel_id = %s and sm.user_id = %s "
            "and (ms.thread_ts = %s or ms.ts = %s) "
            "and ms.alerta_estado in ('reservada', 'publicada') limit 1",
            (channel_id, slack_user_id, thread_ts, thread_ts),
        )
        is not None
    )


def _reservar_grupo(ids: list[str]) -> set[str]:
    """Reserva de una vez todas las señales de un hilo. Una sola sentencia: si
    otro worker ya tenía alguna, esa no vuelve y la alerta sale sin ella, en vez
    de duplicarse. Por la misma razón comparten `alerta_at` (ver
    `_publicadas_desde`)."""
    filas = db.fetch_all(
        "update message_signals set alerta_estado = 'reservada', alerta_at = now(), "
        "alerta_intentos = alerta_intentos + 1 "
        "where id = any(%s::uuid[]) and alerta_estado is null returning id::text as id",
        (ids,),
    )
    return {fila["id"] for fila in filas}


def _publicar_hilo(grupo: list[dict], reader, ventana: datetime) -> bool | None:
    """Las señales pendientes de un mismo hilo, en una sola alerta. None si no
    quedó nadie a quien avisar, True/False según Slack."""
    channel_id, thread_ts = grupo[0]["channel_id"], grupo[0]["thread_ts"]
    elegidas: list[tuple[dict, dict | None]] = []
    vistos: set[str] = set()
    # Del más antiguo al más reciente: si alguien respondió dos veces, se queda
    # la primera vez que lo dijo.
    for senal in sorted(grupo, key=lambda s: float(s["ts"])):
        uid = senal["slack_user_id"]
        if uid in vistos:
            _omitir(senal["id"], MOTIVO_HILO_REPETIDA)
            continue
        if len(elegidas) >= alerta_card.MAX_PERSONAS_HILO:
            # Sin decisión: sigue pendiente y saldrá en la vuelta siguiente.
            continue
        if _ya_avisada_en_hilo(uid, channel_id, thread_ts):
            _omitir(senal["id"], MOTIVO_HILO_REPETIDA)
            continue
        persona, motivo = _persona_y_motivo(senal, ventana)
        if motivo:
            _omitir(senal["id"], motivo)
            continue
        vistos.add(uid)
        elegidas.append((senal, persona))
    if not elegidas:
        return None

    reservadas = _reservar_grupo([senal["id"] for senal, _ in elegidas])
    elegidas = [(s, p) for s, p in elegidas if s["id"] in reservadas]
    if not elegidas:
        return None
    ids = [senal["id"] for senal, _ in elegidas]

    try:
        principal = db.fetch_one(
            "select text from slack_messages where channel_id = %s and ts = %s",
            (channel_id, thread_ts),
        )
        entradas = [
            {"senal": senal, "person": persona, "permalink": _permalink(reader, senal)}
            for senal, persona in elegidas
        ]
        bloques = alerta_card.build_hilo(principal["text"] if principal else None, entradas)
        canal, ts = slack_writer.post_card(bloques, alerta_card.summary_hilo(entradas))
    except Exception as exc:
        logger.exception("alertas: no se pudo publicar la alerta del hilo %s", thread_ts)
        for senal, _ in elegidas:
            try:
                _liberar(senal)
            except Exception:
                logger.exception("alertas: no se pudo liberar la reserva de %s", senal["id"])
        _registrar(
            ACCION_FALLIDA,
            {"motivo": f"{type(exc).__name__}: {exc}", "senal_ids": ids, "hilo": thread_ts},
        )
        return False

    try:
        db.execute(
            "update message_signals set alerta_estado = 'publicada', alerta_channel_id = %s, "
            "alerta_ts = %s where id = any(%s::uuid[])",
            (canal, ts, ids),
        )
    except Exception:
        # Igual que en `_publicar_una`: reservadas y no duplicadas.
        logger.exception(
            "alertas: publicada (canal=%s, ts=%s) pero no se pudo anotar el hilo %s",
            canal,
            ts,
            thread_ts,
        )
    _registrar(
        ACCION_PUBLICADA,
        {
            "senal_ids": ids,
            "hilo": thread_ts,
            "tipo": elegidas[0][0]["tipo"],
            "canal": canal,
            "ts": ts,
        },
    )
    return True


def _unidades(pendientes: list[dict]) -> list[dict | list[dict]]:
    """Cada señal de un mensaje principal es su propia alerta; las de un mismo
    hilo van juntas, en el puesto de la primera que aparece (la de más
    confianza, o la más reciente), para que el orden de siempre se respete."""
    unidades: list[dict | list[dict]] = []
    grupos: dict[tuple[str, str], list[dict]] = {}
    for senal in pendientes:
        if senal["thread_ts"] is None:
            unidades.append(senal)
            continue
        clave = (senal["channel_id"], senal["thread_ts"])
        if clave not in grupos:
            grupos[clave] = []
            unidades.append(grupos[clave])
        grupos[clave].append(senal)
    return unidades


def _publicar(reader, ahora: datetime) -> int:
    if not db_config.value("senales_activas", True):
        return 0
    try:
        guards.check_kill_switch()
        guards.check_monthly_budget()
    except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
        _parada(exc)
        return 0

    settings = load_settings()
    if not (settings.handoff_bot_token and settings.handoff_channel_id):
        # Sin bot o sin canal no hay dónde publicar. Se comprueba ANTES del
        # detector: clasificar cuesta una llamada al LLM por mensaje y no
        # serviría de nada. Solo un log informativo, sin fila de ledger por
        # ciclo. Las señales ya detectadas caducan por edad cuando haya bot.
        logger.info("alertas sin configurar (faltan HANDOFF_SLACK_*), se omite")
        return 0

    try:
        procesar.procesar_pendientes()
    except Exception:
        # Un fallo del detector no impide publicar lo que ya estaba detectado.
        logger.exception("alertas: el detector falló; se publica lo pendiente")

    _caducar(ahora, procesar.edad_maxima_horas())
    _omitir_hilos_dudosos()

    hoy = _publicadas_desde(_inicio_del_dia(ahora))
    cupo = db_config.value("alertas_por_dia", 5) - hoy
    if cupo <= 0:
        logger.info("alertas: tope diario alcanzado")
        return 0
    ventana = ahora - timedelta(days=db_config.value("alertas_dias_misma_persona", 7))

    publicadas = 0
    for unidad in _unidades(_pendientes()):
        if publicadas >= cupo:
            break
        if isinstance(unidad, list):
            resultado = _publicar_hilo(unidad, reader, ventana)
        else:
            resultado = _publicar_suelta(unidad, reader, ventana)
        if resultado is None:
            continue
        if resultado:
            # Una alerta de hilo cuenta una vez, tenga las personas que tenga.
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
