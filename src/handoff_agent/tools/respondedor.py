"""Mandar UN mensaje al Founders Club como Anthony. Lo único que escribe allí.

Hasta la migración 0034 el agente no podía escribir una sola palabra en ese workspace, y
eso lo sostenían cuatro capas: la lista blanca `READ_METHODS` del lector, la lista negra
`JAMAS` de su test, un test que intenta escribir y comprueba que falla, y que lo que el
agente publica va por otro bot a otro workspace. El equipo necesita poder contestar
cuando AG no está, Anthony lo autorizó (comunicado el 2026-11-09) y para una sola
persona. Esta clase es esa puerta, y nada más que esa puerta.

Por qué es una clase aparte y no un método nuevo del lector:

* `FoundersClubReader` sigue siendo de solo lectura PARA SIEMPRE. No se le añade nada,
  no se hereda de él y no se le pasa este cliente. Su lista blanca, su lista negra y sus
  tests se quedan exactamente como estaban: lo comprueba `tests/test_slack_client.py`.
* La superficie de escritura tiene que poder leerse de un vistazo. Aquí hay un método de
  Slack y uno solo (`WRITE_METHODS`), y `_call` lo impone en tiempo de ejecución igual
  que hace el lector con los suyos: ni reaccionar, ni editar, ni borrar, ni unirse a un
  canal, ni abrir una conversación nueva. Añadir un método no es un descuido posible.

Los mensajes salen con el nombre de Anthony, así que cada freno de aquí existe porque las
palabras de otra persona van a llevar el suyo:

* `chat_responder_activo` apagado: no se manda nada. Nace apagado (0034).
* El kill switch del sistema para esto como para todo lo demás.
* Tope diario (`chat_mensajes_por_dia`), para que un error no se vuelva una ráfaga.
* **Solo donde ya hay conversación**: un canal que ya vigilamos, o un DM que ya existe en
  `dm_rastro`. Nunca en ningún otro sitio. Esto es lo que impide abrir una conversación
  nueva con un desconocido o escribir en un canal cualquiera del club.
* El texto lo escribe una persona, siempre. No hay plantillas, no hay modelo y no hay un
  solo camino que mande algo que nadie haya tecleado: `enviar` exige el texto y lo
  rechaza vacío.

Nada de esto se manda solo: quien orquesta es `chat_envio.enviar_desde_panel`, que deja
constancia en `mensajes_enviados` ANTES de llamar aquí.
"""

from __future__ import annotations

import http.client
import logging
import re
from datetime import UTC, datetime

from slack_sdk.errors import SlackApiError

from .. import canales, db, db_config, guards, secretos

# El mismo corte de día que usa el resto del sistema para sus topes (en `zona_horaria`,
# no en UTC). Se importa en vez de copiarse para que "hoy" signifique exactamente lo
# mismo aquí que en el resumen de seguimiento y en las alertas.
from ..delivery.seguimiento import _inicio_del_dia

# Se reutilizan los tipos de error y el constructor del cliente del lector, no su clase:
# `_web_client` es la ÚNICA pieza que sabe que un token de sesión (xoxc-) solo vale con la
# cabecera de la cookie `d`, y duplicar esa regla aquí sería tener dos sitios donde
# equivocarse. Importar esto no le da al lector ninguna capacidad de escritura.
from ..slack_client import AUTH_ERRORS, SlackAuthFailed, SlackUnavailable, _web_client

logger = logging.getLogger(__name__)

# El único método de Slack que esta clase puede llamar. La lista vive aquí, y no solo en
# el test que la vigila, para que `_call` la imponga en ejecución. Es deliberadamente de
# un solo elemento: la forma espeja la del lector (`READ_METHODS`) para que cualquiera que
# conozca una entienda la otra.
WRITE_METHODS = frozenset({"chat_postMessage"})

CLAVE_ACTIVO = "chat_responder_activo"
CLAVE_TOPE = "chat_mensajes_por_dia"
TOPE_POR_DEFECTO = 50

