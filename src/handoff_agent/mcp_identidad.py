"""Quién llama al MCP remoto, para las herramientas que escriben.

Las herramientas MCP no reciben las cabeceras HTTP, pero la puerta (`web/mcp_auth.py`)
ya sabe de quién es el token. La puerta deja aquí el correo del dueño y el sufijo (los
4 últimos caracteres) en un `ContextVar` justo antes de pasar la petición al servidor
MCP, y las herramientas lo leen con `identidad()`.

Por qué un ContextVar y no un argumento: la identidad no puede venir de lo que Claude
manda en la llamada (el modelo podría ponerse el correo de otro), solo de la puerta.
El SDK corre las herramientas síncronas en un hilo que hereda el contexto de la
petición, así que el valor llega; y se restablece al terminar, para que nada se filtre
a la petición siguiente.

Solo guarda el correo y el sufijo. El token no entra aquí: lo que no se guarda no
puede acabar en una auditoría, en un log ni en un mensaje de error.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver.exceptions import ToolError

from . import db_config

_identidad: ContextVar[tuple[str, str] | None] = ContextVar("mcp_identidad", default=None)

ZONA_POR_DEFECTO = "America/New_York"


class SinIdentidad(ToolError):
    """Una herramienta de escritura se llamó sin pasar por la puerta autenticada."""


@contextmanager
def como(email: str, sufijo: str):
    """Fija quién llama mientras dura el bloque (la puerta, y los tests)."""
    marca = _identidad.set((email, sufijo))
    try:
        yield
    finally:
        _identidad.reset(marca)


def identidad() -> tuple[str, str]:
    """(correo del dueño del token, sufijo del token) de la petición en curso.

    Falla si no hay: una escritura sin quién la firma no debe ejecutarse."""
    valor = _identidad.get()
    if valor is None:
        raise SinIdentidad(
            "no caller identity: write tools only work through the authenticated /mcp endpoint"
        )
    return valor


def zona_desde(nombre: object) -> ZoneInfo:
    """La zona con ese nombre; si está mal escrita (o no es un texto), la de por defecto."""
    try:
        return ZoneInfo(nombre)  # type: ignore[arg-type]
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo(ZONA_POR_DEFECTO)


def zona_horaria() -> ZoneInfo:
    """La zona de `zona_horaria` en config (con su propia conexión)."""
    return zona_desde(db_config.value("zona_horaria", ZONA_POR_DEFECTO))


def hoy() -> str:
    """El día de hoy (ISO) en la zona del equipo: la ventana de los topes diarios."""
    return datetime.now(zona_horaria()).date().isoformat()
