"""The long-running worker: read Slack once per poll interval, drain the
research queue in between. Once per cycle it also runs the target-account
radar (accounts/radar.py), which only does work when an account is due, and
the decisor (accounts/decisor.py), which finds who decides each pursued
opening and queues their research, and the message signals (signals/alertas.py),
which detect what people ask for in Slack and post a light alert. Every few
minutes it also goes over the owner's one-to-one DMs (ingest/dms.py) to know
which conversations are waiting -- the timing only -- and posts one follow-up
summary a day (delivery/seguimiento.py). Right after, and only if Anthony's
switch is on, it reads the text of his DMs with leads (ingest/dm_texto.py) so
replies can be suggested; with the switch off that same step wipes the text.

A dead token or a system stop raises an operational alert and the loop keeps
running: the token may be renewed and the cap may be raised without anyone
restarting the process.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

from .. import canales, estado_lector, guards, ops_alerts
from ..accounts import decisor, radar
from ..delivery import aviso_lector, seguimiento
from ..research.worker import SYSTEM_STOPS
from ..signals import alertas, eventos
from ..slack_client import SlackAuthFailed
from . import descubrimiento, dm_texto, dms, queue
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
        # Aviso, no log: recuperar una tarea significa que alguien murió a
        # media investigación y, si en realidad seguía vivo, que puede haberse
        # pagado el research dos veces. Tiene que poder verse después.
        ops_alerts.alert(
            "tareas_recuperadas",
            f"{reclaimed} tarea(s) abandonada(s) devueltas a la cola",
        )


def _radar() -> None:
    """Una pasada del radar de cuentas objetivo, si a alguna cuenta le toca.

    Nunca tumba el ciclo: un fallo de JobSpy o de una cuenta ya queda en su
    last_scan_error (ver accounts/radar.py), y cualquier otra cosa se registra
    y se reintenta en la vuelta siguiente sin parar la cola de research. El
    kill switch no es un fallo: la cola ya avisa de la parada del sistema.
    """
    try:
        resultados = radar.escanear_pendientes()
    except guards.KillSwitchActive:
        logger.info("radar: kill switch activo, no se escanea")
        return
    except Exception:
        logger.exception("radar fallido; se reintenta en el siguiente ciclo")
        return
    for resultado in resultados:
        logger.info(
            "radar %s: %d vistas, %d nuevas, %d cerradas, %d auto-pursued%s",
            resultado.nombre,
            resultado.vistas,
            resultado.nuevas,
            resultado.cerradas,
            resultado.auto_pursued,
            f" (error: {resultado.error})" if resultado.error else "",
        )


def _decisor() -> None:
    """Una pasada del decisor sobre las vacantes pursued, protegida igual que
    el radar: un fallo se registra y el ciclo sigue con la cola de research.
    Las paradas del sistema no son un fallo; la cola ya avisa de ellas."""
    try:
        resultados = decisor.procesar_pendientes()
    except SYSTEM_STOPS as exc:
        logger.info("decisor: parada del sistema, no se busca: %s", exc)
        return
    except Exception:
        logger.exception("decisor fallido; se reintenta en el siguiente ciclo")
        return
    for resultado in resultados:
        logger.info(
            "decisor %s: %s%s",
            resultado.signal_id,
            resultado.estado,
            f" ({resultado.detalle})" if resultado.detalle else "",
        )


def _senales(reader) -> None:
    """Una pasada de las señales en mensajes: detecta y publica las alertas.

    Protegida igual que el radar y el decisor: es un extra, y un fallo aquí se
    registra y se reintenta en la vuelta siguiente sin tocar el research.
    `publicar_pendientes` ya gestiona por dentro las paradas del sistema y no
    lanza; esta guarda es la segunda línea por si algo se le escapa."""
    try:
        publicadas = alertas.publicar_pendientes(reader)
    except Exception:
        logger.exception("señales fallidas; se reintenta en el siguiente ciclo")
        return
    if publicadas:
        logger.info("señales: %d alerta(s) publicada(s)", publicadas)


def _eventos_canal(reader) -> None:
    """Menciones a AG y presentaciones en el canal de presentaciones (0030).

    No cuestan una llamada al modelo, así que van en cada vuelta junto a las señales.
    `publicar_pendientes` ya gestiona por dentro los topes y no lanza; esta guarda es la
    segunda línea."""
    try:
        avisos = eventos.publicar_pendientes(reader)
    except Exception:
        logger.exception("eventos de canal fallidos; se reintenta en la vuelta siguiente")
        return
    if avisos:
        logger.info("eventos: %d aviso(s) publicado(s)", avisos)


def _rastro_dms(reader) -> None:
    """Una pasada por los mensajes directos del dueño de la credencial, para
    saber qué conversaciones esperan respuesta (ingest/dms.py).

    Protegida igual que el radar y las señales: es un extra y un fallo aquí se
    registra y se reintenta en la pasada siguiente, sin tocar el research ni la
    entrega. La credencial caducada sí se trata, porque es lo que hay que
    avisar a quien pega el token."""
    try:
        resultado = dms.rastro_tick(reader)
    except SlackAuthFailed as exc:
        _credencial_caducada(exc)
        return
    except Exception:
        logger.exception("rastro de DMs fallido; se reintenta en la pasada siguiente")
        return
    logger.info(
        "DMs: %d vistos, %d en el rastro, %d omitidos, %d caducados",
        resultado.vistos,
        resultado.guardados,
        resultado.omitidos,
        resultado.borrados,
    )
    for error in resultado.errores:
        logger.warning("DMs: %s", error)


def _texto_dms(reader) -> None:
    """El texto de los DMs con leads (ingest/dm_texto.py, 0031), justo después del
    rastro: el rastro acaba de decir qué conversaciones se movieron.

    Corre siempre, también con el interruptor apagado, porque es esa pasada la que
    borra lo guardado al apagarlo. Protegida igual que el rastro: un fallo se
    registra y se reintenta en la pasada siguiente, y la credencial caducada se
    avisa a quien pega el token."""
    try:
        resultado = dm_texto.pasada(reader)
    except SlackAuthFailed as exc:
        _credencial_caducada(exc)
        return
    except Exception:
        logger.exception("texto de DMs fallido; se reintenta en la pasada siguiente")
        return
    logger.info(
        "texto de DMs: %d conversaciones, %d mensajes nuevos, %d omitidos, "
        "%d caducados, %d borrados por el interruptor",
        resultado.conversaciones,
        resultado.guardados,
        resultado.omitidos,
        resultado.purgados,
        resultado.borrados,
    )
    for error in resultado.errores:
        logger.warning("texto de DMs: %s", error)


def _descubrir_canales(reader) -> None:
    """Le pregunta a Slack qué canales hay y en cuáles está la cuenta lectora.

    Solo corre en el modo automático (0028). Es `conversations.list`, de solo lectura y
    ya en la lista blanca del lector. Protegido como el radar y las señales: si falla, se
    sigue con lo que la tabla ya sabe -- los canales descubiertos antes no se olvidan --
    y se reintenta en el próximo descubrimiento. La credencial caducada sí se trata,
    porque es lo que hay que avisarle a quien pega el token."""
    from ..config import load_settings

    try:
        vistos = reader.canales()
    except SlackAuthFailed as exc:
        _credencial_caducada(exc)
        return
    except Exception:
        logger.exception("no se pudieron descubrir los canales; se sigue con los conocidos")
        return
    try:
        conteo = descubrimiento.guardar_descubrimiento(vistos, load_settings().handoff_channel_id)
    except Exception:
        logger.exception("no se pudo guardar el descubrimiento de canales")
        return
    logger.info(
        "canales: %d vistos en el workspace, %d con la cuenta dentro",
        conteo["vistos"],
        conteo["dentro"],
    )


def _seguimiento() -> None:
    """El resumen diario de las conversaciones que se enfrían. `publicar_resumen`
    ya gestiona por dentro los frenos y no lanza; esta guarda es la segunda
    línea, por si algo se le escapa."""
    try:
        lineas = seguimiento.publicar_resumen()
    except Exception:
        logger.exception("seguimiento fallido; se reintenta en la pasada siguiente")
        return
    if lineas:
        logger.info("seguimiento: resumen con %d línea(s)", lineas)


def _avisos_seguimiento() -> None:
    """Los avisos inmediatos de las conversaciones que ya pasaron de su plazo (0029).

    Van en la misma pasada que el rastro de los DMs, justo después: el rastro acaba de
    actualizar quién habló último, así que esto decide sobre datos recién traídos.
    `publicar_avisos` ya gestiona por dentro el cupo diario y los frenos y no lanza;
    esta guarda es la segunda línea."""
    try:
        avisos = seguimiento.publicar_avisos()
    except Exception:
        logger.exception("avisos de seguimiento fallidos; se reintenta en la pasada siguiente")
        return
    if avisos:
        logger.info("seguimiento: %d aviso(s) publicado(s)", avisos)


def _credencial_caducada(exc: SlackAuthFailed) -> None:
    """La alerta operativa de siempre, más un aviso en el Slack de Handoff (con
    el bot, que tiene otro token) para que se entere quien no mira el panel. El
    aviso se frena solo (aviso_token_horas) y nunca lanza; la guarda es por si
    acaso: avisar no puede empeorar un fallo que ya hay."""
    ops_alerts.alert("slack_auth", str(exc))
    try:
        aviso_lector.avisar_credencial_caducada()
    except Exception:
        logger.exception("no se pudo avisar de la credencial caducada")


def run_loop(
    reader,
    *,
    channels: Sequence[str] | Callable[[], Sequence[str]],
    lookback_hours: float,
    poll_seconds: int,
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_cycles: int | None = None,
) -> int:
    next_watch = clock()
    # El rastro de los DMs va a su propio ritmo (ingest/dms.py): una
    # conversación que se enfría no tiene prisa, y cada pasada habla con Slack
    # una vez por conversación.
    next_rastro = clock()
    # El descubrimiento de canales va a su propio ritmo y solo en modo automático: la
    # lista de canales de un workspace no cambia cada dos minutos.
    next_descubrimiento = clock()
    cycles = 0
    failing = False
    try:
        _reclaim_stale()
    except Exception:
        # Postgres puede no estar listo aún cuando arranca el worker; no
        # arrancar por eso sería peor. El primer ciclo lo reintenta y avisa.
        logger.exception("no se pudieron recuperar las tareas abandonadas al arrancar")
    while not should_stop():
        try:
            if clock() >= next_watch:
                next_watch = clock() + poll_seconds
                _reclaim_stale()
                try:
                    # Modo automático (0028): los canales salen de la tabla y cada uno
                    # lleva su ritmo, así que la lista de esta vuelta son los que les
                    # tocaba, no todos. Apagado, el camino de siempre: la lista del
                    # panel, resuelta en cada sondeo porque es un callable.
                    rotando = canales.modo_todos()
                    if rotando:
                        if clock() >= next_descubrimiento:
                            next_descubrimiento = (
                                clock() + canales.MINUTOS_ENTRE_DESCUBRIMIENTOS * 60
                            )
                            _descubrir_canales(reader)
                        current = canales.por_sondear()
                    else:
                        current = list(channels() if callable(channels) else channels)
                    try:
                        tick = watch_tick(reader, current, lookback_hours, rotando=rotando)
                    except Exception as exc:
                        # El latido va aparte del aviso: lo lee el panel, no el equipo
                        # de operaciones. Se vuelve a lanzar tal cual.
                        estado_lector.registrar_fallo(exc)
                        raise
                    estado_lector.registrar_ok(tick.user_id, tick.team, tick.channels_read)
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
                    _credencial_caducada(exc)

            if clock() >= next_rastro:
                next_rastro = clock() + dms.MINUTOS_ENTRE_PASADAS * 60
                _rastro_dms(reader)
                _texto_dms(reader)
                _avisos_seguimiento()
                _seguimiento()

            _radar()
            _decisor()
            _senales(reader)
            _eventos_canal(reader)

            try:
                result = run_next_job(reader)
            except SYSTEM_STOPS as exc:
                ops_alerts.alert("parada_del_sistema", str(exc))
                sleep(SYSTEM_STOP_PAUSE_SECONDS)
            except SlackAuthFailed as exc:
                _credencial_caducada(exc)
                sleep(IDLE_SECONDS)
            else:
                if result is None or result.status == "limitado":
                    sleep(IDLE_SECONDS)
                elif result.status != "hecho":
                    logger.info(
                        "research %s: %s %s", result.slack_user_id, result.status, result.detail
                    )
        except Exception as exc:
            # Un parpadeo de Postgres o de la red tumba cualquiera de las
            # llamadas de arriba. Morir aquí deja la tarea en_curso y a esa
            # persona bloqueada hasta el siguiente reclaim: mejor avisar una vez
            # por racha y reintentar despacio.
            # El precio de que la guarda sea tan ancha: un error de programación
            # (un TypeError tras un refactor) también se queda reintentando en
            # silencio. Por eso cada vuelta fallida escribe la traza entera.
            if not failing:
                ops_alerts.alert("ciclo_fallido", f"{type(exc).__name__}: {exc}")
                failing = True
            logger.exception("ciclo del worker fallido; se reintenta")
            sleep(IDLE_SECONDS)
        else:
            failing = False

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
    return cycles
