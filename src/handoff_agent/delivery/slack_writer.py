"""El único sitio del código que escribe en Slack, y solo en el de Handoff.

El lector del Founders Club y este escritor son clases/módulos distintos con
tokens distintos a propósito: así "el agente nunca escribe en el Founders
Club" es algo que impone el código, no una intención. Antes de publicar o
actualizar una tarjeta se comprueba que el canal de destino no sea uno de los
vigilados (`Settings.slack_channel_ids`) y que exista un token de bot propio
de Handoff — nunca se reutiliza ni se cae de vuelta al token de lectura.
"""

from __future__ import annotations

from functools import lru_cache

from slack_sdk import WebClient

from ..config import Settings, load_settings


class SlackWriteRefused(RuntimeError):
    """Falta el token del bot, falta el canal, o el destino es un canal vigilado."""


def _require_bot_token(settings: Settings) -> str:
    if not settings.handoff_bot_token:
        raise SlackWriteRefused("falta HANDOFF_SLACK_BOT_TOKEN")
    return settings.handoff_bot_token


@lru_cache(maxsize=1)
def _client() -> WebClient:
    settings = load_settings()
    token = _require_bot_token(settings)
    return WebClient(token=token, timeout=settings.http_timeout_seconds)


def _guard_target_channel(channel: str | None, settings: Settings) -> str:
    if not channel:
        raise SlackWriteRefused("falta el canal de destino")
    # Normalizar: espacios en blanco y comparación insensible a mayúsculas
    channel_normalized = channel.strip().upper()
    for watched_id in settings.slack_channel_ids:
        if channel_normalized == watched_id.upper():
            raise SlackWriteRefused(
                f"{channel} es un canal vigilado del Founders Club: no se escribe allí"
            )
    return channel


def post_card(blocks: list[dict], text: str) -> tuple[str, str]:
    """Publica una tarjeta nueva en el canal de Handoff configurado.

    Las comprobaciones de token y canal se hacen aquí, no solo dentro de
    `_client()`: en los tests `_client` se sustituye por un doble que no las
    aplicaría, y un token o un canal mal configurados tienen que impedir la
    publicación pase lo que pase con el cliente real.
    """
    settings = load_settings()
    _require_bot_token(settings)
    channel = _guard_target_channel(settings.handoff_channel_id, settings)
    response = _client().chat_postMessage(channel=channel, blocks=blocks, text=text)
    return response["channel"], response["ts"]


def update_card(channel: str, ts: str, blocks: list[dict], text: str) -> None:
    """Actualiza en el sitio una tarjeta ya publicada.

    Sujeta a las mismas comprobaciones que `post_card`: un `channel` que
    apunte al Founders Club, o que llegue vacío, se rechaza igual.
    """
    settings = load_settings()
    _require_bot_token(settings)
    _guard_target_channel(channel, settings)
    _client().chat_update(channel=channel, ts=ts, blocks=blocks, text=text)
