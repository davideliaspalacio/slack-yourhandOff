"""Valores operativos que viven en la tabla config, no en el entorno.

Los topes y umbrales tienen que poder cambiarse sin redeploy: es lo que pide
el spec y lo que permite parar o aflojar el sistema en caliente.
"""

from __future__ import annotations

import logging

from . import db

logger = logging.getLogger(__name__)


def value[T](key: str, default: T) -> T:
    row = db.fetch_one("select value from config where key = %s", (key,))
    if row is None:
        return default
    found = row["value"]
    # bool es subclase de int en Python: sin este caso aparte, un default
    # entero aceptaría un true/false guardado como si fuera 1/0, y un default
    # booleano nunca podría leer un valor real de la tabla (isinstance(found,
    # bool) siempre lo habría rechazado).
    if isinstance(default, bool):
        valid = isinstance(found, bool)
    else:
        valid = isinstance(found, type(default)) and not isinstance(found, bool)
    if not valid:
        logger.warning("config %s: valor inválido %r, se usa %r", key, found, default)
        return default
    return found
