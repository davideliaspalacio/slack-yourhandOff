"""Read-only access to the Founders Club Slack.

The workspace belongs to a third party and Anthony is one member among 1,400.
The agent never writes there — no messages, no reactions, no DMs — so this
wrapper exposes reads only: `READ_METHODS` lists the methods it may call,
`_call` refuses anything else, and a test pins that list. Anything the agent
posts goes to Handoff's own Slack, through a different credential.
"""

from __future__ import annotations

import http.client
import logging
from dataclasses import dataclass

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry import default_retry_handlers
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from . import secretos

logger = logging.getLogger(__name__)

# No se arreglan reintentando: hace falta una persona (token nuevo o más scopes).
AUTH_ERRORS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "missing_scope",
    }
)
PAGE_SIZE = 200
RATE_LIMIT_RETRIES = 3

# Los únicos métodos de Slack que este cliente puede llamar. La lista está aquí, y no
# solo en el test que la vigila (tests/test_slack_client.py), para que `_call` la imponga
# en tiempo de ejecución: añadir un método de escritura no es un descuido posible.
READ_METHODS = frozenset(
    {
        "auth_test",
        "conversations_history",
        "conversations_info",
        "conversations_list",
        "conversations_members",
        # Las respuestas de un hilo (0032). Solo lista: no publica, no reacciona y no
        # marca nada como leído.
        "conversations_replies",
        "users_info",
        "chat_getPermalink",
    }
)

# Lo que NUNCA entra en la lista de arriba, por si a alguien le parece inocente:
# `conversations.mark` no publica nada, pero movería el marcador de leído de SUS
# mensajes directos -- el agente le borraría las notificaciones sin querer. Leer
# el rastro de los DMs no puede cambiar nada de lo que él ve en su Slack.
# tests/test_slack_client.py lo comprueba.


class SlackAuthFailed(RuntimeError):
    """The token is missing, revoked or lacks a scope. Needs a human."""


class SlackUnavailable(RuntimeError):
    """Slack answered with an error worth retrying later."""


class SlackUserNotFound(SlackUnavailable):
    """users.info no conoce a esa persona: se fue del workspace, o se creó a
    mano (scripts de prueba, panel). Reintentar no sirve; hereda de
    SlackUnavailable para que quien ya lo captura siga igual."""


@dataclass(frozen=True)
class UserProfile:
    user_id: str
    real_name: str
    title: str
    email: str
    is_bot: bool
    deleted: bool


def _web_client(token: str, cookie: str | None = None) -> WebClient:
    # Un token de sesión (xoxc-) solo lo acepta Slack junto con la cookie `d`.
    headers = {"Cookie": f"d={cookie}"} if token.startswith("xoxc-") and cookie else None
    return WebClient(
        token=token,
        headers=headers,
        retry_handlers=[
            *default_retry_handlers(),
            RateLimitErrorRetryHandler(max_retry_count=RATE_LIMIT_RETRIES),
        ],
    )


