"""Contestar en el Founders Club desde el panel: el permiso, el rastro y el envío.

El único camino por el que un mensaje sale hacia ese workspace. `tools/respondedor.py`
sabe mandarlo y aplica sus cinco frenos; aquí se decide QUIÉN puede pedirlo y se deja
constancia de quién lo pidió, porque en Slack el mensaje va a llevar el nombre de
Anthony y no el de quien lo escribió.

Dos cosas gobiernan este módulo:

1. **Ser del panel no basta.** Ver el chat lo puede todo el equipo; escribir en él lo
   puede quien tenga `panel_users.puede_responder`, que hoy es una sola persona (0034, y
   la columna nace en false para todos). Se comprueba aquí, en el worker, no en el panel:
   el panel es una pantalla y lo que decide es esto.
2. **La fila antes del envío.** La fila de `mensajes_enviados` se escribe en `pendiente`
   ANTES de llamar a Slack, como la marca del resumen diario (`delivery/digest.py`): si
   el proceso se muere entre el envío y la actualización, queda una fila `pendiente` que
   alguien puede mirar, y nunca un mensaje publicado del que no haya rastro. Al revés
   (mandar y luego anotar) el único fallo posible es el que no se puede permitir.

`enviar_desde_panel` no lanza NUNCA. Quien llama es un endpoint HTTP contestando a una
persona que está mirando una pantalla: devuelve un diccionario con `ok` y, si no, un
`error` en inglés que se le puede enseñar tal cual. Un rechazo queda como fila `fallido`
con su motivo, que es información útil (se ve quién intentó escribir a dónde) y no ruido.

Nada de esto se dispara solo: `texto` es obligatorio y lo teclea una persona. No hay
plantillas, no hay modelo y no hay ningún camino automático hacia el envío.
"""

from __future__ import annotations

import logging

from . import db
from .guards import KillSwitchActive
from .slack_client import SlackAuthFailed, SlackUnavailable
from .tools import respondedor
from .tools.respondedor import EnvioRechazado, Respondedor

logger = logging.getLogger(__name__)

# Lo que se le dice a quien no puede escribir. El mismo texto para "no estás en el panel"
# y para "estás pero sin permiso": quien mira esta pantalla no necesita la diferencia, y
# el caso real es siempre el segundo.
SIN_PERMISO = (
    "You are not allowed to send messages as Anthony. Ask David to turn on your permission."
)


def _puede_responder(email: str) -> bool:
    """Si ese correo tiene el permiso. `panel_users.email` está siempre en minúsculas
    (check de 0006), así que se compara en minúsculas."""
    fila = db.fetch_one(
        "select puede_responder from panel_users where email = %s", (email.lower(),)
    )
    return bool(fila and fila["puede_responder"])


def _anotar(autor_email: str, destino: str, texto: str, thread_ts: str | None) -> str:
    """La fila `pendiente`, antes de que Slack sepa nada. Devuelve su id."""
    fila = db.fetch_one(
        """
        insert into mensajes_enviados (destino, thread_ts, autor_email, texto, estado)
             values (%s, %s, %s, %s, 'pendiente')
          returning id
        """,
        (destino, thread_ts or None, autor_email, texto),
    )
    return str(fila["id"])


def _cerrar(id_fila: str, estado: str, ts: str | None, motivo: str | None) -> None:
    """Deja la fila en su estado final. Un fallo al cerrarla no puede tumbar nada: el
    mensaje ya salió (o ya se rechazó) y la fila `pendiente` que quedaría es visible."""
    try:
        db.execute(
            "update mensajes_enviados set estado = %s, enviado_ts = %s, motivo = %s where id = %s",
            (estado, ts, motivo, id_fila),
        )
    except Exception:  # la constancia nunca decide el resultado
        logger.exception("chat: no se pudo cerrar la fila %s como %s", id_fila, estado)


