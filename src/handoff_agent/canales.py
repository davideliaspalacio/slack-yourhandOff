"""Los nombres de los canales de Slack, para no tener que mostrar solo su código.

`C08MRLP46G6` no le dice nada a nadie. La tabla `canales` (migración 0025) guarda el
nombre que Slack da a cada canal y cuándo se pidió por última vez; lo rellena el ciclo de
lectura del worker (`ingest/watcher.py`) con `conversations.info`, un método de solo
lectura. El nombre puede faltar siempre (la llamada falla, el canal es privado): entonces
se muestra el código, como hasta ahora, y nada se rompe.

Este módulo solo LEE y formatea, y las lecturas que sirven al MCP remoto van con el
cursor de quien llama (su transacción es `READ ONLY`): así puede entrar en el servidor de
lectura sin abrirle ningún camino de escritura. Quien guarda es el watcher.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from . import db

# Cada cuánto se vuelve a preguntar el nombre de un canal que ya se conoce. Un canal se
# renombra muy de vez en cuando, y cada consulta es una llamada más a Slack.
HORAS_FRESCAS = 24

# El formato de un id de canal y el máximo de canales vigilados: los mismos que valida
# la base (0016 y 0025).
CODIGO = re.compile(r"^[CG][A-Z0-9]{5,}$")
MAXIMO = 20


class CanalDesconocido(ValueError):
    """La referencia a un canal no se pudo resolver: no existe, o el nombre es ambiguo."""


def etiqueta(channel_id: str, nombre: str | None) -> str:
    """`#founders-general` si se conoce el nombre; el código a secas si no."""
    limpio = (nombre or "").strip().lstrip("#")
    return f"#{limpio}" if limpio else channel_id


def necesita_nombre(channel_id: str) -> bool:
    """Si toca preguntarle a Slack: nunca se preguntó, o la última vez fue hace mucho.

    Cuenta el último intento, no el último acierto: un canal que no da su nombre (la
    llamada falla, es privado) deja su fila sin nombre, y así no se le pregunta otra vez
    en cada sondeo."""
    fila = db.fetch_one("select actualizado_at from canales where channel_id = %s", (channel_id,))
    if fila is None:
        return True
    return fila["actualizado_at"] < datetime.now(UTC) - timedelta(hours=HORAS_FRESCAS)


def nombres(cur, channel_ids) -> dict[str, str]:
    """{código: nombre} de los que se conozcan, con el cursor de quien pregunta."""
    ids = [c for c in dict.fromkeys(channel_ids) if c]
    if not ids:
        return {}
    cur.execute(
        "select channel_id, nombre from canales "
        "where channel_id = any(%s) and nombre is not null and nombre <> ''",
        (ids,),
    )
    return {f["channel_id"]: f["nombre"] for f in cur.fetchall()}


def _conocidos(cur) -> list[tuple[str, str | None]]:
    cur.execute("select channel_id, nombre from canales order by nombre, channel_id")
    return [(f["channel_id"], f["nombre"]) for f in cur.fetchall()]


def _pista(conocidos: list[tuple[str, str | None]], vigilados) -> str:
    """Qué canales hay, para el mensaje de error: `#nombre (CÓDIGO)` o el código."""
    nombres_por_codigo = {c: n for c, n in conocidos if n}
    todos = {c for c in vigilados if c} | {c for c, _ in conocidos}
    listado = sorted(
        f"{etiqueta(c, nombres_por_codigo[c])} ({c})" if c in nombres_por_codigo else c
        for c in todos
    )
    return ", ".join(listado) if listado else "none yet"


def resolver(cur, referencia: str, vigilados=()) -> str:
    """El código de un canal a partir de un código o de su nombre (con `#` o sin él).

    Un código vale si se vigila o si la tabla lo conoce; así una errata no se convierte
    en una respuesta vacía que parezca un "aquí no hay nada". Un nombre se busca en la
    tabla, sin distinguir mayúsculas. Lo que no se pueda resolver -- o lo que salga en dos
    canales a la vez -- es un error que dice qué canales hay."""
    bruto = (referencia or "").strip()
    conocidos = _conocidos(cur)
    pista = _pista(conocidos, vigilados)
    if not bruto:
        raise CanalDesconocido(f"empty channel; channels to choose from: {pista}")

    codigo = bruto.upper()
    if codigo in {c for c, _ in conocidos} or codigo in {c.upper() for c in vigilados if c}:
        return codigo

    buscado = bruto.lstrip("#").strip().lower()
    coincidencias = sorted(c for c, n in conocidos if (n or "").strip().lower() == buscado)
    if len(coincidencias) == 1:
        return coincidencias[0]
    if len(coincidencias) > 1:
        raise CanalDesconocido(
            f"ambiguous channel {bruto!r}: {', '.join(coincidencias)} share that name; use the code"
        )
    raise CanalDesconocido(f"unknown channel {bruto!r}; channels to choose from: {pista}")
