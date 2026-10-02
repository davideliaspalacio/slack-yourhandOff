"""Latido del lector de Slack para el panel.

Cada sondeo deja en `config` (clave `lector_estado`) si el lector pudo leer o
por qué no. Quien pega el token suele no tocar Railway ni la base: esto es lo
único que le dice si la credencial funciona de verdad.

Escribir el latido nunca puede romper ni parar el sondeo: cualquier fallo se
registra y se sigue. Y nunca se guarda una credencial: el detalle de un error
se recorta y se le quitan los tokens y cookies de Slack por si alguno se
coló en el mensaje de una excepción.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from . import db

logger = logging.getLogger(__name__)

LECTOR_ESTADO_KEY = "lector_estado"
DETALLE_MAX = 200
# xoxc-/xoxp-/xoxb-/xoxa-… y la cookie xoxd-…, hasta el primer espacio o separador.
_CREDENCIAL = re.compile(r"xox[a-z]-[^\s'\",;:)]+", re.IGNORECASE)


def _detalle(exc: BaseException) -> str:
    texto = _CREDENCIAL.sub("[credencial]", str(exc))
    texto = " ".join(texto.split())
    return texto if len(texto) <= DETALLE_MAX else texto[: DETALLE_MAX - 1] + "…"


def _escribir(estado: dict) -> None:
    try:
        db.execute(
            """
            insert into config (key, value, updated_at) values (%s, %s::jsonb, now())
            on conflict (key) do update set value = excluded.value, updated_at = now()
            """,
            (LECTOR_ESTADO_KEY, json.dumps(estado)),
        )
    except Exception:
        logger.exception("no se pudo escribir el latido del lector; el sondeo sigue")


def _ahora() -> str:
    return datetime.now(UTC).isoformat()


def registrar_ok(usuario: str, equipo: str, canales: int) -> None:
    _escribir(
        {"ok": True, "en": _ahora(), "usuario": usuario, "equipo": equipo, "canales": canales}
    )


def registrar_fallo(exc: BaseException) -> None:
    _escribir({"ok": False, "en": _ahora(), "error": type(exc).__name__, "detalle": _detalle(exc)})
