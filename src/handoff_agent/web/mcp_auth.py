"""Puerta del MCP remoto: ninguna herramienta corre sin un token válido.

Es un middleware ASGI puro delante de la app MCP. Para cada petición a `/mcp`:

1. `Authorization: Bearer <token>`. Sin cabecera, con otro esquema, con algo que no
   tiene la forma de un token nuestro, desconocido o revocado: 401, y la petición ni
   siquiera llega al servidor MCP (por tanto, antes de ejecutar nada).
2. Los tokens se comparan por hash: en la base solo vive el sha256 (migración 0023).
   La búsqueda es por igualdad sobre un índice único del hash, no una comparación de
   cadenas en Python, así que no hay temporización útil que medir sobre el token.
3. Cada `tools/call` cuenta contra el tope diario del token (`mcp_llamadas_por_dia`,
   día en la zona de `zona_horaria`): pasado el tope, 429. El contador es un solo UPDATE
   condicional, así que dos peticiones a la vez no se cuelan por encima. Las demás
   peticiones del protocolo (initialize, tools/list, ping...) no gastan tope, pero sí
   exigen token y sí actualizan `ultimo_uso_at`.
4. Un token revocado deja de valer en la petición siguiente: se mira `revocado_at` en
   cada petición, sin caché.

Nunca se registra el token: ni en los logs, ni en el ledger, ni en los mensajes de
error. Solo el correo de su dueño y su sufijo (4 últimos caracteres).

Constancia de uso, sin ruido: `agent_actions` recibe UNA fila por sesión, entendida
como la primera llamada a una herramienta que hace un token en el día
(`mcp_sesion`, con `{"email", "sufijo", "herramienta"}`), no una por llamada. Una
persona preguntando veinte cosas seguidas deja una fila, no veinte; lo que sí queda
exacto por token en la tabla es `usos_hoy`. El MCP es sin estado (cada petición es
independiente), así que "sesión" no puede ser la del protocolo: el día es la ventana.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import db, db_config, ledger, mcp_identidad

logger = logging.getLogger(__name__)

# El token que genera la migración 0023: 32 bytes en hexadecimal. Rechazar cualquier
# otra cosa antes de tocar la base evita consultas por basura.
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
ACCION_SESION = "mcp_sesion"
TOPE_POR_DEFECTO = 500
# Un mensaje JSON-RPC de una consulta es pequeño; esto corta cuerpos absurdos antes de
# guardarlos en memoria para mirar el método.
CUERPO_MAXIMO = 1_000_000


@dataclass(frozen=True)
class TokenMCP:
    id: str
    email: str
    sufijo: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_de_cabecera(valor: str | None) -> str | None:
    """El token de `Bearer <token>`, o None si falta o no tiene la forma de uno."""
    if not valor:
        return None
    esquema, _, resto = valor.partition(" ")
    token = resto.strip()
    if esquema.lower() != "bearer" or not TOKEN_RE.match(token):
        return None
    return token


def autenticar(token: str) -> TokenMCP | None:
    fila = db.fetch_one(
        "select id, email, sufijo from mcp_tokens where token_hash = %s and revocado_at is null",
        (hash_token(token),),
    )
    return TokenMCP(str(fila["id"]), fila["email"], fila["sufijo"]) if fila else None


def _hoy() -> str:
    return mcp_identidad.hoy()


# Resultados de `registrar_uso`.
OK, TOPE, REVOCADO = "ok", "tope", "revocado"


def registrar_uso(token: TokenMCP, llamadas: int, herramienta: str | None = None) -> str:
    """Anota el uso y aplica el tope. `llamadas` es cuántos `tools/call` trae la petición.

    Todo ocurre en un único UPDATE condicional: reinicia el contador si cambió el día,
    suma, y solo se aplica si el token sigue sin revocar y no se pasa del tope.
    """
    if llamadas == 0:
        filas = db.execute(
            "update mcp_tokens set ultimo_uso_at = now() where id = %s and revocado_at is null",
            (token.id,),
        )
        return OK if filas else REVOCADO

    hoy = _hoy()
    tope = db_config.value("mcp_llamadas_por_dia", TOPE_POR_DEFECTO)
    fila = db.fetch_one(
        """
        update mcp_tokens
           set usos_hoy = (case when dia_uso is distinct from %(hoy)s::date
                                then 0 else usos_hoy end) + %(n)s,
               dia_uso = %(hoy)s::date,
               ultimo_uso_at = now()
         where id = %(id)s
           and revocado_at is null
           and (case when dia_uso is distinct from %(hoy)s::date
                     then 0 else usos_hoy end) + %(n)s <= %(tope)s
        returning usos_hoy
        """,
        {"hoy": hoy, "n": llamadas, "id": token.id, "tope": tope},
    )
    if fila is None:
        sigue = db.fetch_one(
            "select 1 as ok from mcp_tokens where id = %s and revocado_at is null", (token.id,)
        )
        return TOPE if sigue else REVOCADO
    if fila["usos_hoy"] == llamadas:
        # Antes de esta petición el contador estaba a cero: primera llamada del día.
        _dejar_constancia(token, herramienta)
    return OK


def _dejar_constancia(token: TokenMCP, herramienta: str | None) -> None:
    try:
        ledger.record_action(
            ACCION_SESION,
            {"email": token.email, "sufijo": token.sufijo, "herramienta": herramienta},
        )
    except Exception as exc:  # noqa: BLE001 - la constancia nunca debe tumbar la consulta
        logger.warning("mcp: no se pudo anotar la sesión: %s", type(exc).__name__)


def _llamadas_a_herramientas(cuerpo: bytes) -> tuple[int, str | None]:
    """Cuántos mensajes JSON-RPC `tools/call` trae el cuerpo y el nombre del primero.
    Un cuerpo ilegible cuenta cero: lo rechazará el servidor MCP, pero ya con token."""
    try:
        datos = json.loads(cuerpo)
    except ValueError:
        return 0, None
    mensajes = datos if isinstance(datos, list) else [datos]
    nombres = []
    for mensaje in mensajes:
        if isinstance(mensaje, dict) and mensaje.get("method") == "tools/call":
            params = mensaje.get("params")
            nombre = params.get("name") if isinstance(params, dict) else None
            nombres.append(nombre if isinstance(nombre, str) else None)
    return len(nombres), (nombres[0] if nombres else None)


def _rechazo(status: int, mensaje: str) -> JSONResponse:
    cabeceras = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"error": mensaje}, status_code=status, headers=cabeceras)


async def _leer_cuerpo(receive: Receive) -> bytes | None:
    """El cuerpo entero, o None si pasa de `CUERPO_MAXIMO`."""
    partes: list[bytes] = []
    total = 0
    while True:
        mensaje = await receive()
        if mensaje["type"] != "http.request":
            return b"".join(partes)  # el cliente colgó: lo que haya, el servidor MCP decidirá
        trozo = mensaje.get("body", b"")
        total += len(trozo)
        if total > CUERPO_MAXIMO:
            return None
        partes.append(trozo)
        if not mensaje.get("more_body", False):
            return b"".join(partes)


class GuardiaMCP:
    """ASGI: autentica, aplica el tope y reenvía a la app MCP (`destino`)."""

    def __init__(self) -> None:
        # Lo pone el ciclo de vida de la app web (web/app.py) cuando el servidor MCP
        # está listo, y lo quita al apagar. Sin él, un token válido recibe 503.
        self.destino: ASGIApp | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        cabeceras = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        token = token_de_cabecera(cabeceras.get("authorization"))
        if token is None:
            await _rechazo(401, "missing or invalid token")(scope, receive, send)
            return
        try:
            autenticado = await run_in_threadpool(autenticar, token)
        except Exception as exc:  # noqa: BLE001 - nunca un 500 con detalles
            logger.error("mcp: fallo al autenticar: %s", type(exc).__name__)
            await _rechazo(503, "temporarily unavailable")(scope, receive, send)
            return
        if autenticado is None:
            logger.warning("mcp: token rechazado (desconocido o revocado)")
            await _rechazo(401, "missing or invalid token")(scope, receive, send)
            return

        cuerpo = None
        llamadas, herramienta = 0, None
        if scope["method"] == "POST":
            cuerpo = await _leer_cuerpo(receive)
            if cuerpo is None:
                await _rechazo(413, "request too large")(scope, receive, send)
                return
            llamadas, herramienta = _llamadas_a_herramientas(cuerpo)

        try:
            resultado = await run_in_threadpool(registrar_uso, autenticado, llamadas, herramienta)
        except Exception as exc:  # noqa: BLE001
            logger.error("mcp: fallo al registrar el uso: %s", type(exc).__name__)
            await _rechazo(503, "temporarily unavailable")(scope, receive, send)
            return
        if resultado == REVOCADO:
            await _rechazo(401, "missing or invalid token")(scope, receive, send)
            return
        if resultado == TOPE:
            logger.warning(
                "mcp: tope diario alcanzado (%s ...%s)", autenticado.email, autenticado.sufijo
            )
            await _rechazo(429, "daily call limit reached; try again tomorrow")(
                scope, receive, send
            )
            return
        if self.destino is None:
            await _rechazo(503, "temporarily unavailable")(scope, receive, send)
            return

        if cuerpo is not None:
            entregado = False

            async def reenviar() -> Message:
                nonlocal entregado
                if not entregado:
                    entregado = True
                    return {"type": "http.request", "body": cuerpo, "more_body": False}
                return await receive()

            receptor: Receive = reenviar
        else:
            receptor = receive
        # Quién llama: solo el correo y el sufijo, nunca el token. Lo leen las
        # herramientas de escritura (mcp_identidad); se retira al terminar la petición.
        with mcp_identidad.como(autenticado.email, autenticado.sufijo):
            await self.destino(scope, receptor, send)
