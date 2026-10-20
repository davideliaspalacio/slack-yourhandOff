"""Hourly read of the Founders Club Slack: new messages and new members.

No backfill, on purpose: the first read of a channel looks back
SLACK_LOOKBACK_HOURS only, and the first member snapshot is a baseline.
Otherwise the first run would queue research on all 1,400 members, which was
ruled out. Later reads start right after the last stored top-level message.

With `leer_hilos` on (0032) it also reads thread replies, which
`conversations.history` never returns. Same rule: anything older than the last
24 hours is stored as context only (`archivado`), never as something new.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg

from .. import canales, db, db_config
from ..slack_client import SlackUnavailable
from . import descubrimiento, queue

logger = logging.getLogger(__name__)

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


# Hilos (0032). Una respuesta más vieja que esto se guarda como `archivado`: contexto
# para entender el hilo, nunca research ni aviso. Son las 24 horas que mira el detector
# de señales. Sin este corte, encender `leer_hilos` trataría semanas de conversaciones
# como nuevas, que es justo la inundación que ya pasó una vez.
HORAS_NUEVO = 24
# Cada cuánto se barre un canal buscando respuestas nuevas en hilos viejos. El barrido es
# un `conversations.history` de `hilos_dias` días: varias páginas en un canal movido.
MINUTOS_ENTRE_BARRIDOS = 30
HILOS_DIAS_DEFECTO = 7
HILOS_POR_VUELTA_DEFECTO = 20


@dataclass
class TickResult:
    messages_new: int = 0
    messages_ignored: int = 0
    members_new: int = 0
    user_id: str = ""
    team: str = ""
    channels_read: int = 0
    # Hilos (0032): cuántos se pidieron a Slack y cuántas respuestas `nuevo` trajeron.
    threads_read: int = 0
    replies_new: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Candidato:
    """Un hilo con respuestas que aún no se han traído."""

    channel: str
    thread_ts: str
    latest_reply: str
    # La última respuesta ya traída (`hilos.ultima_respuesta`); None = ninguna.
    marcador: str | None
    # Si salió del barrido de hilos viejos y no de la página leída en esta vuelta.
    del_barrido: bool


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
    """Desde dónde sigue la lectura del canal: el último mensaje PRINCIPAL guardado.

    Solo principales (`thread_ts` nulo o igual a su `ts`), nunca respuestas. Desde 0032 se
    guardan respuestas de hilos, y una respuesta reciente a un hilo viejo subiría el
    marcador por encima de mensajes principales que aún no se han leído: la lectura
    siguiente empezaría después de ellos y se perderían sin error y para siempre. Vale
    también con `leer_hilos` apagado: las respuestas guardadas siguen ahí al apagarlo.
    """
    row = db.fetch_one(
        """
        select ts from slack_messages
        where channel_id = %s and (thread_ts is null or thread_ts = ts)
        order by ts::numeric desc
        limit 1
        """,
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


def _refrescar_nombre(reader, channel: str) -> None:
    """Guarda el nombre del canal si no se conoce o lleva más de un día sin mirarse.

    Es un adorno: sirve para que el panel y el MCP digan `#founders-general` en vez de
    `C08MRLP46G6`. Por eso ni que Slack no conteste, ni que el canal sea privado, ni que
    la base falle puede tumbar la lectura. La credencial muerta sí sale (SlackAuthFailed):
    la gestiona el bucle, que avisa a quien pega el token, y sin ella no se va a leer nada
    de todas formas.
    """
    try:
        if not canales.necesita_nombre(channel):
            return
        try:
            nombre = reader.channel_name(channel) or None
        except SlackUnavailable as exc:
            # Se anota el intento igual (sin nombre, o conservando el que hubiera): así
            # no se le pregunta a Slack en cada sondeo por un canal que no contesta, y
            # se vuelve a intentar al día siguiente.
            nombre = None
            logger.warning("sin nombre para el canal %s (%s); se sigue con el código", channel, exc)
        db.execute(
            """
            insert into canales (channel_id, nombre, actualizado_at)
            values (%s, %s, now())
            on conflict (channel_id) do update
                set nombre = coalesce(excluded.nombre, canales.nombre),
                    actualizado_at = excluded.actualizado_at
            """,
            (channel, nombre),
        )
    except psycopg.Error:
        logger.warning("no se pudo guardar el nombre del canal %s", channel)


def _diff_members(reader, channel: str, owner_id: str, result: TickResult) -> None:
    current = reader.members(channel)
    previous = db.fetch_one(
        "select members from member_snapshots where channel_id = %s order by taken_at desc limit 1",
        (channel,),
    )
    if previous is not None:  # sin línea base no se encola a todo el padrón
        for user_id in sorted(current - set(previous["members"]) - {owner_id}):
            if queue.enqueue(user_id, "miembro_nuevo"):
                result.members_new += 1
    # La instantánea se guarda al final, nunca antes de encolar. Si esto se
    # interrumpe a media lista, la instantánea vieja sigue siendo la base y la
    # vuelta siguiente ve otra vez a los que faltaban; guardarla primero los
    # borraba del diff para siempre.
    # Reencolar a quien ya se encoló sale gratis solo mientras su tarea siga
    # abierta: el ON CONFLICT de enqueue no mira las terminadas. Si el reintento
    # llega con la investigación ya hecha se crea una segunda tarea, y lo que
    # evita pagarla es la frescura de research_person, que no cubre ni un
    # dossier degradado ni a alguien en estado incompleto.
    db.execute(
        "insert into member_snapshots (channel_id, members) values (%s, %s)",
        (channel, sorted(current)),
    )


def _marcadores(channel: str, thread_tss: list[str]) -> dict[str, str | None]:
    """{thread_ts: ultima_respuesta} de los hilos de ese canal que ya tienen fila."""
    if not thread_tss:
        return {}
    filas = db.fetch_all(
        "select thread_ts, ultima_respuesta from hilos "
        "where channel_id = %s and thread_ts = any(%s)",
        (channel, thread_tss),
    )
    return {f["thread_ts"]: f["ultima_respuesta"] for f in filas}


def _con_respuestas_nuevas(
    channel: str, mensajes: list[dict], del_barrido: bool
) -> list[_Candidato]:
    """Los principales cuya `latest_reply` es más nueva que su marcador (o sin marcador).

    `conversations.history` ya trae en cada principal cuántas respuestas tiene y cuál es
    la última: con eso se sabe qué hilos pedir sin preguntarle a Slack por ninguno más.
    """
    padres = [
        m
        for m in mensajes
        if m.get("ts") and m.get("latest_reply") and int(m.get("reply_count") or 0) > 0
    ]
    marcadores = _marcadores(channel, [m["ts"] for m in padres])
    candidatos = []
    for m in padres:
        marcador = marcadores.get(m["ts"])
        if marcador is None or float(m["latest_reply"]) > float(marcador):
            candidatos.append(
                _Candidato(channel, m["ts"], m["latest_reply"], marcador, del_barrido)
            )
    return candidatos


def _toca_barrer(channel: str, now: Callable[[], float]) -> bool:
    """Si toca barrer el canal: nunca se barrió, o hace más de media hora.

    Sin fila en `canales` no se barre: no hay dónde apuntar el barrido, y repetirlo en
    cada vuelta sería un historial de una semana cada dos minutos."""
    fila = db.fetch_one("select hilos_barrido_at from canales where channel_id = %s", (channel,))
    if fila is None:
        return False
    hecho = fila["hilos_barrido_at"]
    return hecho is None or hecho.timestamp() < now() - MINUTOS_ENTRE_BARRIDOS * 60


def _candidatos_del_canal(
    reader,
    channel: str,
    history: list[dict],
    lookback_hours: float,
    dias: int,
    now: Callable[[], float],
    barridos: set[str],
) -> list[_Candidato]:
    """Los hilos de este canal con respuestas por traer: los de la página recién leída y,
    si toca, los del barrido de los últimos `hilos_dias` días."""
    candidatos = _con_respuestas_nuevas(channel, history, del_barrido=False)
    if dias <= 0 or not _toca_barrer(channel, now):
        return candidatos
    # El barrido SOLO sirve para descubrir hilos viejos con respuestas nuevas. Sus
    # mensajes principales no se guardan nunca: guardarlos como `nuevo` haría que el
    # resolver investigara a quien escribió hace una semana, que es exactamente el
    # histórico que se prohibió.
    viejos = reader.history(channel, oldest=f"{now() - dias * 86400:.6f}")
    # Un principal más nuevo que el marcador de lectura todavía no lo ha visto la lectura
    # normal (llegó entre las dos llamadas): se deja para ella, que lo guardará como
    # `nuevo` y lo traerá aquí en la vuelta siguiente. Si se guardara ahora desde el hilo,
    # la lectura normal lo encontraría ya guardado y no lo contaría nunca.
    tope = float(_oldest(channel, lookback_hours, now))
    ya = {c.thread_ts for c in candidatos}
    viejos = [m for m in viejos if m.get("ts") and float(m["ts"]) <= tope and m["ts"] not in ya]
    barridos.add(channel)
    return candidatos + _con_respuestas_nuevas(channel, viejos, del_barrido=True)


def _apuntar_barrido(channel: str, now: Callable[[], float]) -> None:
    # Con el reloj de la vuelta (`now`), el mismo con el que se decide si toca barrer.
    db.execute(
        "update canales set hilos_barrido_at = to_timestamp(%s) where channel_id = %s",
        (now(), channel),
    )


def _traer_hilo(reader, candidato: _Candidato, owner_id: str, now: Callable[[], float]) -> int:
    """Guarda el principal (si falta) y las respuestas nuevas de un hilo, y sube su
    marcador. Devuelve cuántas respuestas se guardaron como `nuevo`."""
    channel, thread_ts = candidato.channel, candidato.thread_ts
    mensajes = reader.respuestas(channel, thread_ts, oldest=candidato.marcador)
    corte = now() - HORAS_NUEVO * 3600
    # El marcador sube como mínimo hasta la `latest_reply` que anunció el historial: si
    # esa respuesta se borró, Slack ya no la devuelve y el hilo se pediría en cada vuelta
    # para siempre. No se salta nada: Slack la anunció, así que lo anterior ya existía y
    # viene en esta misma respuesta.
    marcador = candidato.latest_reply
    if candidato.marcador is not None and float(candidato.marcador) > float(marcador):
        marcador = candidato.marcador
    nuevas = 0
    # De más viejo a más nuevo y cada fila por separado, como en la lectura normal. El
    # marcador del hilo se apunta al final, con todo ya guardado: si esto se corta a
    # medias, la vuelta siguiente pide el hilo otra vez desde el marcador viejo y lo ya
    # guardado choca con el ON CONFLICT sin duplicarse.
    for mensaje in sorted(mensajes, key=lambda m: float(m.get("ts") or 0)):
        ts = mensaje.get("ts")
        if not ts:
            continue
        principal = ts == thread_ts
        if (
            not principal
            and candidato.marcador is not None
            and float(ts) <= float(candidato.marcador)
        ):
            continue  # ya traída en una vuelta anterior
        status = classify(mensaje, owner_id)
        # El principal que falta en la tabla es por fuerza anterior a la ventana de
        # lectura: los de la página leída ya están guardados, y los del barrido son todos
        # más viejos que el marcador de lectura. Es contexto aunque sea de hace unas horas:
        # como `nuevo`, el resolver investigaría a quien la lectura dejó fuera adrede.
        # Si ya estaba guardado, el ON CONFLICT lo deja como estaba.
        if status == "nuevo" and (principal or float(ts) < corte):
            status = "archivado"
        guardado = _store(channel, {**mensaje, "thread_ts": thread_ts}, status)
        if not principal:
            if guardado and status == "nuevo":
                nuevas += 1
            if float(ts) > float(marcador):
                marcador = ts
    # Nunca hacia atrás: el CASE se queda con el mayor de los dos.
    db.execute(
        """
        insert into hilos (channel_id, thread_ts, ultima_respuesta, actualizado_at)
        values (%s, %s, %s, now())
        on conflict (channel_id, thread_ts) do update
            set ultima_respuesta = case
                    when hilos.ultima_respuesta is null
                      or excluded.ultima_respuesta::numeric > hilos.ultima_respuesta::numeric
                    then excluded.ultima_respuesta
                    else hilos.ultima_respuesta
                end,
                actualizado_at = excluded.actualizado_at
        """,
        (channel, thread_ts, marcador),
    )
    return nuevas


def _leer_hilos(
    reader,
    candidatos: list[_Candidato],
    barridos: set[str],
    por_vuelta: int,
    owner_id: str,
    now: Callable[[], float],
    result: TickResult,
) -> None:
    """Trae los hilos de esta vuelta, como mucho `por_vuelta`, los más movidos primero.

    Los que no entran esperan a la vuelta siguiente, y para eso hay que volver a barrer
    su canal: la página de la vuelta siguiente solo trae principales más nuevos que el
    marcador de lectura, así que un hilo que se quedó fuera no volvería a salir hasta el
    próximo barrido, media hora después. Por eso su canal se queda con el barrido sin
    apuntar (y, si ya estaba apuntado, se borra), y el canal que entró entero lo apunta.
    """
    orden = sorted(candidatos, key=lambda c: float(c.latest_reply), reverse=True)
    elegidos, sobran = orden[:por_vuelta], orden[por_vuelta:]
    pendientes = {c.channel for c in sobran}
    for channel in sorted(barridos - pendientes):
        _apuntar_barrido(channel, now)
    for channel in sorted(pendientes):
        db.execute("update canales set hilos_barrido_at = null where channel_id = %s", (channel,))
    for candidato in elegidos:
        try:
            result.replies_new += _traer_hilo(reader, candidato, owner_id, now)
            result.threads_read += 1
        except (SlackUnavailable, psycopg.Error) as exc:
            # Un hilo que falla no para a los demás. Su marcador no se ha movido, así que
            # se vuelve a pedir en cuanto vuelva a salir. La credencial muerta
            # (SlackAuthFailed) no entra aquí: sube, como en la lectura normal.
            result.errors.append(f"{candidato.channel} thread {candidato.thread_ts}: {exc}")


def watch_tick(
    reader,
    channels: list[str],
    lookback_hours: float,
    now: Callable[[], float] = time.time,
    rotando: bool = False,
) -> TickResult:
    """Read every channel once. SlackAuthFailed propagates: it needs a person.

    `rotando` es el modo automático (`canales.modo_todos()`, migración 0028): la lista
    que llega no son todos los canales vigilados sino los que les tocaba, y hay que
    apuntar el resultado para que a cada uno le toque otra vez cuando corresponda. Trae
    además la lista de miembros a su propio ritmo, mucho más lento, porque es la llamada
    más cara y un miembro nuevo no tiene prisa.

    Apagado -- el camino de siempre -- nada de esto corre: se leen todos los canales de
    la lista y se revisan sus miembros en cada vuelta, exactamente como antes.
    """
    result = TickResult()
    owner_id, result.team = reader.identity()
    result.user_id = owner_id
    # Hilos (0032): apagado por omisión, y apagado no se llama a conversations.replies ni
    # se barre nada. Un tope de cero también lo apaga.
    por_vuelta = db_config.value("hilos_por_vuelta", HILOS_POR_VUELTA_DEFECTO)
    leer_hilos = db_config.value("leer_hilos", False) is True and por_vuelta > 0
    dias = db_config.value("hilos_dias", HILOS_DIAS_DEFECTO) if leer_hilos else 0
    candidatos: list[_Candidato] = []
    barridos: set[str] = set()
    for channel in channels:
        nuevos_aqui = 0
        try:
            _refrescar_nombre(reader, channel)
            history = reader.history(channel, oldest=_oldest(channel, lookback_hours, now))
            # De más viejo a más nuevo, a propósito. El marcador de reanudación
            # es el ts más alto ya guardado y cada mensaje se confirma por
            # separado, así que una interrupción a media tanda tiene que dejar
            # guardado un prefijo por abajo. Slack devuelve primero lo más
            # nuevo: guardarlo en ese orden subiría el marcador y dejaría los
            # mensajes viejos de esa misma tanda fuera de todas las lecturas
            # futuras, sin error y para siempre.
            for message in sorted(history, key=lambda m: float(m["ts"])):
                status = classify(message, owner_id)
                if _store(channel, message, status):
                    if status == "nuevo":
                        result.messages_new += 1
                        nuevos_aqui += 1
                    else:
                        result.messages_ignored += 1
            if not rotando or canales.toca_miembros(channel):
                _diff_members(reader, channel, owner_id, result)
                if rotando:
                    descubrimiento.marcar_miembros(channel)
            result.channels_read += 1
            if leer_hilos:
                # Después de guardar la página: el barrido necesita el marcador de
                # lectura ya al día para no adelantarse a la lectura normal. Si el
                # barrido falla, el canal ya está leído y el error se apunta abajo.
                candidatos.extend(
                    _candidatos_del_canal(
                        reader, channel, history, lookback_hours, dias, now, barridos
                    )
                )
        except SlackUnavailable as exc:
            result.errors.append(f"{channel}: {exc}")
        finally:
            # Se apunta pase lo que pase: si este canal falló, su escalón sube igual y
            # no se queda atascado al principio de la cola pidiendo turno en cada vuelta
            # mientras los demás esperan.
            if rotando:
                try:
                    descubrimiento.marcar_sondeado(channel, nuevos_aqui)
                except psycopg.Error:
                    logger.warning("no se pudo apuntar el sondeo del canal %s", channel)
    if leer_hilos:
        # Al final y para todos los canales a la vez: el tope es por vuelta, y así se
        # reparte entre los hilos más movidos de cualquier canal, no entre los primeros.
        _leer_hilos(reader, candidatos, barridos, por_vuelta, owner_id, now, result)
    return result