class FoundersClubReader:
    """Con `client=` o `token=` explícitos se usa ese. Sin ninguno, el token se
    resuelve en cada llamada (panel primero, entorno de respaldo) para que uno
    nuevo pegado en el panel surta efecto en el siguiente sondeo, sin reiniciar."""

    def __init__(self, token: str | None = None, client=None) -> None:
        self._explicit = client is not None or bool(token)
        self._client = client if client is not None else (_web_client(token) if token else None)
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
        if method not in READ_METHODS:
            # No es un error de Slack ni algo que reintentar: es un error de programación.
            raise RuntimeError(f"{method} no es de lectura: el agente nunca escribe allí")
        client = self._resolve_client()
        try:
            return getattr(client, method)(**kwargs)
        except SlackApiError as exc:
            response = exc.response if exc.response is not None else {}
            error = response.get("error", "") if hasattr(response, "get") else ""
            if error in AUTH_ERRORS:
                pista = ""
                if error != "missing_scope":
                    pista = " (la credencial puede haber caducado: pega una nueva en Settings del panel)"
                raise SlackAuthFailed(f"Slack rechazó el token: {error}{pista}") from exc
            if error == "user_not_found":
                raise SlackUserNotFound(f"{method}: {error}") from exc
            raise SlackUnavailable(f"{method}: {error or exc}") from exc
        except (OSError, http.client.HTTPException) as exc:
            # Transport errors (timeout, DNS error, connection reset, remote disconnect)
            # are retryable when wrapped; they signal a temporary unavailability.
            raise SlackUnavailable(
                f"{method}: sin conexión con Slack: {type(exc).__name__}: {exc}"
            ) from exc

    def identity(self) -> tuple[str, str]:
        """(user_id, team) del token, de una sola llamada a auth.test."""
        response = self._call("auth_test")
        return response["user_id"], response.get("team") or ""

    def owner_id(self) -> str:
        """The member whose token this is (Anthony, in production)."""
        return self.identity()[0]

    def team_id(self) -> str:
        """El código del workspace (T…), lo único que falta para montar el
        enlace `app.slack.com/client/<team_id>/<dm_id>` de una conversación.

        Aparte de `identity()` a propósito: aquella devuelve el *nombre* del
        equipo, que es lo que muestra el panel, y cambiar su forma rompería a
        quien ya la usa. Puede volver vacía; quien la use tiene que seguir
        funcionando sin enlace."""
        return self._call("auth_test").get("team_id") or ""

    def dms(self) -> list[dict]:
        """Las conversaciones directas uno a uno del dueño de la credencial.

        `types=im` pide solo DMs: los grupos (`mpim`) y los canales privados no
        entran, ni aquí ni en el filtro de `ingest/dms.py`. Es de lectura, como
        el resto de la clase: listar conversaciones no las marca como leídas.
        """
        conversaciones: list[dict] = []
        cursor = None
        while True:
            page = self._call(
                "conversations_list",
                types="im",
                exclude_archived=True,
                limit=PAGE_SIZE,
                cursor=cursor,
            )
            conversaciones.extend(page.get("channels") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return conversaciones

    def canales(self, incluir_privados: bool = True) -> list[dict]:
        """Los canales del workspace, con `is_member` para saber en cuáles está él.

        Para elegir qué vigilar sin cazar códigos en la URL, y para medir antes de
        ampliar la lista. Pide canales, no DMs (`dms()` es el otro extremo del mismo
        método de Slack). Listar no abre nada ni marca nada como leído.

        Un canal con `is_member` falso se devuelve igual, para poder enseñarlo, pero su
        historial no se puede leer con un token de usuario que no está dentro: el agente
        nunca se une por él, porque unirse sí se vería en el canal.
        """
        tipos = "public_channel,private_channel" if incluir_privados else "public_channel"
        canales: list[dict] = []
        cursor = None
        while True:
            page = self._call(
                "conversations_list",
                types=tipos,
                exclude_archived=True,
                limit=PAGE_SIZE,
                cursor=cursor,
            )
            canales.extend(page.get("channels") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return canales

    def last_message(self, channel: str) -> dict | None:
        """El último mensaje de una conversación, y solo ese (`limit=1`).

        Lo usa el rastro de los DMs, que se queda con la marca de tiempo y el
        autor y descarta el resto: el texto no se guarda en ninguna parte.
        None si la conversación está vacía."""
        page = self._call("conversations_history", channel=channel, limit=1)
        mensajes = page.get("messages") or []
        return mensajes[0] if mensajes else None

    def history(self, channel: str, oldest: str) -> list[dict]:
        messages: list[dict] = []
        cursor = None
        while True:
            page = self._call(
                "conversations_history",
                channel=channel,
                oldest=oldest,
                limit=PAGE_SIZE,
                cursor=cursor,
            )
            messages.extend(page.get("messages") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not page.get("has_more") or not cursor:
                return messages

    def respuestas(self, channel: str, thread_ts: str, oldest: str | None = None) -> list[dict]:
        """Los mensajes de un hilo: primero el principal, luego las respuestas (0032).

        Es `conversations.replies`, de lectura como el resto de la clase: no publica, no
        reacciona y no marca nada como leído. `oldest` es el marcador del hilo (la última
        respuesta ya traída), para no volver a bajar lo que ya está guardado; Slack lo
        trata como exclusivo y devuelve el principal igualmente, así que quien llama
        descarta lo que no sea más nuevo."""
        messages: list[dict] = []
        cursor = None
        while True:
            page = self._call(
                "conversations_replies",
                channel=channel,
                ts=thread_ts,
                oldest=oldest,
                limit=PAGE_SIZE,
                cursor=cursor,
            )
            messages.extend(page.get("messages") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not page.get("has_more") or not cursor:
                return messages

    def members(self, channel: str) -> set[str]:
        found: set[str] = set()
        cursor = None
        while True:
            page = self._call(
                "conversations_members", channel=channel, limit=PAGE_SIZE, cursor=cursor
            )
            found.update(page.get("members") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return found

    def channel_name(self, channel: str) -> str:
        """El nombre del canal (`founders-general`), para mostrarlo en vez del código.

        Es de lectura, como el resto de la clase. Puede volver vacío: un canal sin
        nombre (un DM) o una respuesta sin ese campo. Quien la usa tiene que seguir
        funcionando sin nombre."""
        info = self._call("conversations_info", channel=channel).get("channel") or {}
        return info.get("name") or ""

    def dm_estado(self, dm_id: str) -> dict:
        """Lo no leído de un DM: cuántos mensajes tiene él sin abrir y hasta qué
        `ts` leyó. Para que la sugerencia de respuesta sepa si Anthony ya vio lo
        último (ingest/dm_texto.py).

        Es `conversations.info`, de lectura como el resto de la clase: preguntar
        no marca nada como leído (eso sería `conversations.mark`, que no entra).
        `unread_count_display` es el número que él ve en su Slack; si no viene,
        `unread_count`. Si no viene ninguno, None y no 0: un cero diría que lo
        leyó todo, y no se sabe."""
        info = self._call("conversations_info", channel=dm_id).get("channel") or {}
        sin_leer = info.get("unread_count_display")
        if sin_leer is None:
            sin_leer = info.get("unread_count")
        try:
            sin_leer = int(sin_leer) if sin_leer is not None else None
        except (TypeError, ValueError):
            sin_leer = None
        return {"sin_leer": sin_leer, "leido_hasta": info.get("last_read") or None}

    def user_profile(self, user_id: str) -> UserProfile:
        user = self._call("users_info", user=user_id)["user"]
        profile = user.get("profile") or {}
        return UserProfile(
            user_id=user_id,
            real_name=user.get("real_name") or profile.get("real_name") or "",
            title=profile.get("title") or "",
            email=profile.get("email") or "",
            is_bot=bool(user.get("is_bot")),
            deleted=bool(user.get("deleted")),
        )

    def permalink(self, channel: str, ts: str) -> str | None:
        """Enlace al mensaje real. Es de lectura, como el resto de la clase.

        Todo el sistema existe para producir el clic en este enlace, pero si
        Slack no lo da (mensaje borrado, canal archivado) la tarjeta se manda
        igual: vale más una tarjeta sin enlace que ninguna tarjeta.
        """
        try:
            return self._call("chat_getPermalink", channel=channel, message_ts=ts)["permalink"]
        except SlackUnavailable:
            logger.warning("sin permalink para %s/%s", channel, ts)
            return None
