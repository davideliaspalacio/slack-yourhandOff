"""OAuth del MCP remoto: el flujo completo y, sobre todo, lo que NO debe poder hacerse.

Esto expone un formulario de contraseña a internet, así que la mayoría de estos tests son
sobre lo que tiene que fallar: PKCE obligatorio, redirect_uri exacta, códigos de un solo
uso y de vida corta, refresh rotatorio, límite de intentos, y nada de secretos en los
logs ni en las respuestas.

Supabase Auth no se llama de verdad (respx simula GoTrue), y los tokens estáticos de 0023
tienen que seguir funcionando exactamente igual: lo vigila `tests/test_mcp_auth.py` entero
más `test_a_static_token_and_an_oauth_token_work_side_by_side` de aquí.
"""

import base64
import hashlib
import json
import logging
import re
import secrets

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from handoff_agent import db
from handoff_agent.web import app as web_app
from handoff_agent.web import oauth
from handoff_agent.web import oauth_almacen as almacen
from tests.test_panel_rls import as_user

ANA = "ana@example.com"
BEA = "bea@example.com"
EXTRANO = "intruso@example.com"
PASSWORD = "no-es-la-de-nadie"

BASE = "https://handoff.example"
RECURSO = BASE + "/mcp"
SUPA = "https://supa.example"
SUPA_HOST = "supa.example"
CALLBACK = "https://claude.example/api/mcp/auth_callback"

HEADERS_MCP = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
ID_RE = re.compile(r'name="request_id" value="([^"]+)"')


# --- fixtures y ayudantes --------------------------------------------------------


@pytest.fixture
def equipo(conn):
    for email in (ANA, BEA):
        db.execute("insert into panel_users (email) values (%s)", (email,))


@pytest.fixture
def entorno(monkeypatch, equipo):
    """El OAuth encendido: la URL pública de este servicio y el Supabase simulado."""
    monkeypatch.setenv("MCP_PUBLIC_URL", BASE)
    monkeypatch.setenv("SUPABASE_URL", SUPA)
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_de_prueba")


@pytest.fixture
def cli(entorno):
    # Sin seguir redirecciones: la redirección ES lo que se comprueba.
    with TestClient(web_app.app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def apagado(monkeypatch, equipo):
    """Sin MCP_PUBLIC_URL: el OAuth no existe y el MCP sigue con tokens estáticos."""
    monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
    with TestClient(web_app.app, follow_redirects=False) as c:
        yield c


def gotrue(email=ANA, status=200):
    """La respuesta de Supabase Auth al login por contraseña."""
    cuerpo = (
        {"access_token": "jwt-de-supabase", "token_type": "bearer", "user": {"email": email}}
        if status == 200
        else {"error_code": "invalid_credentials", "msg": "Invalid login credentials"}
    )
    return respx.route(method="POST", host=SUPA_HOST, path="/auth/v1/token").mock(
        return_value=httpx.Response(status, json=cuerpo)
    )


def pkce():
    verifier = secrets.token_urlsafe(32)
    reto = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, reto


def registrar(cli, redirect_uris=(CALLBACK,), **extra):
    return cli.post(
        "/oauth/register",
        json={"redirect_uris": list(redirect_uris), "client_name": "Claude", **extra},
    )


def client_id(cli, **kwargs):
    respuesta = registrar(cli, **kwargs)
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()["client_id"]


def autorizar(cli, cid, reto, **cambios):
    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": CALLBACK,
        "code_challenge": reto,
        "code_challenge_method": "S256",
        "state": "xyz-123",
        "resource": RECURSO,
        **cambios,
    }
    params = {k: v for k, v in params.items() if v is not None}
    return cli.get("/oauth/authorize", params=params)


def id_de_solicitud(pagina):
    encontrado = ID_RE.search(pagina.text)
    assert encontrado, pagina.text[:400]
    return encontrado.group(1)


def entrar(cli, solicitud_id, email=ANA, password=PASSWORD):
    return cli.post(
        "/oauth/authorize",
        data={"request_id": solicitud_id, "email": email, "password": password},
    )


def codigo_de(redireccion):
    assert redireccion.status_code == 302, redireccion.text[:400]
    return dict(httpx.URL(redireccion.headers["location"]).params)


def canjear(cli, cid, codigo, verifier, **cambios):
    datos = {
        "grant_type": "authorization_code",
        "code": codigo,
        "client_id": cid,
        "redirect_uri": CALLBACK,
        "code_verifier": verifier,
        "resource": RECURSO,
        **cambios,
    }
    return cli.post("/oauth/token", data={k: v for k, v in datos.items() if v is not None})


def conectar(cli, email=ANA):
    """El flujo entero de un usuario, tal como lo hace el cliente: registro, autorización,
    login, canje. Devuelve `(client_id, verifier, respuesta del token)`."""
    cid = client_id(cli)
    verifier, reto = pkce()
    with respx.mock:
        gotrue(email)
        pagina = autorizar(cli, cid, reto)
        assert pagina.status_code == 200
        redireccion = entrar(cli, id_de_solicitud(pagina), email)
    codigo = codigo_de(redireccion)["code"]
    return cid, verifier, canjear(cli, cid, codigo, verifier)