def _error_legible(exc: Exception) -> str:
    """El mensaje para la pantalla, en inglés y sin nada sensible dentro.

    Los rechazos propios (`EnvioRechazado`) ya se escribieron para leerse: se pasan tal
    cual. Los de Slack no: el de autenticación va en español y habla del panel, y el de
    disponibilidad trae el código de Slack, que sirve para depurar pero no explica nada a
    quien quería contestar un mensaje. Ninguno de los dos lleva el token (ver
    `slack_client`), pero se traducen igualmente."""
    if isinstance(exc, EnvioRechazado):
        return str(exc)
    if isinstance(exc, KillSwitchActive):
        return "Everything is stopped: the kill switch is on."
    if isinstance(exc, SlackAuthFailed):
        return (
            "Slack rejected the stored credential. Paste a fresh one in Settings, then try again."
        )
    if isinstance(exc, SlackUnavailable):
        return f"Slack did not accept the message ({exc}). Nothing was sent; try again."
    return "Something went wrong and the message was not sent."


def enviar_desde_panel(
    autor_email: str,
    destino: str,
    texto: str,
    thread_ts: str | None = None,
) -> dict:
    """Manda `texto` al Founders Club como Anthony, de parte de `autor_email`.

    `{"ok": True, "ts": ..., "id": ...}` si salió; `{"ok": False, "error": ...}` si no,
    con el error ya en inglés. Nunca lanza.

    `autor_email` lo resuelve quien llama a partir de la sesión de la persona (ver
    `web/app.py`), nunca del formulario: es lo que queda en la fila de auditoría como
    autor de verdad, y tiene que ser quien de verdad pulsó el botón.
    """
    correo = (autor_email or "").strip().lower()
    if not correo:
        return {"ok": False, "error": SIN_PERMISO}

    try:
        permitido = _puede_responder(correo)
    except Exception:  # sin poder comprobar el permiso no se manda nada
        logger.exception("chat: no se pudo comprobar el permiso de %s", correo)
        return {"ok": False, "error": "Could not check your permission. Try again."}
    if not permitido:
        logger.warning("chat: %s intentó enviar sin puede_responder", correo)
        return {"ok": False, "error": SIN_PERMISO}

    destino_limpio = (destino or "").strip().upper()
    hilo = (thread_ts or "").strip() or None

    # La FORMA del destino se mira antes de anotar, no porque la autorización viva aquí
    # (eso es del respondedor), sino porque el check de `mensajes_enviados.destino` (0034)
    # rechazaría la fila: sin esto, una errata saldría como "no se pudo anotar" con su
    # traza en el log en vez de decir lo que pasa. Un id así no puede existir en la tabla,
    # así que no quedarse sin fila es justo lo que la base ya decidió.
    if not respondedor.DESTINO.match(destino_limpio):
        logger.warning("chat: %s probó un destino con otra forma", correo)
        return {
            "ok": False,
            "error": "That is not a channel or conversation you can reply to.",
        }

    try:
        id_fila = _anotar(correo, destino_limpio, texto or "", hilo)
    except Exception:  # sin rastro no se manda: es la regla de 0034
        # Si la base no acepta la fila (un destino con una forma que su check rechaza,
        # por ejemplo) no se llama a Slack: antes no mandar que mandar a ciegas.
        logger.exception("chat: no se pudo anotar el envío de %s a %s", correo, destino_limpio)
        return {"ok": False, "error": "Could not record the message, so nothing was sent."}

    try:
        ts = Respondedor().enviar(destino_limpio, texto or "", hilo)
    except Exception as exc:  # nada de esto puede subir al endpoint
        motivo = f"{type(exc).__name__}: {exc}"
        if not isinstance(
            exc, EnvioRechazado | KillSwitchActive | SlackAuthFailed | SlackUnavailable
        ):
            # Algo que no se previó. Se registra entero en el log, y la fila se queda
            # igualmente como fallido: lo que no puede pasar es que parezca enviado.
            logger.exception("chat: fallo inesperado enviando de %s a %s", correo, destino_limpio)
        else:
            logger.warning("chat: envío rechazado (%s)", motivo)
        _cerrar(id_fila, "fallido", None, motivo)
        return {"ok": False, "error": _error_legible(exc), "id": id_fila}

    _cerrar(id_fila, "enviado", ts, None)
    logger.info("chat: %s envió a %s como Anthony", correo, destino_limpio)
    return {"ok": True, "ts": ts, "id": id_fila}


# Reexportado para que quien orqueste (el endpoint, los tests) pueda nombrar los topes sin
# importar dos módulos.
CLAVE_ACTIVO = respondedor.CLAVE_ACTIVO
CLAVE_TOPE = respondedor.CLAVE_TOPE
