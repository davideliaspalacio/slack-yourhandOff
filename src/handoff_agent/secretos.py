"""Secretos que se pegan desde el panel en vez de vivir en el entorno.

El token del Founders Club lo produce (y puede tener que renovar) una persona
que no toca Railway. Por eso la base gana al entorno: si hay un valor en
`secretos`, se usa ese; `SLACK_USER_TOKEN` queda de respaldo para cuando el
panel nunca lo guardó. Se lee en cada llamada, sin caché, para que un token
nuevo surta efecto en el siguiente sondeo sin reiniciar el worker.
"""

from __future__ import annotations

import psycopg.errors

from . import db
from .config import load_settings

SLACK_USER_TOKEN_KEY = "slack_user_token"


def slack_user_token() -> str | None:
    try:
        row = db.fetch_one("select value from secretos where key = %s", (SLACK_USER_TOKEN_KEY,))
    except psycopg.errors.UndefinedTable:
        # Base anterior a la migración 0012: solo existe el entorno.
        row = None
    if row and row["value"]:
        return row["value"]
    return load_settings().slack_user_token
