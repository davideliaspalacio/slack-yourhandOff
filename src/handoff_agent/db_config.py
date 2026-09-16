"""Valores operativos que viven en la tabla config, no en el entorno.

Los topes y umbrales tienen que poder cambiarse sin redeploy: es lo que pide
el spec y lo que permite parar o aflojar el sistema en caliente.
"""

from __future__ import annotations

import logging
from typing import TypeVar

from . import db

logger = logging.getLogger(__name__)
T = TypeVar("T")


def value[T](key: str, default: T) -> T:
    row = db.fetch_one("select value from config where key = %s", (key,))
    if row is None:
        return default
    found = row["value"]
    if isinstance(found, bool) or not isinstance(found, type(default)):
        logger.warning("config %s: valor inválido %r, se usa %r", key, found, default)
        return default
    return found
