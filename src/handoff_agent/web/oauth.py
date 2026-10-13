"""Servidor de autorización OAuth 2.1 para el MCP remoto: conectar Claude sin llaves.

QUÉ RESUELVE. Hasta ahora, conectar Claude al MCP era entrar al panel, crear un token
(migración 0023), copiarlo y pegarlo en un JSON. Con esto, en Claude: *Add custom
connector* → pegar la URL → *Connect* → entrar con el correo y la contraseña del panel.
Los tokens estáticos siguen funcionando igual (la puerta acepta los dos tipos).

QUÉ IMPLEMENTA, Y DE DÓNDE SALE CADA PIEZA. La especificación de autorización de MCP
(revisión vigente 2026-07-28, consultada el 2026-10-13), que es un subconjunto de:

* OAuth 2.1 (draft-ietf-oauth-v2-1): PKCE obligatorio, clientes públicos sin secreto,
  códigos de un solo uso y de vida corta, refresh rotatorio.
* RFC 9728, Protected Resource Metadata: `/.well-known/oauth-protected-resource` (y la
  variante con la ruta del endpoint, §3), y la cabecera
  `WWW-Authenticate: Bearer resource_metadata="..."` del 401 (§5.1), que es lo que hace
  que el cliente arranque el flujo solo.
* RFC 8414, Authorization Server Metadata: `/.well-known/oauth-authorization-server`.
* RFC 7591, Dynamic Client Registration: `POST /oauth/register`. La revisión vigente de
  MCP lo marca como *deprecated* en favor de los Client ID Metadata Documents, pero lo
  mantiene por compatibilidad y es lo que usan hoy los clientes de Claude; se anuncia
  `client_id_metadata_document_supported: false` para que un cliente que soporte las dos
  cosas elija el registro dinámico sin intentar lo otro primero.
* RFC 7636, PKCE: solo `S256`. `plain` se rechaza, y sin `code_challenge` no se autoriza.
* RFC 8707, Resource Indicators: se acepta y se comprueba el parámetro `resource`.
* RFC 9207, `iss` en la respuesta de autorización (anunciado en los metadatos).
* RFC 7009, Token Revocation: `POST /oauth/revoke`.
* RFC 6750: el 401/403 del recurso y el formato del desafío.

QUIÉN DECIDE QUE ALGUIEN ENTRA: Supabase Auth, el mismo sitio que el panel, más estar en
`panel_users` (ver `oauth_login.py`). Este servidor no guarda contraseñas.

QUÉ NO HAY, A PROPÓSITO: ni consentimiento con casillas (el único scope es `mcp`: lo que
la persona ya puede hacer en el panel, ni más ni menos), ni client_secret (un cliente de
escritorio no puede guardarlo; lo que protege el canje es PKCE), ni JWT (los tokens son
opacos y se comprueban contra la base en cada petición, así que una revocación surte
efecto al instante en vez de dentro de 8 horas).

POR QUÉ NO HAY TOKEN CSRF en el formulario de login: no hay cookies ni sesión de
navegador en ningún punto de este flujo, así que no hay nada que un sitio ajeno pueda
hacer "en nombre de" quien mira. Lo único que el POST acepta es el id de una solicitud
pendiente, que es aleatorio y de un solo uso, así que tampoco se puede dirigir el envío a
la autorización de otra persona. Las cabeceras de la página (`frame-ancestors 'none'`,
`form-action 'self'`) cierran el resto.

SI FALTA `MCP_PUBLIC_URL`, el OAuth queda apagado: sin saber su propia URL pública este
servidor no puede publicar metadatos correctos, y sacarla de la cabecera `Host` sería
dejar que quien llama decida qué servidor de autorización se anuncia. En ese caso todas estas
rutas responden 503 y el MCP sigue funcionando con los tokens estáticos.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import re
import secrets
import uuid
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .. import ledger
from ..config import Settings, load_settings
from . import oauth_almacen as almacen
from . import oauth_login

logger = logging.getLogger(__name__)

router = APIRouter()

# El único scope. No hay permisos parciales: un token OAuth puede exactamente lo que
# puede la persona en el panel (lectura, y los cambios acotados de la migración 0024).
SCOPE = "mcp"

RUTA_MCP = "/mcp"
RUTA_AUTORIZAR = "/oauth/authorize"
RUTA_TOKEN = "/oauth/token"
RUTA_REGISTRO = "/oauth/register"
RUTA_REVOCAR = "/oauth/revoke"
METADATOS_RECURSO = "/.well-known/oauth-protected-resource"
METADATOS_SERVIDOR = "/.well-known/oauth-authorization-server"

# PKCE (RFC 7636 §4.1 y §4.2): el verifier y el reto son 43-128 caracteres del juego
# "unreserved". Un S256 en base64url sin relleno son exactamente 43.
PKCE_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
# Esquema de URI (RFC 3986 §3.1).
ESQUEMA_RE = re.compile(r"^[a-z][a-z0-9+.\-]*$")
# Un redirect_uri de loopback para apps de escritorio (RFC 8252 §7.3).
HOSTS_LOOPBACK = {"localhost", "127.0.0.1", "[::1]"}

MAXIMO_REDIRECTS = 5
LARGO_URI = 500
LARGO_STATE = 500
LARGO_NOMBRE_CLIENTE = 200
# Un registro de cliente es un JSON pequeño; esto corta cuerpos absurdos.
CUERPO_REGISTRO_MAXIMO = 20_000

GRANTS_SOPORTADOS = ("authorization_code", "refresh_token")

# Registrar un cliente es legítimo y se hace varias veces (cada persona que conecta su
# Claude, y de nuevo si lo reconecta), así que el freno es más holgado que el del login:
# empieza a cobrar espera al décimo registro por IP en una hora. Varias personas detrás
# de la misma salida a internet no deben estorbarse.
UMBRAL_REGISTROS = 10

ACCION_SESION = "oauth_sesion"

# Páginas y respuestas de token: nada de esto se guarda en ninguna caché, y la página de
# login no se puede meter en un iframe (ni ella ni nada suyo), que es la mitad de un
# clickjacking sobre un formulario de contraseña.
CABECERAS_PAGINA = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
CABECERAS_SIN_CACHE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

# Los dos documentos de metadatos son públicos por diseño (un cliente tiene que poder
# leerlos ANTES de tener credencial) y no llevan nada de nadie, así que se dejan leer
# desde cualquier origen: un cliente que corra dentro de una página lo necesita. Esto NO
# se pone en `/mcp` ni en `/oauth/token`, que sí llevan credenciales.
CABECERAS_METADATOS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"}


# --- identidades públicas de este servidor ---------------------------------------


def base_publica(settings: Settings | None = None) -> str | None:
    """`https://host` de este servicio, o None si no está configurado (OAuth apagado)."""
    return (settings or load_settings()).mcp_public_url


def recurso(base: str) -> str:
    """El identificador canónico del recurso protegido: la URL del MCP sin barra final."""
    return base + RUTA_MCP


def url_metadatos_recurso(base: str) -> str:
    """RFC 9728 §3: el sufijo va entre el host y la ruta del recurso."""
    return f"{base}{METADATOS_RECURSO}{RUTA_MCP}"


def desafio(base: str | None, error: str | None = None) -> str:
    """El valor de `WWW-Authenticate` del 401/403 del MCP (RFC 6750 §3, RFC 9728 §5.1).

    Sin `base` no hay metadatos que anunciar y queda el desafío de siempre (`Bearer`), que
    es lo que había antes del OAuth: el cliente pedirá un token estático a su dueño."""
    partes = ["Bearer"]
    campos = []
    if error:
        campos.append(f'error="{error}"')
    if base:
        campos.append(f'resource_metadata="{url_metadatos_recurso(base)}"')
        # La spec recomienda decir qué scope hace falta para no pedir de más.
        campos.append(f'scope="{SCOPE}"')
    if campos:
        partes.append(", ".join(campos))
    return " ".join(partes)


# --- metadatos -------------------------------------------------------------------


def _limpiar() -> None:
    """La limpieza de lo caducado va de paso (no hay cron), y nunca puede tumbar un login."""
    try:
        almacen.limpiar()
    except Exception as exc:  # noqa: BLE001
        logger.warning("oauth: no se pudo limpiar lo caducado: %s", type(exc).__name__)


def _apagado() -> JSONResponse:
    logger.error("oauth: MCP_PUBLIC_URL no está configurada; el OAuth está apagado")
    return JSONResponse(
        {"error": "temporarily_unavailable", "error_description": "OAuth is not configured"},
        status_code=503,
        headers=CABECERAS_SIN_CACHE,
    )


def _metadatos_recurso(base: str) -> dict:
    return {
        "resource": recurso(base),
        # RFC 9728 exige al menos uno. Aquí el servidor de autorización es este mismo
        # servicio, así que el emisor es su base (sin ruta: por eso los metadatos del
        # servidor están en la raíz).
        "authorization_servers": [base],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Handoff",
    }


@router.get(METADATOS_RECURSO)
@router.get(METADATOS_RECURSO + RUTA_MCP)
def metadatos_del_recurso() -> Response:
    """Los dos sitios donde la spec permite buscarlo: la raíz y la ruta del endpoint.

    Se sirven los dos con el mismo documento para no depender de cuál pruebe el cliente
    (la spec le obliga a soportar ambos, pero no dice cuál intenta primero)."""
    base = base_publica()
    if base is None:
        return _apagado()
    return JSONResponse(_metadatos_recurso(base), headers=CABECERAS_METADATOS)


@router.get(METADATOS_SERVIDOR)
def metadatos_del_servidor() -> Response:
    base = base_publica()
    if base is None:
        return _apagado()
    return JSONResponse(
        {
            # RFC 8414 §3.3: el cliente comprueba que esto es idéntico a la URL con la
            # que construyó la petición. Es la base, sin ruta ni barra final.
            "issuer": base,
            "authorization_endpoint": base + RUTA_AUTORIZAR,
            "token_endpoint": base + RUTA_TOKEN,
            "registration_endpoint": base + RUTA_REGISTRO,
            "revocation_endpoint": base + RUTA_REVOCAR,
            "response_types_supported": ["code"],
            "grant_types_supported": list(GRANTS_SOPORTADOS),
            # Solo S256: `plain` no protege nada (RFC 7636 §7.2).
            "code_challenge_methods_supported": ["S256"],
            # Clientes públicos: no hay secreto que presentar en /oauth/token.
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE],
            # RFC 9207: se manda `iss` en la redirección, así que hay que anunciarlo.
            "authorization_response_iss_parameter_supported": True,
            # No se resuelven client_id con forma de URL: el registro es dinámico.
            "client_id_metadata_document_supported": False,
        },
        headers=CABECERAS_METADATOS,
    )


# --- páginas ---------------------------------------------------------------------

_ESTILO = """
:root { color-scheme: light dark; }
body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
       margin: 0; padding: 24px; background: #f6f6f5; color: #1b1b1a; }
