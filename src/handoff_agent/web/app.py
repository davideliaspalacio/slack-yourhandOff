"""Recibe los clics de la tarjeta. Es lo único del sistema expuesto a internet.

Slack corta a los 3 segundos, así que cada acción es una escritura corta en la
base y una actualización de la tarjeta -- ambas síncronas, y por eso corren en
`run_in_threadpool` en vez de bloquear el bucle de eventos. Nada de research
aquí: "Investigar más" encola y se va.

Nada de lo que llega en el cuerpo de la petición se confía: puede venir
falsificado (de ahí la firma), truncado, o simplemente no ser lo que Slack
manda. Ninguna entrada, por rara que sea, puede terminar en un 500 ni en una
escritura a medias -- ver las correcciones del controlador en el brief de esta
tarea. El cuerpo y el payload nunca se registran en los logs: llevan el token
de verificación heredado de Slack, que es tan sensible como un secreto.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from .. import db, ledger
from ..config import Settings, load_settings
from ..delivery import slack_writer
from ..delivery.card import _escape_mrkdwn
from ..ingest import queue
from . import slack_signature

logger = logging.getLogger(__name__)
app = FastAPI(title="handoff-acciones")

# "ver_original" es un botón de tipo URL: Slack manda igualmente un
# block_actions para él, pero no representa una acción nuestra -- no está en
# este conjunto, así que cae en el mismo camino que cualquier action_id
# desconocido: 200, sin tocar la base ni repintar la tarjeta.
NEW_STATE = {"contactado": "contactado", "descartar": "descartado"}
NOTES = {
    "contactado": "✅ Marcado como contactado",
    "descartar": "🚫 Descartado: no volverá a aparecer",
}
# Un ID de usuario o de bot de Slack real: solo entonces se usa la mención viva
# <@ID>. Cualquier otra cosa (falta el id, viene vacío, viene manipulado) cae
# al nombre de usuario, ya escapado.
SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")


@app.get("/salud")
def salud() -> dict:
    return {"ok": True}


def _parse_payload(body: bytes) -> dict | None:
    """None si el cuerpo no es lo que Slack manda: ni bien formado como
    `application/x-www-form-urlencoded` con un campo `payload`, ni JSON válido
    dentro de ese campo. Cualquier otra cosa (verbose pero deliberado: nunca
    dejar pasar una excepción sin nombrar) responde 400 sin tocar la base."""
    try:
        text = body.decode()
    except UnicodeDecodeError:
        return None
    try:
        raw = parse_qs(text)["payload"][0]
    except (KeyError, IndexError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _channel_allowed(channel_id: str | None, settings: Settings) -> bool:
    """Comparación normalizada, igual que `slack_writer._guard_target_channel`:
    solo el canal de Handoff configurado, nunca ningún otro."""
    if not channel_id or not settings.handoff_channel_id:
        return False
    return channel_id.strip().upper() == settings.handoff_channel_id.strip().upper()


def _mention(user: dict) -> str:
    user_id = user.get("id") or ""
    if SLACK_ID_RE.match(user_id):
        return f"<@{user_id}>"
    return _escape_mrkdwn(user.get("username") or "desconocido")


def _investigar_mas_note(person: dict) -> str:
    """No encola nunca a una persona descartada: la regla global es que una
    persona descartada no vuelve a generar nada, y el worker de research la
    saltaría igualmente -- pero es mejor no gastar ni la fila de la cola."""
    if person["state"] == "descartado":
        return "🚫 Descartada: no se vuelve a investigar"
    if queue.enqueue(person["slack_user_id"], "manual"):
        return "🔁 En cola para volver a investigar"
    return "⏳ Ya estaba en cola para investigarse"


def _apply_action(action_id: str, person: dict) -> str:
    """Aplica el efecto en la base y devuelve la nota para la tarjeta.

    "descartar" es idempotente por construcción: el UPDATE dos veces sobre el
    mismo estado no es un error, solo un no-op observable.
    """
    if action_id == "investigar_mas":
        return _investigar_mas_note(person)
    db.execute(
        "update prospects set state = %s, updated_at = now() where id = %s",
        (NEW_STATE[action_id], person["id"]),
    )
    return NOTES[action_id]


def _repaint(channel: str, message: dict | None, note_text: str) -> None:
    """Reconstruye la tarjeta a partir de los bloques que Slack ya tenía,
    quita los botones y añade la nota. Si Slack no mandó `message` (no
    debería pasar en un `block_actions` de verdad, pero nunca se confía en la
    entrada), no hay nada que repintar."""
    if message is None:
        return
    blocks = [b for b in (message.get("blocks") or []) if b.get("type") != "actions"]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note_text}]})
    try:
        slack_writer.update_card(channel, message.get("ts"), blocks, note_text)
    except Exception as exc:  # noqa: BLE001 - el estado ya cambió, no se pierde por el repintado
        logger.warning("acciones: no se pudo repintar la tarjeta: %s", type(exc).__name__)


def _handle(payload: dict, settings: Settings) -> None:
    actions = payload.get("actions") or []
    if not actions:
        return
    action = actions[0]
    if not isinstance(action, dict):
        return
    action_id = action.get("action_id")

    channel = (payload.get("channel") or {}).get("id")
    if not _channel_allowed(channel, settings):
        logger.warning("acciones: canal no reconocido: %r", channel)
        return

    if action_id not in NEW_STATE and action_id != "investigar_mas":
        # "ver_original" (botón de URL) y cualquier action_id desconocido.
        return

    try:
        prospect_id = str(uuid.UUID(str(action.get("value"))))
    except (TypeError, ValueError, AttributeError):
        return

    person = db.fetch_one("select * from prospects where id = %s", (prospect_id,))
    if person is None:
        return

    user = payload.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    username = user.get("username") or "desconocido"

    note = _apply_action(action_id, person)

    ledger.record_action(
        "boton_pulsado",
        {"accion": action_id, "usuario": username, "usuario_id": user.get("id")},
        prospect_id=prospect_id,
    )
    logger.info("acciones: %s sobre %s", action_id, prospect_id)

    note_text = f"{note} · por {_mention(user)}"
    _repaint(channel, payload.get("message"), note_text)


@app.post("/slack/acciones")
async def acciones(request: Request) -> Response:
    settings = load_settings()
    body = await request.body()
    if not settings.slack_signing_secret or not slack_signature.verify(
        settings.slack_signing_secret,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        request.headers.get("X-Slack-Signature", ""),
        body,
    ):
        return Response(status_code=401)

    payload = _parse_payload(body)
    if payload is None:
        return Response(status_code=400)

    await run_in_threadpool(_handle, payload, settings)
    return Response(status_code=200)
