"""Ajustes operativos que se pegan desde el panel en vez de vivir en el entorno.

El token del Founders Club lo produce (y puede tener que renovar) una persona
que no toca Railway. Por eso la base gana al entorno: si hay un valor en
`secretos`, se usa ese; `SLACK_USER_TOKEN` queda de respaldo para cuando el
panel nunca lo guardó. Se lee en cada llamada, sin caché, para que un token
nuevo surta efecto en el siguiente sondeo sin reiniciar el worker.

Los canales vigilados siguen la misma regla, aunque no son secretos: viven en
`config` (clave `slack_channel_ids`, un array jsonb) y `SLACK_CHANNEL_IDS` es
el respaldo. Una lista vacía en la base cuenta como "sin valor del panel".
"""

from __future__ import annotations

import psycopg.errors

from . import db
from .config import load_settings

SLACK_USER_TOKEN_KEY = "slack_user_token"
SLACK_D_COOKIE_KEY = "slack_d_cookie"


def _leer(key: str) -> str | None:
    try:
        row = db.fetch_one("select value from secretos where key = %s", (key,))
    except psycopg.errors.UndefinedTable:
        # Base anterior a la migración 0014: solo existe el entorno.
        row = None
    return row["value"] if row and row["value"] else None


def slack_user_token() -> str | None:
    return _leer(SLACK_USER_TOKEN_KEY) or load_settings().slack_user_token


def slack_d_cookie() -> str | None:
    """Cookie `d` (xoxd-…): solo hace falta con un token de sesión (xoxc-)."""
    return _leer(SLACK_D_COOKIE_KEY) or load_settings().slack_d_cookie


SLACK_CHANNEL_IDS_KEY = "slack_channel_ids"


def slack_channel_ids() -> tuple[str, ...]:
    """Canales del Founders Club a vigilar: gana el panel, el entorno es respaldo."""
    try:
        row = db.fetch_one("select value from config where key = %s", (SLACK_CHANNEL_IDS_KEY,))
    except psycopg.errors.UndefinedTable:
        row = None
    valor = row["value"] if row else None
    if isinstance(valor, list):
        canales = tuple(c.strip() for c in valor if isinstance(c, str) and c.strip())
        if canales:
            return canales
    return load_settings().slack_channel_ids