def llamada_mcp(cli, token):
    return cli.post(
        "/mcp",
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "resumen_costes", "arguments": {}},
            }
        ),
        headers={**HEADERS_MCP, "Authorization": f"Bearer {token}"},
    )


# --- metadatos (RFC 9728 y RFC 8414) ---------------------------------------------


@pytest.mark.parametrize(
    "ruta", ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"]
)
def test_the_resource_metadata_is_served_where_the_spec_looks_for_it(cli, ruta):
    datos = cli.get(ruta).json()
    assert datos["resource"] == RECURSO
    assert datos["authorization_servers"] == [BASE]
    assert datos["scopes_supported"] == ["mcp"]
    assert datos["bearer_methods_supported"] == ["header"]


def test_the_authorization_server_metadata_describes_what_is_implemented(cli):
    datos = cli.get("/.well-known/oauth-authorization-server").json()
    # RFC 8414 §3.3: el cliente rechaza el documento si el emisor no es idéntico.
    assert datos["issuer"] == BASE
    assert datos["authorization_endpoint"] == BASE + "/oauth/authorize"
    assert datos["token_endpoint"] == BASE + "/oauth/token"
    assert datos["registration_endpoint"] == BASE + "/oauth/register"
    assert datos["revocation_endpoint"] == BASE + "/oauth/revoke"
    assert datos["code_challenge_methods_supported"] == ["S256"]
    assert datos["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert datos["token_endpoint_auth_methods_supported"] == ["none"]
    assert datos["authorization_response_iss_parameter_supported"] is True


def test_the_401_of_the_mcp_points_at_the_metadata(cli):
    respuesta = cli.post("/mcp", content="{}", headers=HEADERS_MCP)
    assert respuesta.status_code == 401
    desafio = respuesta.headers["www-authenticate"]
    assert desafio.startswith("Bearer ")
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in desafio
    assert 'scope="mcp"' in desafio
    # Y esa URL existe de verdad: es el primer paso del cliente.
    assert cli.get("/.well-known/oauth-protected-resource/mcp").status_code == 200


def test_a_credential_that_does_not_work_says_invalid_token(cli):
    respuesta = cli.post(
        "/mcp", content="{}", headers={**HEADERS_MCP, "Authorization": "Bearer " + "a" * 64}
    )
    assert respuesta.status_code == 401
    assert 'error="invalid_token"' in respuesta.headers["www-authenticate"]


def test_without_the_public_url_oauth_is_off_and_static_tokens_still_work(apagado, cli_estatico):
    assert apagado.get("/.well-known/oauth-authorization-server").status_code == 503
    assert apagado.get("/.well-known/oauth-protected-resource").status_code == 503
    assert registrar(apagado).status_code == 503
    assert apagado.post("/oauth/token", data={"grant_type": "refresh_token"}).status_code == 503
    # El desafío vuelve a ser el de antes del OAuth, sin metadatos que anunciar.
    respuesta = apagado.post("/mcp", content="{}", headers=HEADERS_MCP)
    assert respuesta.headers["www-authenticate"] == "Bearer"
    # Y lo que ya funcionaba sigue funcionando: un token estático del panel.
    assert llamada_mcp(apagado, cli_estatico).status_code == 200


@pytest.fixture
def cli_estatico(equipo):
    """Un token estático de los de siempre (0023)."""
    return as_user(ANA, "select public.panel_crear_token_mcp('Claude de Ana')")[0][
        "panel_crear_token_mcp"
    ]


# --- registro dinámico de cliente (RFC 7591) -------------------------------------


def test_registering_a_client_needs_nothing_and_returns_no_secret(cli):
    respuesta = registrar(cli)
    assert respuesta.status_code == 201
    datos = respuesta.json()
    assert datos["client_id"].startswith("cli_")
    assert datos["redirect_uris"] == [CALLBACK]
    assert datos["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in datos
    assert datos["grant_types"] == ["authorization_code", "refresh_token"]
    assert respuesta.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "uris",
    [
        ["http://claude.example/cb"],  # http fuera de loopback
        ["https://claude.example/cb#frag"],  # con fragmento (RFC 6749 §3.1.2)
        ["javascript:alert(1)"],
        ["/relativa"],
        [""],
        ["x" * 600],
        [f"https://c.example/{i}" for i in range(6)],  # más de las permitidas
    ],
)
def test_bad_redirect_uris_are_refused(cli, uris):
    respuesta = registrar(cli, redirect_uris=uris)
    assert respuesta.status_code == 400
    assert respuesta.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:51703/callback",
        "http://127.0.0.1:8976/oauth/callback",
        "https://claude.ai/api/mcp/auth_callback",
        "com.anthropic.claude:/oauth/callback",
    ],
)
def test_the_redirect_uris_real_clients_use_are_accepted(cli, uri):
    assert registrar(cli, redirect_uris=[uri]).status_code == 201


