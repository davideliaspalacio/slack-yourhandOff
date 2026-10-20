"""El mensaje de "What's new" en el canal de Handoff después de cada despliegue.

Cada funcionalidad nueva añade aquí una entrada: un `id` que no cambia nunca, un título
y unas pocas líneas en inglés, escritas para Anthony y no para desarrolladores. Cuando
el worker arranca con código nuevo, publica en el canal de tarjetas las entradas que aún
no se habían anunciado, todas en un solo mensaje.

Por qué así y no de otra forma:

- **Lo escribe una persona, no el modelo.** Resumir commits con IA saldría barato, pero
  los commits son técnicos, están en español y un resumen automático puede inventarse
  lo que no hace una funcionalidad. Aquí se publica exactamente lo que está escrito.
- **No se repite.** Lo anunciado se apunta en el ledger (`novedades_publicadas`, con
  los `id`), así que reiniciar el worker, o desplegar sin entradas nuevas, no publica
  nada. Por eso un `id` no se reutiliza ni se cambia nunca.
- **El apunte va antes de publicar**, como en el resumen diario (`delivery/digest.py`):
  si Slack falla se borra el apunte y se reintenta en la vuelta siguiente; si lo que
  falla es borrar el apunte, se pierde ese anuncio en vez de repetirlo cada vuelta.
- Va al canal de Handoff con el bot de Handoff (`slack_writer`), como todo lo que
  publica el agente. Nunca al Founders Club.

Nada de esto lanza: un anuncio no puede tumbar el bucle.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .. import db, guards
from ..config import load_settings
from . import slack_writer

logger = logging.getLogger(__name__)

ACCION = "novedades_publicadas"
CABECERA = "📣 *What's new in the Founders Club agent*"


@dataclass(frozen=True)
class Novedad:
    id: str
    titulo: str
    lineas: tuple[str, ...]


# Las entradas, de la más vieja a la más nueva. Al añadir una funcionalidad, se añade
# aquí su entrada en el mismo commit. Lo ya anunciado no hace falta borrarlo: no vuelve a
# salir, y queda como historial de lo que se ha ido contando.
NOVEDADES: tuple[Novedad, ...] = ()


def _anunciadas() -> set[str]:
    filas = db.fetch_all("select payload from agent_actions where action = %s", (ACCION,))
    ids: set[str] = set()
    for fila in filas:
        ids.update((fila["payload"] or {}).get("ids") or [])
    return ids


def bloques(lista: list[Novedad]) -> list[dict]:
    """Un bloque por entrada: el límite de Slack es por bloque, y así una entrada larga
    no se come a las demás."""
    salida = [{"type": "section", "text": {"type": "mrkdwn", "text": CABECERA}}]
    for n in lista:
        texto = f"*{n.titulo}*\n" + "\n".join(f"• {linea}" for linea in n.lineas)
        salida.append({"type": "section", "text": {"type": "mrkdwn", "text": texto}})
    return salida


def resumen_texto(lista: list[Novedad]) -> str:
    """El texto de la notificación del móvil, que no enseña los bloques."""
    return "What's new: " + "; ".join(n.titulo for n in lista)


def _publicar() -> int:
    settings = load_settings()
    if not (settings.handoff_bot_token and settings.handoff_channel_id):
        return 0
    try:
        guards.check_kill_switch()
    except guards.KillSwitchActive:
        # Con el agente parado no se anuncia nada; sale cuando se reactive.
        return 0

    pendientes = [n for n in NOVEDADES if n.id not in _anunciadas()]
    if not pendientes:
        return 0

    marcador = db.fetch_one(
        "insert into agent_actions (action, payload) values (%s, %s) returning id",
        (ACCION, json.dumps({"ids": [n.id for n in pendientes]})),
    )
    try:
        slack_writer.post_card(bloques(pendientes), resumen_texto(pendientes))
    except Exception:
        logger.exception("novedades: no se pudo publicar; se reintenta en la vuelta siguiente")
        db.execute("delete from agent_actions where id = %s", (marcador["id"],))
        return 0
    return len(pendientes)


def publicar_pendientes() -> int:
    """Publica las novedades aún no anunciadas. Devuelve cuántas salieron. Nunca lanza."""
    try:
        return _publicar()
    except Exception:
        logger.exception("novedades: fallo inesperado; se reintenta en la vuelta siguiente")
        return 0