# El formato de un destino, el mismo que valida la base (`mensajes_enviados.destino`, en
# 0034): un canal (C…/G…) o una conversación directa (D…). Cualquier otra cosa se rechaza
# antes de preguntarle nada a la base.
DESTINO = re.compile(r"^[CGD][A-Z0-9]+$")

# Largo máximo del texto. Slack acepta bastante más, pero un mensaje de más de esto no es
# una respuesta en un chat: es un error de quien pega algo, y sale con el nombre de
# Anthony. Se prefiere rechazarlo y que la persona lo parta.
MAX_TEXTO = 4000


class EnvioRechazado(RuntimeError):
    """Algo impide mandar este mensaje. Nunca llegó a Slack.

    Su texto se le enseña a la persona del panel, así que va en inglés como todo lo que
    sale de aquí hacia fuera."""


class RespuestasApagadas(EnvioRechazado):
    """El interruptor `chat_responder_activo` está en false."""


class TopeDiarioAlcanzado(EnvioRechazado):
    """Hoy ya se intentaron tantos mensajes como permite `chat_mensajes_por_dia`."""


class DestinoNoPermitido(EnvioRechazado):
    """Ese canal no se vigila, o ese DM no existe todavía. No se abre nada nuevo."""


class TextoInvalido(EnvioRechazado):
    """Vacío, o tan largo que no es una respuesta."""


def _activo() -> bool:
    """Apagado salvo que la base diga true, como nace en 0034: que el panel pueda
    escribir no puede ser el estado por omisión de nada."""
    return db_config.value(CLAVE_ACTIVO, False) is True


def _comprobar_texto(texto: str) -> None:
    if not texto.strip():
        raise TextoInvalido("Write something before sending.")
    if len(texto) > MAX_TEXTO:
        raise TextoInvalido(
            f"That message is {len(texto)} characters long; the limit is {MAX_TEXTO}."
        )


def _comprobar_destino(destino: str) -> None:
    """Solo donde ya hay conversación. Es el freno que más importa de los cinco.

    Un canal vale si está en la lista que vigilamos a mano o -- con el modo automático
    encendido -- si el descubrimiento lo vio con la cuenta lectora dentro (`es_miembro`).
    Un DM vale solo si ya está en `dm_rastro`, es decir si la conversación ya existía: el
    panel no puede escribirle primero a nadie.
    """
    if not DESTINO.match(destino):
        raise DestinoNoPermitido(f"{destino or 'That'} is not a channel or conversation id.")

    if destino.startswith("D"):
        existe = db.fetch_one("select 1 as ok from dm_rastro where dm_id = %s", (destino,))
        if existe is None:
            raise DestinoNoPermitido(
                "That conversation is not on file yet. You can only reply to conversations "
                "that already exist."
            )
        return

    if destino in {c.strip().upper() for c in secretos.slack_channel_ids()}:
        return
    if canales.modo_todos():
        miembro = db.fetch_one(
            "select 1 as ok from canales where channel_id = %s and es_miembro", (destino,)
        )
        if miembro is not None:
            return
    raise DestinoNoPermitido(
        f"{destino} is not one of the watched channels. You can only reply where the agent "
        "is already reading."
    )


def _comprobar_tope() -> None:
    """El tope diario, contado en `zona_horaria` como el resto de topes del sistema.

    Cuenta TODAS las filas de hoy, no solo las enviadas: un intento fallido se pagó
    igual en atención de alguien, y contar solo los éxitos dejaría que un bucle de
    fallos diera vueltas sin tope -- que es justo lo que el tope existe para evitar
    (ver la cabecera de 0034).

    La fila de ESTE intento ya está escrita cuando se llega aquí: `chat_envio` la deja en
    `pendiente` antes de llamar, para que no pueda existir un mensaje enviado sin
    rastro. Por eso la comparación es `>` y no `>=`: con el tope en 50, el mensaje número
    50 ve 50 filas y pasa, y el 51 ve 51 y no.
    """
    tope = db_config.value(CLAVE_TOPE, TOPE_POR_DEFECTO)
    fila = db.fetch_one(
        "select count(*) as n from mensajes_enviados where created_at >= %s",
        (_inicio_del_dia(datetime.now(UTC)),),
    )
    hoy = fila["n"] if fila else 0
    if hoy > tope:
        raise TopeDiarioAlcanzado(
            f"Daily limit of {tope} messages reached. Try again tomorrow, or raise "
            "the limit in Settings."
        )