def test_a_client_asking_for_a_secret_or_the_wrong_grant_is_refused(cli):
    malo = registrar(cli, token_endpoint_auth_method="client_secret_post")
    assert malo.status_code == 400
    assert malo.json()["error"] == "invalid_client_metadata"
    assert registrar(cli, grant_types=["client_credentials"]).status_code == 400
    assert registrar(cli, response_types=["token"]).status_code == 400


def test_a_body_that_is_not_json_is_a_400_not_a_500(cli):
    assert cli.post("/oauth/register", content="no soy json").status_code == 400
    assert cli.post("/oauth/register", json=["lista"]).status_code == 400
    grande = cli.post("/oauth/register", content="x" * (oauth.CUERPO_REGISTRO_MAXIMO + 1))
    assert grande.status_code == 400


def test_registering_over_and_over_from_one_place_starts_costing_time(cli):
    for _ in range(oauth.UMBRAL_REGISTROS):
        assert registrar(cli).status_code == 201
    frenado = registrar(cli)
    assert frenado.status_code == 429
    assert int(frenado.headers["retry-after"]) > 0


# --- autorización: lo que no se puede ---------------------------------------------


def test_an_unknown_client_never_gets_a_redirect(cli):
    respuesta = autorizar(cli, "cli_inventado", pkce()[1])
    assert respuesta.status_code == 400
    assert "location" not in respuesta.headers
    assert "not registered" in respuesta.text


@pytest.mark.parametrize(
    "destino",
    [
        "https://claude.example/api/mcp/auth_callback2",  # prefijo
        "https://claude.example/api/mcp/auth_callback/x",  # subruta
        "https://claude.example/api/mcp/auth_callback?x=1",  # con query
        "http://claude.example/api/mcp/auth_callback",  # otro esquema
        "https://malo.example/?u=https://claude.example/api/mcp/auth_callback",
        "",
    ],
)
def test_a_redirect_uri_that_is_not_exactly_the_registered_one_never_redirects(cli, destino):
    cid = client_id(cli)
    respuesta = autorizar(cli, cid, pkce()[1], redirect_uri=destino)
    assert respuesta.status_code == 400
    assert "location" not in respuesta.headers