main { max-width: 380px; margin: 10vh auto; background: #fff; border: 1px solid #e3e3e0;
       border-radius: 10px; padding: 28px; }
h1 { font-size: 18px; margin: 0 0 4px; }
p { line-height: 1.5; }
.lead { margin: 0 0 20px; color: #4a4a46; font-size: 14px; }
label { display: block; font-size: 13px; color: #4a4a46; margin-bottom: 4px; }
input { width: 100%; box-sizing: border-box; padding: 9px 10px; font-size: 15px;
        border: 1px solid #c9c9c4; border-radius: 6px; background: #fff; color: #1b1b1a;
        margin-bottom: 14px; }
button { width: 100%; padding: 10px; font-size: 15px; border: 0; border-radius: 6px;
         background: #1b1b1a; color: #fff; cursor: pointer; }
.error { background: #fdf1f1; border: 1px solid #f0cfcf; color: #7a2020; padding: 10px;
         border-radius: 6px; font-size: 13px; margin: 0 0 14px; }
.muted { color: #76766f; font-size: 12px; margin: 16px 0 0; }
@media (prefers-color-scheme: dark) {
  body { background: #131312; color: #ececea; }
  main { background: #1b1b1a; border-color: #2f2f2d; }
  .lead, label { color: #a5a59e; }
  input { background: #131312; border-color: #3a3a37; color: #ececea; }
  button { background: #ececea; color: #131312; }
  .error { background: #2a1616; border-color: #5a2a2a; color: #f0b9b9; }
}
"""


def _pagina(titulo: str, cuerpo: str, status: int = 200) -> HTMLResponse:
    documento = (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(titulo)}</title>\n"
        f"<style>{_ESTILO}</style>\n</head>\n<body>\n<main>\n{cuerpo}\n</main>\n</body>\n</html>\n"
    )
    return HTMLResponse(documento, status_code=status, headers=CABECERAS_PAGINA)


def _pagina_login(
    solicitud: dict, error: str | None = None, email: str = "", status: int = 200
) -> HTMLResponse:
    """El formulario. Solo lleva el id de la solicitud: los parámetros de la autorización
    ya están validados y guardados, y no vuelven por aquí (ver `oauth_almacen`)."""
    nombre = solicitud.get("client_name") or "an application"
    aviso = f'<p class="error">{html.escape(error)}</p>\n' if error else ""
    cuerpo = (
        "<h1>Sign in to Handoff</h1>\n"
        f'<p class="lead">{html.escape(nombre)} is asking to connect to your Handoff '
        "workspace. Use the email and password you use for the panel.</p>\n"
        f"{aviso}"
        f'<form method="post" action="{RUTA_AUTORIZAR}">\n'
        f'<input type="hidden" name="request_id" value="{html.escape(str(solicitud["id"]))}">\n'
        '<label for="email">Email</label>\n'
        '<input id="email" name="email" type="email" required autocomplete="username"'
        f' autofocus value="{html.escape(email)}">\n'
        '<label for="password">Password</label>\n'
        '<input id="password" name="password" type="password" required'
        ' autocomplete="current-password">\n'
        '<button type="submit">Sign in</button>\n'
        "</form>\n"
        '<p class="muted">Only people on the team can connect. Once connected, this '
        "application can read what the panel shows and make the same limited changes you "
        "can; every change is recorded under your name. You can disconnect it at any time "
        "from Settings &rarr; Connect your Claude.</p>"
    )
    return _pagina("Sign in to Handoff", cuerpo, status)


def _pagina_error(mensaje: str, status: int = 400) -> HTMLResponse:
    """Para lo que NO se puede devolver al cliente por redirección: si el `client_id` o el
    `redirect_uri` no son de fiar, redirigir sería un redirector abierto (OAuth 2.1 §7.5)."""
    cuerpo = (
        "<h1>Can&rsquo;t connect</h1>\n"
        f'<p class="lead">{html.escape(mensaje)}</p>\n'
        '<p class="muted">Nothing was shared. Close this window and try connecting '
        "again from your assistant.</p>"
    )
    return _pagina("Can't connect", cuerpo, status)


# --- registro dinámico de cliente (RFC 7591) -------------------------------------


def _error_registro(error: str, descripcion: str) -> JSONResponse:
    """RFC 7591 §3.2.2: 400 con `error` y `error_description`."""
    return JSONResponse(
        {"error": error, "error_description": descripcion},
        status_code=400,
        headers=CABECERAS_SIN_CACHE,
    )


def redirect_valida(uri: object) -> bool:
    """Las únicas formas aceptables de volver al cliente.

    Tres familias, las de OAuth 2.1 y RFC 8252 para clientes públicos: `https`, `http`
    solo en loopback (una app de escritorio que abre un puerto local), y un esquema
    propio con punto en el nombre (`com.ejemplo.app:/cb`, RFC 8252 §7.1). Nunca `http`
    hacia internet, y nunca con fragmento (RFC 6749 §3.1.2)."""
    if not isinstance(uri, str) or not 0 < len(uri) <= LARGO_URI:
        return False
    try:
        partes = urlsplit(uri)
    except ValueError:
        return False
    if partes.fragment or "#" in uri:
        return False
    esquema = partes.scheme.lower()
    if not ESQUEMA_RE.match(esquema):
        return False
    if esquema == "https":
        return bool(partes.hostname)
    if esquema == "http":
        return partes.hostname is not None and partes.hostname.lower() in HOSTS_LOOPBACK
    # Esquema propio: tiene que parecer un dominio invertido y llevar algo detrás.
    return "." in esquema and bool(partes.path or partes.netloc)


def _lista_de_textos(valor: object) -> list[str] | None:
    if not isinstance(valor, list) or not valor:
        return None
    return [v for v in valor if isinstance(v, str)] or None


def _registrar(cuerpo: dict, ip: str) -> JSONResponse:
    espera = almacen.espera_restante(f"reg:{ip}")
    if espera:
        return JSONResponse(
            {
                "error": "temporarily_unavailable",
                "error_description": "Too many registrations; try again later",
            },
            status_code=429,
            headers={**CABECERAS_SIN_CACHE, "Retry-After": str(espera)},
        )

    redirects = _lista_de_textos(cuerpo.get("redirect_uris"))
    if redirects is None:
        return _error_registro("invalid_redirect_uri", "redirect_uris is required")
    if len(redirects) > MAXIMO_REDIRECTS:
        return _error_registro("invalid_redirect_uri", f"at most {MAXIMO_REDIRECTS} redirect_uris")
    for uri in redirects:
        if not redirect_valida(uri):
            # No se repite la URI en el mensaje: va a los logs del cliente tal cual.
            return _error_registro(
                "invalid_redirect_uri",
                "every redirect_uri must be https, http on loopback, or a private-use "
                "scheme, and must not contain a fragment",
            )

    # Solo clientes públicos: no se emite ningún client_secret, así que un cliente que
    # pida autenticarse con uno no podría usarlo (RFC 7591 §2 "token_endpoint_auth_method").
    metodo = cuerpo.get("token_endpoint_auth_method")
    if metodo not in (None, "none"):
        return _error_registro(
            "invalid_client_metadata",
            "only public clients are supported: token_endpoint_auth_method must be none",
        )

    tipos = _lista_de_textos(cuerpo.get("response_types")) or ["code"]
    if "code" not in tipos:
        return _error_registro("invalid_client_metadata", "response_types must include code")

    pedidos = _lista_de_textos(cuerpo.get("grant_types")) or ["authorization_code"]
    if "authorization_code" not in pedidos:
        return _error_registro(
            "invalid_client_metadata", "grant_types must include authorization_code"
        )
    # Se conceden siempre los dos: `/oauth/token` emite un refresh con cada access, así
    # que anunciar menos sería describir algo que no es. Lo que el cliente pidiera de más
    # (client_credentials y compañía) no se concede ni se guarda como concedido.
    grants = list(GRANTS_SOPORTADOS)

    nombre = cuerpo.get("client_name")
    nombre = nombre.strip()[:LARGO_NOMBRE_CLIENTE] if isinstance(nombre, str) else None

    # Lo que pidió el cliente, para poder depurar. Recortado, y sin nada que no sea JSON.
    metadata = {
        k: v
        for k, v in cuerpo.items()
        if k in {"client_uri", "logo_uri", "application_type", "scope", "software_id"}
        and isinstance(v, str)
    }

    fila = almacen.registrar_cliente(nombre, redirects, grants, metadata)
    almacen.anotar_intento(f"reg:{ip}", UMBRAL_REGISTROS)
    _limpiar()
    logger.info("oauth: cliente registrado (%s)", fila["client_id"])
    # RFC 7591 §3.2.1: 201 con los metadatos tal como quedaron registrados.
    return JSONResponse(
        {
            "client_id": fila["client_id"],
            "client_id_issued_at": int(fila["creado_at"].timestamp()),
            "client_name": fila["client_name"],
            "redirect_uris": list(fila["redirect_uris"]),
            "grant_types": list(fila["grant_types"]),
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        },
        status_code=201,
        headers=CABECERAS_SIN_CACHE,
    )


@router.post(RUTA_REGISTRO)
async def registrar(request: Request) -> Response:
    if base_publica() is None:
        return _apagado()
    crudo = await request.body()
    if len(crudo) > CUERPO_REGISTRO_MAXIMO:
        return _error_registro("invalid_client_metadata", "request too large")
    try:
        cuerpo = json.loads(crudo)
    except ValueError:
        return _error_registro("invalid_client_metadata", "the body must be JSON")
    if not isinstance(cuerpo, dict):
        return _error_registro("invalid_client_metadata", "the body must be a JSON object")
    return await run_in_threadpool(_registrar, cuerpo, ip_de(request))


# --- autorización ----------------------------------------------------------------


def ip_de(request: Request) -> str:
    """La IP del cliente para el límite de intentos.

    Detrás del proxy de Railway la IP del socket es la del proxy, así que se usa el primer
    salto de `X-Forwarded-For`. Ese valor lo puede inventar quien llama, así que el freno
    que de verdad protege una cuenta es el de por correo; este solo encarece el barrido."""
    reenviada = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    directa = request.client.host if request.client else ""
    return (reenviada or directa or "desconocida")[:100]


def _redirigir(base: str, destino: str, params: dict[str, str]) -> RedirectResponse:
    separador = "&" if urlsplit(destino).query else "?"
    # RFC 9207: el emisor va en toda respuesta de autorización, también en los errores.
    url = destino + separador + urlencode({**params, "iss": base})
    return RedirectResponse(url, status_code=302, headers=CABECERAS_SIN_CACHE)


def _error_redirigido(
    base: str, destino: str, error: str, descripcion: str, state: str | None
) -> RedirectResponse:
    params = {"error": error, "error_description": descripcion}
    if state is not None:
        params["state"] = state
    return _redirigir(base, destino, params)


@router.get(RUTA_AUTORIZAR)
def autorizar(request: Request) -> Response:
    """Valida la petición y pinta el login. No toca credenciales: eso es el POST.

    El orden de las comprobaciones es la parte que importa. Mientras no se sabe que el
    `client_id` y el `redirect_uri` son de los registrados, NADA se devuelve por
    redirección: hacerlo convertiría este endpoint en un redirector abierto que presta
    la credibilidad del dominio (OAuth 2.1 §7.5). Solo cuando el destino está
    comprobado, los demás errores sí vuelven al cliente como manda el protocolo."""
    base = base_publica()
    if base is None:
        return _pagina_error("This server is not set up to connect assistants yet.", 503)

    params = request.query_params
    client_id = params.get("client_id") or ""
    cliente = almacen.cliente(client_id) if client_id else None
    if cliente is None:
        logger.warning("oauth: autorización con un client_id desconocido")
        return _pagina_error("This application is not registered with Handoff.")

    destino = params.get("redirect_uri") or ""
    # Comparación exacta contra las registradas: ni comodines, ni prefijos, ni puertos
    # distintos (OAuth 2.1 §4.1.2.1 y §7.5).
    if destino not in (cliente["redirect_uris"] or []):
        logger.warning("oauth: redirect_uri que no está registrada para el cliente")
        return _pagina_error(
            "The address this application asked us to return to is not one it registered."
        )

    state = params.get("state")
    if state is not None and len(state) > LARGO_STATE:
        return _error_redirigido(base, destino, "invalid_request", "state is too long", None)

    if params.get("response_type") != "code":
        return _error_redirigido(
            base, destino, "unsupported_response_type", "only response_type=code", state
        )

    reto = params.get("code_challenge") or ""
    metodo = params.get("code_challenge_method")
    if not reto:
        # Sin PKCE no se autoriza: es el único freno de un cliente público contra el robo
        # del código (RFC 7636 §1, OAuth 2.1 §7.5.1).
        return _error_redirigido(
            base, destino, "invalid_request", "code_challenge is required (PKCE S256)", state
        )
    if metodo != "S256":
        return _error_redirigido(
            base, destino, "invalid_request", "code_challenge_method must be S256", state
        )
    if not PKCE_RE.match(reto):
        return _error_redirigido(
            base, destino, "invalid_request", "malformed code_challenge", state
        )

    pedido = params.get("scope")
    if pedido is not None and set(pedido.split()) - {SCOPE}:
        return _error_redirigido(
            base, destino, "invalid_scope", f"the only scope is {SCOPE}", state
        )

    # RFC 8707: si el cliente dice para qué recurso quiere el token, tiene que ser este.
    pedido_recurso = params.get("resource")
    if pedido_recurso is not None and not recurso_valido(base, pedido_recurso):
        return _error_redirigido(base, destino, "invalid_target", "unknown resource", state)

    solicitud_id = almacen.crear_solicitud(client_id, destino, reto, SCOPE, pedido_recurso, state)
    _limpiar()
    return _pagina_login({"id": solicitud_id, "client_name": cliente["client_name"]})


def recurso_valido(base: str, pedido: str) -> bool:
    """El `resource` que manda el cliente apunta a este MCP.

    Se aceptan la forma canónica y las dos variantes inofensivas que mandan los clientes
    (con barra final, o la base sin `/mcp`); cualquier otra cosa es un token pedido para
    otro servidor y se rechaza, que es justo lo que RFC 8707 viene a evitar."""
    limpio = pedido.rstrip("/").lower()
    return limpio in {recurso(base).lower(), base.lower()}


def _login(solicitud_id: str, email: str, password: str, ip: str, settings: Settings) -> Response:
    """El POST del formulario, ya fuera del bucle de eventos. Devuelve página o redirección."""
    base = settings.mcp_public_url
    if base is None:
        return _pagina_error("This server is not set up to connect assistants yet.", 503)

    # El id tiene que ser un uuid antes de llegar a la base: preguntar por basura sería
    # un error de tipo de Postgres, y eso es un 500, no un "vuelve a empezar".
    try:
        solicitud_id = str(uuid.UUID(solicitud_id))
    except (ValueError, AttributeError, TypeError):
        solicitud_id = ""
    solicitud = almacen.solicitud_pendiente(solicitud_id) if solicitud_id else None
    if solicitud is None:
        # Caducada (10 minutos), ya usada, o un id inventado. No se dice cuál.
        return _pagina_error("This sign-in has expired. Start again from your assistant.", 400)

    claves = [f"email:{email[:200]}", f"ip:{ip}"] if email else [f"ip:{ip}"]
    espera = max(almacen.espera_restante(clave) for clave in claves)
    if espera:
        logger.warning("oauth: login bloqueado por demasiados intentos")
        return _pagina_login(
            solicitud,
            f"Too many attempts. Try again in {espera} seconds.",
            email,
            status=429,
        )

    resultado, correo = oauth_login.verificar(settings, email, password)
    if resultado == oauth_login.NO_DISPONIBLE:
        return _pagina_login(
            solicitud, "We can't check your sign-in right now. Try again.", email, status=503
        )
    if resultado != oauth_login.OK or correo is None:
        bloqueo = max(almacen.anotar_intento(clave) for clave in claves)
        # El mismo mensaje para "la contraseña no es", "ese correo no existe" y "existe
        # pero no está en el equipo": si no, esta página diría quién tiene cuenta.
        mensaje = "Incorrect email or password, or this account can't connect."
        if bloqueo:
            mensaje += f" Too many attempts: wait {bloqueo} seconds."
        return _pagina_login(solicitud, mensaje, email, status=401)

    almacen.olvidar_intentos(claves)
    codigo = almacen.emitir_codigo(str(solicitud["id"]), correo)
    if codigo is None:
        # Alguien envió el formulario dos veces a la vez: solo uno emite código.
        return _pagina_error("This sign-in has already been used. Start again.", 400)

    logger.info("oauth: login correcto de %s para el cliente %s", correo, solicitud["client_id"])
    params = {"code": codigo}
    if solicitud["state"] is not None:
        # Se devuelve tal cual, sin interpretarlo: es del cliente, no nuestro.
        params["state"] = solicitud["state"]
    return _redirigir(base, solicitud["redirect_uri"], params)


@router.post(RUTA_AUTORIZAR)
async def autorizar_login(request: Request) -> Response:
    formulario = await request.form()
    solicitud_id = str(formulario.get("request_id") or "")
    email = str(formulario.get("email") or "").strip().lower()[:320]
    password = str(formulario.get("password") or "")
    # La contraseña no se vuelve a tocar después de esta línea: va al verificador y
    # desaparece con la petición. No entra en ningún log ni en ninguna respuesta.
    return await run_in_threadpool(
        _login, solicitud_id, email, password, ip_de(request), load_settings()
    )


# --- tokens ----------------------------------------------------------------------


def _error_token(error: str, descripcion: str, status: int = 400) -> JSONResponse:
    """RFC 6749 §5.2 / OAuth 2.1 §4.1.4: error en JSON y sin caché."""
    return JSONResponse(
        {"error": error, "error_description": descripcion},
        status_code=status,
        headers=CABECERAS_SIN_CACHE,
    )


def verifica_pkce(reto: str, verifier: str) -> bool:
    """RFC 7636 §4.6: S256 = BASE64URL(SHA256(ASCII(code_verifier))), sin relleno."""
    if not PKCE_RE.match(verifier):
        return False
    calculado = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return secrets.compare_digest(calculado, reto)


def _respuesta_tokens(access: str, refresh: str) -> JSONResponse:
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": almacen.VIDA_ACCESS_SEGUNDOS,
            "refresh_token": refresh,
            "scope": SCOPE,
        },
        headers=CABECERAS_SIN_CACHE,
    )


def _token_por_codigo(datos: dict) -> JSONResponse:
    codigo = datos.get("code") or ""
    client_id = datos.get("client_id") or ""
    verifier = datos.get("code_verifier") or ""
    if not codigo or not client_id or not verifier:
        return _error_token("invalid_request", "code, client_id and code_verifier are required")

    resultado, solicitud = almacen.canjear_codigo(codigo)
    if resultado == "reusado":
        # El código ya se había canjeado: o lo robaron, o el cliente lo reenvió. No se
        # puede distinguir, así que se corta la sesión que salió de él (ya lo hizo el
        # almacén) y no se emite nada.
        logger.warning("oauth: código reusado; la sesión que salió de él queda revocada")
        return _error_token("invalid_grant", "this code has already been used")
    if resultado != "ok" or solicitud is None:
        return _error_token("invalid_grant", "unknown or expired code")

    # El código está ligado a su cliente, a su redirect_uri y a su reto PKCE: los tres se
    # comparan aquí. Un código robado no sirve en otro cliente ni sin el verifier.
    if client_id != solicitud["client_id"]:
        logger.warning("oauth: código presentado por otro cliente")
        return _error_token("invalid_grant", "this code was not issued to this client")
    destino = datos.get("redirect_uri")
    if destino is None or destino != solicitud["redirect_uri"]:
        return _error_token("invalid_grant", "redirect_uri does not match")
    if not verifica_pkce(solicitud["code_challenge"], verifier):
        logger.warning("oauth: code_verifier que no corresponde al reto")
        return _error_token("invalid_grant", "code_verifier does not match the challenge")
    pedido_recurso = datos.get("resource")
    base = base_publica()
    if pedido_recurso and base and not recurso_valido(base, pedido_recurso):
        return _error_token("invalid_target", "unknown resource")

    email = solicitud["email"]
    if not oauth_login.es_del_panel(email):
        # Entre el login y el canje pasa un instante, pero si en ese instante alguien
        # salió del equipo, no se emite nada.
        return _error_token("invalid_grant", "this account can no longer connect")

    sesion_id = almacen.crear_sesion(
        str(solicitud["id"]), email, solicitud["client_id"], solicitud["scope"]
    )
    access, refresh = almacen.emitir_tokens(sesion_id)
    try:
        ledger.record_action(
            ACCION_SESION,
            {"email": email, "cliente": solicitud["client_id"], "sesion": sesion_id},
        )
    except Exception as exc:  # noqa: BLE001 - la constancia nunca tumba el login
        logger.warning("oauth: no se pudo anotar la sesión: %s", type(exc).__name__)
    logger.info("oauth: sesión nueva de %s", email)
    return _respuesta_tokens(access, refresh)


def _token_por_refresh(datos: dict) -> JSONResponse:
    refresh = datos.get("refresh_token") or ""
    client_id = datos.get("client_id") or ""
    if not refresh or not client_id:
        return _error_token("invalid_request", "refresh_token and client_id are required")

    resultado, sesion = almacen.canjear_refresh(refresh)
    if resultado == "reusado":
        logger.warning("oauth: refresh reusado; la sesión queda revocada")
        return _error_token("invalid_grant", "this refresh token has already been used")
    if resultado != "ok" or sesion is None:
        return _error_token("invalid_grant", "unknown or expired refresh token")
    if client_id != sesion["client_id"]:
        logger.warning("oauth: refresh presentado por otro cliente")
        return _error_token("invalid_grant", "this refresh token was not issued to this client")
    if not oauth_login.es_del_panel(sesion["email"]):
        almacen.revocar_sesion(str(sesion["id"]))
        return _error_token("invalid_grant", "this account can no longer connect")

    access, nuevo_refresh = almacen.emitir_tokens(str(sesion["id"]))
    return _respuesta_tokens(access, nuevo_refresh)


@router.post(RUTA_TOKEN)
async def token(request: Request) -> Response:
    if base_publica() is None:
        return _apagado()
    formulario = await request.form()
    datos = {k: str(v) for k, v in formulario.items() if isinstance(v, str)}
    grant = datos.get("grant_type")
    if grant == "authorization_code":
        return await run_in_threadpool(_token_por_codigo, datos)
    if grant == "refresh_token":
        return await run_in_threadpool(_token_por_refresh, datos)
    return _error_token("unsupported_grant_type", "use authorization_code or refresh_token")


# --- revocación (RFC 7009) -------------------------------------------------------


def _revocar(valor: str) -> JSONResponse:
    try:
        almacen.revocar_por_token(valor)
    except Exception as exc:  # noqa: BLE001 - nunca un 500 con detalles
        logger.error("oauth: fallo al revocar: %s", type(exc).__name__)
        return JSONResponse(
            {"error": "temporarily_unavailable"}, status_code=503, headers=CABECERAS_SIN_CACHE
        )
    # RFC 7009 §2.2: 200 también si el token no existía. Decir "no existe" sería un oráculo
    # para comprobar tokens robados.
    return JSONResponse({}, headers=CABECERAS_SIN_CACHE)


@router.post(RUTA_REVOCAR)
async def revocar(request: Request) -> Response:
    if base_publica() is None:
        return _apagado()
    formulario = await request.form()
    valor = str(formulario.get("token") or "")
    if not valor:
        return _error_token("invalid_request", "token is required")
    return await run_in_threadpool(_revocar, valor)