class Respondedor:
    """`chat.postMessage` y nada más, con la credencial de sesión de Anthony.

    Con `client=` explícito se usa ese (los tests). Sin él, el token se resuelve en cada
    llamada -- panel primero, entorno de respaldo -- igual que en el lector, para que una
    credencial nueva pegada en Settings surta efecto sin reiniciar el servicio.
    """

    def __init__(self, client=None) -> None:
        self._explicit = client is not None
        self._client = client
        self._current_token: tuple[str, str | None] | None = None

    def _resolve_client(self):
        if self._explicit:
            return self._client
        token = secretos.slack_user_token()
        if not token:
            raise SlackAuthFailed(
                "SLACK_USER_TOKEN no está configurado (ni en el panel, en Settings, ni en .env)"
            )
        cookie = None
        if token.startswith("xoxc-"):
            cookie = secretos.slack_d_cookie()
            if not cookie:
                raise SlackAuthFailed(
                    "El token de sesión (xoxc-) necesita la cookie d (xoxd-…): "
                    "pégala en Settings del panel, o en SLACK_D_COOKIE en .env"
                )
        if (token, cookie) != self._current_token:
            self._client = _web_client(token, cookie)
            self._current_token = (token, cookie)
        return self._client

    def _call(self, method: str, **kwargs):
        """La misma puerta que `FoundersClubReader._call`, con la lista de esta clase.

        Un método que no esté en `WRITE_METHODS` no es un error de Slack ni algo que
        reintentar: es un error de programación, y se para aquí antes de tocar la red."""
        if method not in WRITE_METHODS:
            raise RuntimeError(f"{method} no está permitido: esto solo manda un mensaje")
        client = self._resolve_client()
        try:
            return getattr(client, method)(**kwargs)
        except SlackApiError as exc:
            response = exc.response if exc.response is not None else {}
            error = response.get("error", "") if hasattr(response, "get") else ""
            if error in AUTH_ERRORS:
                raise SlackAuthFailed(f"Slack rechazó el token: {error}") from exc
            raise SlackUnavailable(f"{method}: {error or exc}") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise SlackUnavailable(
                f"{method}: sin conexión con Slack: {type(exc).__name__}: {exc}"
            ) from exc

    def enviar(self, destino: str, texto: str, thread_ts: str | None = None) -> str:
        """Publica `texto` en `destino` y devuelve el `ts` con el que quedó en Slack.

        `thread_ts` lo convierte en una respuesta dentro de ese hilo en vez de un mensaje
        nuevo en el canal; sin él, no se manda el campo en absoluto.

        Los cinco frenos se comprueban aquí, antes de la llamada, y cualquiera de ellos
        corta con su propio tipo de error sin que Slack se entere de nada. El texto es
        obligatorio y lo escribió una persona: no hay forma de llamar a esto sin uno.
        """
        destino = (destino or "").strip().upper()
        texto = texto or ""

        if not _activo():
            raise RespuestasApagadas(
                'Replying is turned off. Turn on "Reply in the Founders Club as Anthony" '
                "in Settings first."
            )
        guards.check_kill_switch()
        _comprobar_texto(texto)
        _comprobar_destino(destino)
        _comprobar_tope()

        argumentos: dict[str, object] = {"channel": destino, "text": texto}
        hilo = (thread_ts or "").strip()
        if hilo:
            argumentos["thread_ts"] = hilo
        respuesta = self._call("chat_postMessage", **argumentos)

        ts = respuesta.get("ts") if hasattr(respuesta, "get") else None
        if not ts:
            # Slack dijo ok pero sin `ts`. El mensaje puede haber salido, así que esto no
            # se trata como un rechazo: quien llama lo anotará como fallido y una persona
            # mirará el canal. Nunca se reintenta solo.
            raise SlackUnavailable("chat_postMessage no devolvió el ts del mensaje")
        return str(ts)