@pytest.mark.parametrize(
    ("cambios", "error"),
    [
        ({"code_challenge": None}, "invalid_request"),
        ({"code_challenge": "corto"}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge_method": None}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"scope": "mcp admin"}, "invalid_scope"),
        ({"resource": "https://otro.example/mcp"}, "invalid_target"),
    ],
)
def test_a_request_the_protocol_does_not_allow_comes_back_as_an_error(cli, cambios, error):
    cid = client_id(cli)
    respuesta = autorizar(cli, cid, pkce()[1], **cambios)
    params = codigo_de(respuesta)
    assert params["error"] == error
    assert params["state"] == "xyz-123"  # RFC 6749: el state vuelve también en los errores
    assert params["iss"] == BASE  # RFC 9207
    assert "code" not in params
    # Y no se guardó ninguna solicitud que luego pudiera canjearse.
    assert db.fetch_one("select count(*) as n from oauth_solicitudes")["n"] == 0


def test_without_pkce_nothing_is_authorized_even_if_the_client_insists(cli):
    cid = client_id(cli)
    sin_pkce = autorizar(cli, cid, None, code_challenge=None, code_challenge_method=None)
    assert "code" not in codigo_de(sin_pkce)


# --- la página de login ----------------------------------------------------------


def test_the_login_page_cannot_be_framed_or_cached(cli):
    pagina = autorizar(cli, client_id(cli), pkce()[1])
    assert pagina.status_code == 200
    assert pagina.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in pagina.headers["content-security-policy"]
    assert "form-action 'self'" in pagina.headers["content-security-policy"]
    assert "no-store" in pagina.headers["cache-control"]
    assert pagina.headers["referrer-policy"] == "no-referrer"


def test_the_login_page_says_who_is_asking_and_escapes_it(cli):
    cid = client_id(cli, client_name='<script>alert("x")</script>')
    pagina = autorizar(cli, cid, pkce()[1])
    assert "<script>" not in pagina.text
    assert "&lt;script&gt;" in pagina.text
    assert 'type="password"' in pagina.text


def test_the_form_only_carries_the_request_id_not_the_parameters(cli):
    _, reto = pkce()
    pagina = autorizar(cli, client_id(cli), reto)
    assert reto not in pagina.text
    assert CALLBACK not in pagina.text


# --- el login en sí --------------------------------------------------------------


@respx.mock
def test_a_wrong_password_gives_no_code_and_says_nothing_useful(cli):
    gotrue(status=400)
    pagina = autorizar(cli, client_id(cli), pkce()[1])
    respuesta = entrar(cli, id_de_solicitud(pagina))
    assert respuesta.status_code == 401
    assert "location" not in respuesta.headers
    assert "Incorrect email or password" in respuesta.text
    assert db.fetch_one("select count(*) as n from oauth_sesiones")["n"] == 0
    assert db.fetch_one("select code_hash from oauth_solicitudes")["code_hash"] is None


@respx.mock
def test_an_email_outside_the_team_gets_nothing_even_with_the_right_password(cli):
    gotrue(EXTRANO)
    pagina = autorizar(cli, client_id(cli), pkce()[1])
    respuesta = entrar(cli, id_de_solicitud(pagina), EXTRANO)
    assert respuesta.status_code == 401
    # Exactamente el mismo mensaje que con la contraseña mal: desde fuera no se puede
    # saber si ese correo existe ni si está en el equipo.
    assert "Incorrect email or password" in respuesta.text
    assert db.fetch_one("select count(*) as n from oauth_sesiones")["n"] == 0


@respx.mock
def test_the_identity_is_the_one_supabase_confirms_not_the_one_typed(cli):
    """Si Supabase dice que el token es de bea@, la sesión es de bea@ aunque el formulario
    trajera otro correo: la identidad la decide el proveedor, no la entrada."""
    gotrue(BEA)
    pagina = autorizar(cli, client_id(cli), pkce()[1])
    entrar(cli, id_de_solicitud(pagina), email="ana@example.com")
    assert db.fetch_one("select email from oauth_solicitudes")["email"] == BEA


@respx.mock
def test_when_supabase_is_down_nobody_is_told_their_password_is_wrong(cli):
    gotrue(status=503)
    pagina = autorizar(cli, client_id(cli), pkce()[1])
    respuesta = entrar(cli, id_de_solicitud(pagina))
    assert respuesta.status_code == 503
    assert "Incorrect" not in respuesta.text
    # Y no cuenta como intento fallido: la culpa no es de quien escribe. (La fila
    # "reg:" la deja el registro del cliente, que sí se cuenta, con su umbral holgado.)
    assert db.fetch_all("select clave from oauth_intentos where clave like %s", ("email:%",)) == []


@respx.mock
def test_an_expired_or_invented_request_cannot_be_used_to_log_in(cli):
    gotrue()
    pagina = autorizar(cli, client_id(cli), pkce()[1])
    solicitud_id = id_de_solicitud(pagina)
    db.execute("update oauth_solicitudes set expira_at = now() - interval '1 second'")
    assert entrar(cli, solicitud_id).status_code == 400
    assert entrar(cli, "11111111-1111-1111-1111-111111111111").status_code == 400
    # Y una entrada que ni siquiera es un id: página de error, nunca un 500.
    assert entrar(cli, "no-soy-un-uuid").status_code == 400
    assert entrar(cli, "").status_code == 400
    assert db.fetch_one("select count(*) as n from oauth_sesiones")["n"] == 0


@respx.mock
def test_trying_passwords_starts_costing_time_and_a_good_login_clears_it(cli):
    gotrue(status=400)
    cid = client_id(cli)

    for _ in range(almacen.UMBRAL_INTENTOS):
        pagina = autorizar(cli, cid, pkce()[1])
        assert entrar(cli, id_de_solicitud(pagina)).status_code == 401

    pagina = autorizar(cli, cid, pkce()[1])
    frenado = entrar(cli, id_de_solicitud(pagina))
    assert frenado.status_code == 429
    assert "Too many attempts" in frenado.text

    # La espera crece: el bloqueo de ahora es mayor que el primero.
    primera = almacen.espera_restante(f"email:{ANA}")
    db.execute("update oauth_intentos set bloqueado_hasta = null")
    pagina = autorizar(cli, cid, pkce()[1])
    entrar(cli, id_de_solicitud(pagina))
    assert almacen.espera_restante(f"email:{ANA}") > primera

    # Un login correcto borra el historial de esa persona.
    db.execute("update oauth_intentos set bloqueado_hasta = null")
    gotrue()
    pagina = autorizar(cli, cid, pkce()[1])
    assert entrar(cli, id_de_solicitud(pagina)).status_code == 302
    assert almacen.espera_restante(f"email:{ANA}") == 0


@respx.mock
def test_the_limit_counts_the_email_and_the_place_it_comes_from(cli):
    gotrue(status=400)
    cid = client_id(cli)
    for _ in range(almacen.UMBRAL_INTENTOS):
        pagina = autorizar(cli, cid, pkce()[1])
        entrar(cli, id_de_solicitud(pagina))
    filas = db.fetch_all(
        "select clave, intentos from oauth_intentos where clave not like %s", ("reg:%",)
    )
    assert {f["clave"] for f in filas} == {f"email:{ANA}", "ip:testclient"}
    assert {f["intentos"] for f in filas} == {almacen.UMBRAL_INTENTOS}


@respx.mock
def test_a_good_login_comes_back_with_the_code_the_state_and_the_issuer(cli):
    gotrue()
    cid = client_id(cli)
    pagina = autorizar(cli, cid, pkce()[1], state="estado con espacios & símbolos")
    redireccion = entrar(cli, id_de_solicitud(pagina))
    params = codigo_de(redireccion)
    assert params["code"].startswith(almacen.PREFIJO_CODIGO)
    # El state vuelve tal cual, sin interpretarlo (RFC 6749 §4.1.2).
    assert params["state"] == "estado con espacios & símbolos"
    assert params["iss"] == BASE  # RFC 9207
    # Y vuelve exactamente a la dirección registrada, no a otra.
    assert redireccion.headers["location"].startswith(CALLBACK + "?")


@respx.mock
def test_a_client_that_does_not_send_the_resource_parameter_still_works(cli):
    """RFC 8707 obliga al CLIENTE a mandar `resource`; un servidor que lo exigiera
    rompería con cualquier cliente que aún no lo haga."""
    gotrue()
    cid = client_id(cli)
    verifier, reto = pkce()
    pagina = autorizar(cli, cid, reto, resource=None)
    codigo = codigo_de(entrar(cli, id_de_solicitud(pagina)))["code"]
    tokens = canjear(cli, cid, codigo, verifier, resource=None)
    assert tokens.status_code == 200
    assert llamada_mcp(cli, tokens.json()["access_token"]).status_code == 200


# --- el canje del código ---------------------------------------------------------


def test_the_whole_flow_works_end_to_end(cli):
    cid, _verifier, tokens = conectar(cli)
    assert tokens.status_code == 200
    datos = tokens.json()
    assert datos["token_type"] == "Bearer"
    assert datos["expires_in"] == almacen.VIDA_ACCESS_SEGUNDOS
    assert datos["scope"] == "mcp"
    assert datos["access_token"].startswith(almacen.PREFIJO_ACCESS)
    assert datos["refresh_token"].startswith(almacen.PREFIJO_REFRESH)
    assert tokens.headers["cache-control"] == "no-store"

    # El access token abre el MCP, y la herramienta corre de verdad.
    respuesta = llamada_mcp(cli, datos["access_token"])
    assert respuesta.status_code == 200
    assert not respuesta.json()["result"].get("isError")

    # Y queda constancia de quién conectó y con qué cliente, sin ningún token.
    [fila] = db.fetch_all("select action, payload from agent_actions where action = 'oauth_sesion'")
    assert fila["payload"]["email"] == ANA
    assert fila["payload"]["cliente"] == cid
    volcado = json.dumps(db.fetch_all("select * from agent_actions"), default=str)
    for secreto in (datos["access_token"], datos["refresh_token"], PASSWORD):
        assert secreto not in volcado


def test_only_the_hash_of_a_token_is_stored(cli):
    _, _, tokens = conectar(cli)
    datos = tokens.json()
    filas = db.fetch_all("select * from oauth_tokens")
    assert {f["tipo"] for f in filas} == {"access", "refresh"}
    volcado = json.dumps(filas, default=str)
    assert datos["access_token"] not in volcado
    assert datos["refresh_token"] not in volcado
    assert almacen.hash_valor(datos["access_token"]) in volcado
    # Del código tampoco queda el valor, y la contraseña no se guarda en ningún sitio.
    todo = json.dumps(db.fetch_all("select * from oauth_solicitudes"), default=str)
    assert PASSWORD not in todo


@respx.mock
def test_a_code_works_once_and_the_second_time_kills_the_session(cli):
    gotrue()
    cid = client_id(cli)
    verifier, reto = pkce()
    pagina = autorizar(cli, cid, reto)
    codigo = codigo_de(entrar(cli, id_de_solicitud(pagina)))["code"]

    primera = canjear(cli, cid, codigo, verifier)
    assert primera.status_code == 200
    access = primera.json()["access_token"]
    assert llamada_mcp(cli, access).status_code == 200

    segunda = canjear(cli, cid, codigo, verifier)
    assert segunda.status_code == 400
    assert segunda.json()["error"] == "invalid_grant"
    # Un código reusado es un robo mientras no se demuestre lo contrario: la sesión que
    # salió de él se corta, así que el token que ya se había emitido deja de valer.
    assert llamada_mcp(cli, access).status_code == 401


@respx.mock
def test_a_code_dies_in_a_minute(cli):
    gotrue()
    cid = client_id(cli)
    verifier, reto = pkce()
    pagina = autorizar(cli, cid, reto)
    codigo = codigo_de(entrar(cli, id_de_solicitud(pagina)))["code"]
    vida = db.fetch_one(
        "select extract(epoch from (expira_at - now())) as s from oauth_solicitudes"
    )["s"]
    assert 0 < vida <= 60
    db.execute("update oauth_solicitudes set expira_at = now() - interval '1 second'")
    assert canjear(cli, cid, codigo, verifier).json()["error"] == "invalid_grant"


@respx.mock
@pytest.mark.parametrize(
    ("cambios", "error"),
    [
        ({"code_verifier": None}, "invalid_request"),
        ({"client_id": None}, "invalid_request"),
        ({"code": None}, "invalid_request"),
        ({"code": "hmc_inventado"}, "invalid_grant"),
        ({"redirect_uri": "https://claude.example/otra"}, "invalid_grant"),
        ({"redirect_uri": None}, "invalid_grant"),
        ({"resource": "https://otro.example/mcp"}, "invalid_target"),
    ],
)
def test_a_code_exchange_that_does_not_add_up_gets_no_token(cli, cambios, error):
    gotrue()
    cid = client_id(cli)
    verifier, reto = pkce()
    pagina = autorizar(cli, cid, reto)
    codigo = codigo_de(entrar(cli, id_de_solicitud(pagina)))["code"]
    respuesta = canjear(cli, cid, codigo, verifier, **cambios)
    assert respuesta.status_code == 400
    assert respuesta.json()["error"] == error
    assert "access_token" not in respuesta.text


@respx.mock
def test_the_code_verifier_has_to_be_the_one_that_made_the_challenge(cli):
    gotrue()
    cid = client_id(cli)
    _, reto = pkce()
    otro_verifier, _ = pkce()
    pagina = autorizar(cli, cid, reto)
    codigo = codigo_de(entrar(cli, id_de_solicitud(pagina)))["code"]
    respuesta = canjear(cli, cid, codigo, otro_verifier)
    assert respuesta.json()["error"] == "invalid_grant"
    assert db.fetch_one("select count(*) as n from oauth_sesiones")["n"] == 0


@respx.mock
def test_a_stolen_code_is_useless_to_another_client(cli):
    gotrue()
    cid = client_id(cli)
    ladron = client_id(cli)
    verifier, reto = pkce()
    pagina = autorizar(cli, cid, reto)
    codigo = codigo_de(entrar(cli, id_de_solicitud(pagina)))["code"]
    assert canjear(cli, ladron, codigo, verifier).json()["error"] == "invalid_grant"


@respx.mock
def test_someone_taken_off_the_team_between_login_and_exchange_gets_no_token(cli):
    gotrue()
    cid = client_id(cli)
    verifier, reto = pkce()
    pagina = autorizar(cli, cid, reto)
    codigo = codigo_de(entrar(cli, id_de_solicitud(pagina)))["code"]
    db.execute("delete from panel_users where email = %s", (ANA,))
    assert canjear(cli, cid, codigo, verifier).json()["error"] == "invalid_grant"


def test_an_unsupported_grant_type_is_refused(cli):
    respuesta = cli.post("/oauth/token", data={"grant_type": "client_credentials"})
    assert respuesta.status_code == 400
    assert respuesta.json()["error"] == "unsupported_grant_type"
    assert cli.post("/oauth/token", data={}).json()["error"] == "unsupported_grant_type"


# --- refresh ---------------------------------------------------------------------


def test_refreshing_gives_new_tokens_and_the_old_refresh_dies(cli):
    cid, _, tokens = conectar(cli)
    primero = tokens.json()

    segundo = cli.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": primero["refresh_token"],
            "client_id": cid,
        },
    )
    assert segundo.status_code == 200
    nuevo = segundo.json()
    assert nuevo["access_token"] != primero["access_token"]
    assert nuevo["refresh_token"] != primero["refresh_token"]
    assert llamada_mcp(cli, nuevo["access_token"]).status_code == 200

    # Presentar el refresh viejo revoca la sesión entera (OAuth 2.1: o lo robaron, o el
    # cliente guardó una copia, y desde aquí no se distingue).
    reusado = cli.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": primero["refresh_token"],
            "client_id": cid,
        },
    )
    assert reusado.json()["error"] == "invalid_grant"
    assert llamada_mcp(cli, nuevo["access_token"]).status_code == 401


