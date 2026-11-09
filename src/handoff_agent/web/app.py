"""Recibe los clics de la tarjeta y sirve el MCP remoto. Es lo único expuesto a internet.

Cuatro puertas, cada una con su llave: `/slack/acciones` verifica la firma de Slack,
`/mcp` (web/mcp_auth.py) exige una credencial personal, `/oauth/*` (web/oauth.py) es
el servidor de autorización que las emite tras un login contra Supabase Auth, y
`/panel/chat/enviar` exige el access token de la sesión del panel y lo cambia por el
correo de su dueño contra ese mismo Supabase Auth. `/salud` y los `/.well-known/*` son
públicos y no llevan nada sensible.

`/panel/chat/enviar` existe porque el panel no tiene ningún otro camino hasta el worker:
todo lo que hace pasa por PostgREST y por las funciones RLS de la base (el modo de
pruebas encola y el worker drena, ver `panel/app/(panel)/pruebas/`), y mandar un mensaje
a Slack necesita la credencial de sesión de Anthony, que vive en el worker y nunca sale
de él. Quién llama se resuelve SIEMPRE del token y nunca del cuerpo de la petición: el
correo que acaba en la fila de auditoría tiene que ser el de quien pulsó el botón, porque
el mensaje sale en Slack con el nombre de Anthony.

Slack corta a los 3 segundos, así que cada acción es una escritura corta en la
base y una actualización de la tarjeta -- ambas síncronas, y por eso corren en
`run_in_threadpool` en vez de bloquear el bucle de eventos. Nada de research
aquí: "Research more" encola y se va.

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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import chat_envio, db, ledger
from ..config import Settings, load_settings
from ..delivery import slack_writer
from ..delivery.card import _escape_mrkdwn
from ..ingest import queue
from ..mcp_remoto import mcp as mcp_remoto
from . import oauth, slack_signature
from .mcp_auth import GuardiaMCP

logger = logging.getLogger(__name__)

# MCP remoto (mcp_remoto.py) en `/mcp`, detrás de la puerta de credenciales
# (mcp_auth.py): sin token válido, 401 antes de que corra ninguna herramienta. Valen un
# token estático del panel (0023) y un access token OAuth (0026, web/oauth.py).
guardia_mcp = GuardiaMCP()


@asynccontextmanager
async def _ciclo_de_vida(_app: FastAPI) -> AsyncIterator[None]:
    """Arranca el gestor de sesiones del MCP junto con la app web.

    Se crea una app MCP nueva en cada arranque (un gestor de sesiones solo puede
    ejecutarse una vez). Sin estado (`stateless_http`) y con respuestas JSON: cada
    petición es independiente, así que un redeploy de Railway no deja a nadie con una
    sesión muerta, y no hace falta ningún canal SSE abierto. La protección contra DNS
    rebinding del SDK está pensada para servidores en localhost sin autenticación; este
    va detrás de un token Bearer (que un navegador engañado no puede adjuntar) y con
    otro dominio, así que se apaga: dejada en su valor por defecto rechazaría con 421 el
    dominio de Railway.
    """
    interna = mcp_remoto.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    async with interna.router.lifespan_context(interna):
        guardia_mcp.destino = interna
        try:
            yield
        finally:
            guardia_mcp.destino = None


app = FastAPI(title="handoff-acciones", lifespan=_ciclo_de_vida)
app.router.routes.append(Route("/mcp", endpoint=guardia_mcp))
# Servidor de autorización OAuth del MCP (web/oauth.py): metadatos, registro de cliente,
# login y tokens. Es la tercera puerta expuesta a internet, y la única con un formulario
# de contraseña: sus propias defensas están documentadas allí.
app.include_router(oauth.router)

# El cliente de Claude canjea el código desde el navegador, así que sin CORS el
# preflight de /oauth/token muere con 405 y el canje nunca llega (visto en
# producción el 2026-10-13: login correcto, redirección correcta y ni una sola
# petición al endpoint de token). Se abre solo a los dominios de Claude, solo
# para lo que necesita, y sin credenciales de navegador: la autorización viaja
# en la cabecera Authorization, no en cookies. `WWW-Authenticate` se expone
# porque es lo que le dice al cliente dónde está el servidor de autorización.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://(.*\.)?(claude\.ai|claude\.com|anthropic\.com)",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["authorization", "content-type", "mcp-protocol-version", "mcp-session-id"],
    expose_headers=["WWW-Authenticate", "Mcp-Session-Id", "MCP-Protocol-Version"],
    allow_credentials=False,
    max_age=600,
)

# "ver_original" es un botón de tipo URL: Slack manda igualmente un
# block_actions para él, pero no representa una acción nuestra -- no está en
# este conjunto, así que cae en el mismo camino que cualquier action_id
# desconocido: 200, sin tocar la base ni repintar la tarjeta.
NEW_STATE = {"contactado": "contactado", "descartar": "descartado"}
NOTES = {
    "contactado": "✅ Marked as contacted",
    "descartar": "🚫 Discarded: won't show up again",
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
    return _escape_mrkdwn(user.get("username") or "unknown")


def _investigar_mas_note(person: dict) -> str:
    """`delivery/card.py` ya no dibuja el botón "Research more" (Task 2: ese
    trabajo se mudó al panel, "Help the research" / `panel_ayudar_research`,
    migración 0008) -- pero las tarjetas publicadas antes de ese cambio siguen
    vivas en Slack con el botón puesto, y sus clics tienen que seguir
    funcionando. Por eso este manejador y sus tests se quedan tal cual.

    No encola nunca a una persona descartada: la regla global es que una
    persona descartada no vuelve a generar nada, y el worker de research la
    saltaría igualmente -- pero es mejor no gastar ni la fila de la cola.

    Una persona contactada sí se reencola (el research sigue corriendo para
    ella, ver research/worker.py y delivery/deliver.py NO_ALERT_STATES): solo
    deja de avisar, así que la nota lo dice para que nadie espere una tarjeta
    nueva.
    """
    if person["state"] == "descartado":
        return "🚫 Discarded: won't be researched again"
    queued = queue.enqueue(person["slack_user_id"], "manual")
    if person["state"] == "contactado":
        suffix = " (contacted: no new card will be posted)"
        return (
            f"🔁 Queued for new research{suffix}"
            if queued
            else f"⏳ Already queued for research{suffix}"
        )
    return "🔁 Queued for new research" if queued else "⏳ Already queued for research"


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
        # "ver_original" (botón de URL), "abrir_panel" (idem, Task 2) y
        # cualquier action_id desconocido. "investigar_mas" ya no se dibuja en
        # tarjetas nuevas (ver delivery/card.py y _investigar_mas_note más
        # arriba), pero se sigue aceptando: tarjetas viejas ya publicadas
        # todavía lo llevan.
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

    note_text = f"{note} · by {_mention(user)}"
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


# ---------------------------------------------------------------------------------
# Contestar en el Founders Club desde el panel (0034).
#
# La cuarta puerta. La llave es el access token de la sesión de Supabase que el panel ya
# tiene en la cookie de quien mira: se cambia por su correo contra Supabase Auth, igual
# que `web/oauth_login.py` cambia correo y contraseña por una identidad. De ahí en
# adelante decide `chat_envio` (el permiso `puede_responder`) y `tools/respondedor`
# (interruptor, kill switch, tope, destino, texto).
#
# Por qué un token de sesión y no un secreto compartido entre panel y worker: con un
# secreto, el correo del autor tendría que venir en el cuerpo de la petición, y entonces
# la fila de auditoría diría lo que el llamante quisiera. Los mensajes salen con el
# nombre de Anthony; quién los escribió no puede ser un campo de formulario.
# ---------------------------------------------------------------------------------

# Un access token de Supabase es un JWT. No se valida su firma aquí (haría falta el
# secreto del proyecto, que el worker no tiene): se le pregunta a Supabase, que además
# así ve las sesiones ya cerradas. Esta forma solo descarta basura antes de salir a la red.
JWT_RE = re.compile(r"^[A-Za-z0-9._-]{20,4096}$")
# Una persona delante de una caja de texto esperando: más que esto, mejor "try again".
SUPABASE_TIMEOUT = 10.0


def _token_de_sesion(valor: str | None) -> str | None:
    """El token de `Authorization: Bearer <jwt>`, o None si no tiene esa forma."""
    if not valor:
        return None
    esquema, _, resto = valor.partition(" ")
    token = resto.strip()
    if esquema.lower() != "bearer" or not JWT_RE.match(token):
        return None
    return token


def _correo_de_sesion(settings: Settings, token: str) -> str | None:
    """El correo que Supabase Auth confirma para ese access token, o None.

    Es la misma comprobación que hace el panel con `supabase.auth.getUser()`, hecha aquí
    porque la de allí no prueba nada: lo que llega es una petición HTTP. Un token
    caducado, revocado o inventado devuelve None y la petición muere con 401.

    Nunca se registra el token, ni entero ni en trozos: es la sesión de una persona.
    """
    if not settings.supabase_url or not settings.supabase_publishable_key:
        logger.error("chat: falta SUPABASE_URL o SUPABASE_PUBLISHABLE_KEY")
        return None
    try:
        respuesta = httpx.get(
            f"{settings.supabase_url}/auth/v1/user",
            headers={
                "apikey": settings.supabase_publishable_key,
                "Authorization": f"Bearer {token}",
            },
            timeout=SUPABASE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        logger.error("chat: Supabase Auth no responde: %s", type(exc).__name__)
        return None
    if respuesta.status_code != 200:
        # 401/403 es un token que no vale; cualquier otra cosa, un problema de Supabase.
        # Para quien llama son lo mismo (no se le deja pasar) y el cuerpo no se registra.
        if respuesta.status_code not in (400, 401, 403):
            logger.error("chat: Supabase Auth devolvió %s", respuesta.status_code)
        return None
    try:
        datos = respuesta.json()
    except ValueError:
        logger.error("chat: Supabase Auth devolvió algo que no es JSON")
        return None
    correo = datos.get("email") if isinstance(datos, dict) else None
    if not isinstance(correo, str) or "@" not in correo:
        return None
    return correo.strip().lower()


def _texto_o_none(valor: object) -> str | None:
    return valor.strip() or None if isinstance(valor, str) else None


@app.post("/panel/chat/enviar")
async def panel_chat_enviar(request: Request) -> Response:
    """Manda un mensaje al Founders Club como Anthony, de parte de quien trae la sesión.

    Un rechazo del envío (interruptor apagado, sin permiso, destino no vigilado, tope)
    NO es un error de la petición: devuelve 200 con `ok: false` y un texto en inglés que
    el panel enseña tal cual. El 401 y el 400 se reservan para lo que no se pudo ni
    evaluar, para que el panel no confunda "no se pudo mandar" con "se cayó algo".
    """
    settings = load_settings()
    token = _token_de_sesion(request.headers.get("Authorization"))
    if token is None:
        return JSONResponse(
            {"ok": False, "error": "Sign in again to send messages."}, status_code=401
        )
    correo = await run_in_threadpool(_correo_de_sesion, settings, token)
    if correo is None:
        return JSONResponse(
            {"ok": False, "error": "Your session expired. Sign in again."}, status_code=401
        )

    try:
        cuerpo = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "Malformed request."}, status_code=400)
    if not isinstance(cuerpo, dict):
        return JSONResponse({"ok": False, "error": "Malformed request."}, status_code=400)

    destino = cuerpo.get("destino")
    texto = cuerpo.get("texto")
    if not isinstance(destino, str) or not isinstance(texto, str):
        return JSONResponse({"ok": False, "error": "Malformed request."}, status_code=400)

    # El correo va del token, no del cuerpo: un `autor_email` en la petición se ignora.
    resultado = await run_in_threadpool(
        chat_envio.enviar_desde_panel,
        correo,
        destino,
        texto,
        _texto_o_none(cuerpo.get("thread_ts")),
    )
    return JSONResponse(resultado, status_code=200)
