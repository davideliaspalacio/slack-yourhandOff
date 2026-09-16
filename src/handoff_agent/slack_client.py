"""Read-only access to the Founders Club Slack.

The workspace belongs to a third party and Anthony is one member among 1,400.
The agent never writes there — no messages, no reactions, no DMs — so this
wrapper exposes reads only, and a test pins that surface. Anything the agent
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


class SlackAuthFailed(RuntimeError):
    """The token is missing, revoked or lacks a scope. Needs a human."""


class SlackUnavailable(RuntimeError):
    """Slack answered with an error worth retrying later."""


@dataclass(frozen=True)
class UserProfile:
    user_id: str
    real_name: str
    title: str
    email: str
    is_bot: bool
    deleted: bool


class FoundersClubReader:
    def __init__(self, token: str | None = None, client=None) -> None:
        if client is None:
            if not token:
                raise SlackAuthFailed("SLACK_USER_TOKEN no está configurado")
            client = WebClient(
                token=token,
                retry_handlers=[
                    *default_retry_handlers(),
                    RateLimitErrorRetryHandler(max_retry_count=RATE_LIMIT_RETRIES),
                ],
            )
        self._client = client

    def _call(self, method: str, **kwargs):
        try:
            return getattr(self._client, method)(**kwargs)
        except SlackApiError as exc:
            response = exc.response if exc.response is not None else {}
            error = response.get("error", "") if hasattr(response, "get") else ""
            if error in AUTH_ERRORS:
                raise SlackAuthFailed(f"Slack rechazó el token: {error}") from exc
            raise SlackUnavailable(f"{method}: {error or exc}") from exc
        except (OSError, http.client.HTTPException) as exc:
            # Transport errors (timeout, DNS error, connection reset, remote disconnect)
            # are retryable when wrapped; they signal a temporary unavailability.
            raise SlackUnavailable(
                f"{method}: sin conexión con Slack: {type(exc).__name__}: {exc}"
            ) from exc

    def owner_id(self) -> str:
        """The member whose token this is (Anthony, in production)."""
        return self._call("auth_test")["user_id"]

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