def test_a_refresh_token_is_useless_to_another_client_or_unknown(cli):
    cid, _, tokens = conectar(cli)
    otro = client_id(cli)
    ajeno = cli.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.json()["refresh_token"],
            "client_id": otro,
        },
    )
    assert ajeno.json()["error"] == "invalid_grant"
    desconocido = cli.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": "hmr_nada", "client_id": cid},
    )
    assert desconocido.json()["error"] == "invalid_grant"


def test_refreshing_does_not_work_for_someone_off_the_team_or_a_revoked_session(cli):
    cid, _, tokens = conectar(cli)
    refresh = tokens.json()["refresh_token"]
    db.execute("delete from panel_users where email = %s", (ANA,))
    respuesta = cli.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": cid},
    )
    assert respuesta.json()["error"] == "invalid_grant"
    # Y la sesión queda cerrada, no solo este intento.
    assert db.fetch_one("select revocado_at from oauth_sesiones")["revocado_at"] is not None


# --- caducidad y revocación ------------------------------------------------------


def test_an_expired_access_token_stops_working_on_the_next_request(cli):
    _, _, tokens = conectar(cli)
    access = tokens.json()["access_token"]
    vida = db.fetch_one(
        "select extract(epoch from (expira_at - now())) as s from oauth_tokens"
        " where tipo = 'access'"
    )["s"]
    assert 0 < vida <= almacen.VIDA_ACCESS_SEGUNDOS
    assert llamada_mcp(cli, access).status_code == 200
    db.execute("update oauth_tokens set expira_at = now() - interval '1 second'")
    assert llamada_mcp(cli, access).status_code == 401


def test_revoking_from_the_panel_stops_an_oauth_session_at_once(cli):
    _, _, tokens = conectar(cli)
    access, refresh = tokens.json()["access_token"], tokens.json()["refresh_token"]
    assert llamada_mcp(cli, access).status_code == 200

    [sesion] = as_user(ANA, "select * from public.panel_sesiones_oauth()")
    as_user(ANA, "select public.panel_revocar_sesion_oauth(%s)", (sesion["id"],))

    assert llamada_mcp(cli, access).status_code == 401
    rechazado = cli.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": "x"},
    )
    assert rechazado.json()["error"] == "invalid_grant"


def test_the_client_can_revoke_its_own_session(cli):
    _, _, tokens = conectar(cli)
    datos = tokens.json()
    respuesta = cli.post("/oauth/revoke", data={"token": datos["refresh_token"]})
    assert respuesta.status_code == 200
    assert llamada_mcp(cli, datos["access_token"]).status_code == 401
    # RFC 7009 §2.2: un token que no existe no es un error (si no, sería un oráculo).
    assert cli.post("/oauth/revoke", data={"token": "hmr_nada"}).status_code == 200
    assert cli.post("/oauth/revoke", data={}).status_code == 400


def test_revoking_the_access_token_closes_the_session_too(cli):
    cid, _, tokens = conectar(cli)
    datos = tokens.json()
    cli.post("/oauth/revoke", data={"token": datos["access_token"]})
    rechazado = cli.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": datos["refresh_token"],
            "client_id": cid,
        },
    )
    assert rechazado.json()["error"] == "invalid_grant"


# --- topes diarios ---------------------------------------------------------------


def test_the_daily_cap_applies_to_an_oauth_session_and_a_refresh_does_not_reset_it(
    cli, restore_config
):
    db.execute("update config set value = '2'::jsonb where key = 'mcp_llamadas_por_dia'")
    cid, _, tokens = conectar(cli)
    access = tokens.json()["access_token"]
    assert llamada_mcp(cli, access).status_code == 200
    assert llamada_mcp(cli, access).status_code == 200
    assert llamada_mcp(cli, access).status_code == 429

    # Un token nuevo por refresh NO devuelve el cupo: el contador vive en la sesión.
    nuevo = cli.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.json()["refresh_token"],
            "client_id": cid,
        },
    ).json()["access_token"]
    assert llamada_mcp(cli, nuevo).status_code == 429
    assert db.fetch_one("select usos_hoy from oauth_sesiones")["usos_hoy"] == 2


def test_a_static_token_and_an_oauth_token_work_side_by_side(cli, cli_estatico):
    _, _, tokens = conectar(cli)
    assert llamada_mcp(cli, tokens.json()["access_token"]).status_code == 200
    assert llamada_mcp(cli, cli_estatico).status_code == 200
    # Cada uno cuenta en su sitio, y revocar uno no toca al otro.
    assert db.fetch_one("select usos_hoy from mcp_tokens")["usos_hoy"] == 1
    assert db.fetch_one("select usos_hoy from oauth_sesiones")["usos_hoy"] == 1
    [sesion] = as_user(ANA, "select * from public.panel_sesiones_oauth()")
    as_user(ANA, "select public.panel_revocar_sesion_oauth(%s)", (sesion["id"],))
    assert llamada_mcp(cli, cli_estatico).status_code == 200


# --- nada de secretos en los logs ------------------------------------------------


@respx.mock
def test_no_password_code_or_token_ever_reaches_the_logs_or_a_body(cli, caplog):
    caplog.set_level(logging.DEBUG)
    gotrue()
    cid = client_id(cli)
    verifier, reto = pkce()
    pagina = autorizar(cli, cid, reto)
    redireccion = entrar(cli, id_de_solicitud(pagina))
    codigo = codigo_de(redireccion)["code"]
    tokens = canjear(cli, cid, codigo, verifier)
    datos = tokens.json()

    respuestas = [
        pagina,
        tokens,
        canjear(cli, cid, codigo, verifier),  # código reusado
        llamada_mcp(cli, datos["access_token"]),
        cli.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": datos["refresh_token"],
                "client_id": cid,
            },
        ),
    ]
    gotrue(status=400)
    pagina_mala = autorizar(cli, cid, pkce()[1])
    respuestas.append(entrar(cli, id_de_solicitud(pagina_mala), password="otra-contraseña"))

    secretos = [
        PASSWORD,
        "otra-contraseña",
        codigo,
        verifier,
        datos["access_token"],
        datos["refresh_token"],
    ]
    for secreto in secretos:
        assert secreto not in caplog.text, secreto[:8]
    # La redirección lleva el código en la URL (es su sitio), pero ningún cuerpo lleva
    # una contraseña, y el formulario nunca devuelve la contraseña escrita.
    for respuesta in respuestas:
        assert PASSWORD not in respuesta.text
        assert "otra-contraseña" not in respuesta.text
    volcado = json.dumps(db.fetch_all("select * from agent_actions"), default=str)
    for secreto in secretos:
        assert secreto not in volcado


# --- las funciones del panel -----------------------------------------------------


def test_the_panel_sees_its_own_sessions_and_never_a_hash(cli):
    cid, _, tokens = conectar(cli)
    sesiones = as_user(ANA, "select * from public.panel_sesiones_oauth()")
    assert len(sesiones) == 1
    assert sesiones[0]["cliente"] == "Claude"
    assert sesiones[0]["client_id"] == cid
    assert sesiones[0]["revocado_at"] is None
    volcado = json.dumps(sesiones, default=str)
    assert tokens.json()["access_token"] not in volcado
    assert almacen.hash_valor(tokens.json()["access_token"]) not in volcado
    # Otra persona no ve nada de esto.
    assert as_user(BEA, "select * from public.panel_sesiones_oauth()") == []


def test_only_the_owner_revokes_a_session(cli):
    conectar(cli)
    [sesion] = as_user(ANA, "select * from public.panel_sesiones_oauth()")
    with pytest.raises(psycopg.errors.RaiseException, match="session not found"):
        as_user(BEA, "select public.panel_revocar_sesion_oauth(%s)", (sesion["id"],))
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        as_user(EXTRANO, "select * from public.panel_sesiones_oauth()")
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        as_user(None, "select * from public.panel_sesiones_oauth()")
    assert db.fetch_one("select revocado_at from oauth_sesiones")["revocado_at"] is None

    # Y revocar dos veces no es un error.
    as_user(ANA, "select public.panel_revocar_sesion_oauth(%s)", (sesion["id"],))
    primera = db.fetch_one("select revocado_at from oauth_sesiones")["revocado_at"]
    as_user(ANA, "select public.panel_revocar_sesion_oauth(%s)", (sesion["id"],))
    assert db.fetch_one("select revocado_at from oauth_sesiones")["revocado_at"] == primera


def test_the_panel_cannot_read_the_oauth_tables_directly(cli):
    conectar(cli)
    for tabla in ("oauth_tokens", "oauth_sesiones", "oauth_clients", "oauth_solicitudes"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_user(ANA, f"select * from {tabla}")


@respx.mock
def test_a_person_keeps_a_handful_of_connections_and_the_oldest_falls_off(cli):
    """Reconectar es normal; lo que no puede es acumular autorizaciones olvidadas."""
    gotrue()
    cid = client_id(cli)
    primeros = []
    for _ in range(almacen.MAXIMO_SESIONES + 1):
        verifier, reto = pkce()
        pagina = autorizar(cli, cid, reto)
        codigo = codigo_de(entrar(cli, id_de_solicitud(pagina)))["code"]
        primeros.append(canjear(cli, cid, codigo, verifier).json()["access_token"])

    activas = db.fetch_one("select count(*) as n from oauth_sesiones where revocado_at is null")[
        "n"
    ]
    assert activas == almacen.MAXIMO_SESIONES
    # La más vieja se cerró, y su token dejó de valer; la última sigue entrando.
    assert llamada_mcp(cli, primeros[0]).status_code == 401
    assert llamada_mcp(cli, primeros[-1]).status_code == 200


def test_a_write_from_an_oauth_connection_is_signed_with_the_persons_email(cli):
    """Las herramientas de escritura y la auditoría necesitan saber quién llama; da igual
    de qué tipo sea la credencial, el correo sale de ella y nunca de lo que Claude mande."""
    db.execute("insert into prospects (slack_user_id, full_name) values ('U1', 'Ada')")
    _, _, tokens = conectar(cli)
    respuesta = cli.post(
        "/mcp",
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "persona_cambiar_estado",
                    "arguments": {"id_o_slack_user_id": "U1", "estado": "contactado"},
                },
            }
        ),
        headers={**HEADERS_MCP, "Authorization": f"Bearer {tokens.json()['access_token']}"},
    )
    assert respuesta.status_code == 200
    assert not respuesta.json()["result"].get("isError")
    assert db.fetch_one("select state from prospects")["state"] == "contactado"

    [firma] = db.fetch_all("select payload from agent_actions where action = 'mcp_escritura'")
    assert firma["payload"]["email"] == ANA
    assert firma["payload"]["sufijo"] == tokens.json()["access_token"][-4:]
    assert tokens.json()["access_token"] not in json.dumps(firma, default=str)
