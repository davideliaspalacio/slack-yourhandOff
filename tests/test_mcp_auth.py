"""Tokens del MCP remoto: la base (funciones del panel) y la puerta HTTP (`/mcp`).

Lo que más importa aquí: sin token válido no corre ninguna herramienta, un token
revocado deja de valer al instante, el tope diario corta con 429 y el token en claro
no aparece nunca en la base, en el ledger ni en los logs.
"""

import json
import logging

import psycopg
import pytest
from fastapi.testclient import TestClient

from handoff_agent import db, mcp_remoto
from handoff_agent.web import app as web_app
from handoff_agent.web import mcp_auth
from tests.test_panel_rls import as_user

ANA = "ana@example.com"
BEA = "bea@example.com"
EXTRANO = "intruso@example.com"

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(metodo, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": metodo, "params": params or {}}


def llamada(nombre="resumen_costes", argumentos=None):
    return rpc("tools/call", {"name": nombre, "arguments": argumentos or {}})


@pytest.fixture
def equipo(conn):
    for email in (ANA, BEA):
        db.execute("insert into panel_users (email) values (%s)", (email,))


def crear(email, nombre="Claude de prueba"):
    return as_user(email, "select public.panel_crear_token_mcp(%s)", (nombre,))[0][
        "panel_crear_token_mcp"
    ]


def listar(email):
    return as_user(email, "select * from public.panel_tokens_mcp()")


@pytest.fixture
def cliente():
    # Con `with` corre el ciclo de vida: arranca el servidor MCP de verdad.
    with TestClient(web_app.app) as c:
        yield c


def post(cliente, cuerpo, token=None, cabeceras=None):
    h = dict(HEADERS)
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    h.update(cabeceras or {})
    return cliente.post("/mcp", content=json.dumps(cuerpo), headers=h)


# --- funciones del panel ---


def test_creating_a_token_returns_it_once_and_stores_only_the_hash(equipo):
    token = crear(ANA, "Claude de Ana")
    assert len(token) == 64 and set(token) <= set("0123456789abcdef")
    fila = db.fetch_one("select * from mcp_tokens")
    assert fila["email"] == ANA
    assert fila["nombre"] == "Claude de Ana"
    assert fila["sufijo"] == token[-4:]
    assert fila["token_hash"] == mcp_auth.hash_token(token)
    assert token not in json.dumps(fila, default=str)
    # Ni listarlos devuelve el token ni el hash.
    [visto] = listar(ANA)
    assert set(visto) == {"id", "nombre", "sufijo", "creado_at", "ultimo_uso_at", "revocado_at"}
    assert token not in json.dumps(visto, default=str)


def test_two_tokens_are_never_equal(equipo):
    assert crear(ANA) != crear(ANA)


def test_a_signed_in_stranger_cannot_use_the_token_functions(equipo):
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        crear(EXTRANO)
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        listar(EXTRANO)


def test_a_request_without_a_session_cannot_create_tokens(equipo):
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        as_user(None, "select public.panel_crear_token_mcp('x')")


def test_the_panel_cannot_read_the_table_directly(equipo):
    crear(ANA)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(ANA, "select * from mcp_tokens")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(ANA, "update mcp_tokens set revocado_at = null")


def test_a_name_is_required_and_bounded(equipo):
    with pytest.raises(psycopg.errors.RaiseException, match="invalid name"):
        crear(ANA, "   ")
    with pytest.raises(psycopg.errors.RaiseException, match="invalid name"):
        crear(ANA, "x" * 61)


def test_at_most_five_active_tokens_per_person(equipo):
    for i in range(5):
        crear(ANA, f"t{i}")
    with pytest.raises(psycopg.errors.RaiseException, match="too many tokens: revoke one first"):
        crear(ANA, "sexto")
    # Otra persona tiene su propio cupo.
    crear(BEA)
    # Revocar uno libera cupo.
    primero = listar(ANA)[-1]
    as_user(ANA, "select public.panel_revocar_token_mcp(%s)", (primero["id"],))
    crear(ANA, "sexto")


def test_you_only_see_your_own_tokens(equipo):
    crear(ANA, "de ana")
    crear(BEA, "de bea")
    assert [t["nombre"] for t in listar(ANA)] == ["de ana"]
    assert [t["nombre"] for t in listar(BEA)] == ["de bea"]


def test_you_cannot_revoke_someone_elses_token(equipo):
    crear(ANA)
    ajeno = listar(ANA)[0]["id"]
    with pytest.raises(psycopg.errors.RaiseException, match="token not found"):
        as_user(BEA, "select public.panel_revocar_token_mcp(%s)", (ajeno,))
    assert listar(ANA)[0]["revocado_at"] is None


def test_revoking_is_idempotent_and_keeps_the_first_date(equipo):
    crear(ANA)
    token_id = listar(ANA)[0]["id"]
    as_user(ANA, "select public.panel_revocar_token_mcp(%s)", (token_id,))
    primera = listar(ANA)[0]["revocado_at"]
    assert primera is not None
    as_user(ANA, "select public.panel_revocar_token_mcp(%s)", (token_id,))
    assert listar(ANA)[0]["revocado_at"] == primera


def test_the_daily_cap_can_be_edited_from_the_panel_but_only_as_a_number(equipo, restore_config):
    assert (
        db.fetch_one("select value from config where key = 'mcp_llamadas_por_dia'")["value"] == 500
    )
    sql = "update config set value = %s::jsonb where key = 'mcp_llamadas_por_dia'"
    assert as_user(ANA, sql, ("700",)) == 1
    for malo in ('"mucho"', "true", "-1", "null"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_user(ANA, sql, (malo,))
    # Un desconocido no la toca (RLS: cero filas), y la política no abre otras claves.
    assert as_user(EXTRANO, sql, ("1",)) == 0
    assert as_user(ANA, "update config set value = 'true'::jsonb where key = 'kill_switch'") == 0


# --- puerta HTTP ---


def test_no_token_is_401_and_no_tool_runs(equipo, cliente, monkeypatch):
    ejecutadas = []
    monkeypatch.setattr(
        mcp_remoto.ledger, "cost_summary", lambda *a, **k: ejecutadas.append(1) or {}
    )
    r = post(cliente, llamada())
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert ejecutadas == []
    assert db.fetch_one("select count(*) as n from agent_actions")["n"] == 0


@pytest.mark.parametrize(
    "cabecera",
    [
        "Bearer",
        "Bearer ",
        "Basic " + "a" * 64,
        "Bearer corto",
        "Bearer " + "g" * 64,  # no es hexadecimal
        "Bearer " + "A" * 64,  # mayúsculas: el token es minúsculas
        "Bearer " + "a" * 65,
        "Token " + "a" * 64,
    ],
)
def test_malformed_credentials_are_401(equipo, cliente, cabecera):
    r = cliente.post(
        "/mcp", content=json.dumps(llamada()), headers={**HEADERS, "Authorization": cabecera}
    )
    assert r.status_code == 401


def test_an_unknown_but_well_formed_token_is_401(equipo, cliente, monkeypatch):
    ejecutadas = []
    monkeypatch.setattr(
        mcp_remoto.ledger, "cost_summary", lambda *a, **k: ejecutadas.append(1) or {}
    )
    r = post(cliente, llamada(), token="a" * 64)
    assert r.status_code == 401
    assert ejecutadas == []


def test_the_token_in_the_query_string_is_not_accepted(equipo, cliente):
    token = crear(ANA)
    r = cliente.post(f"/mcp?token={token}", content=json.dumps(llamada()), headers=HEADERS)
    assert r.status_code == 401


def test_a_valid_token_lists_exactly_the_read_tools(equipo, cliente):
    token = crear(ANA)
    r = post(cliente, rpc("tools/list"), token)
    assert r.status_code == 200
    nombres = {t["name"] for t in r.json()["result"]["tools"]}
    assert nombres == {
        "personas_buscar",
        "persona_ver",
        "senales_recientes",
        "resumen_costes",
        "estado_sistema",
    }


def test_a_valid_token_runs_a_tool(equipo, cliente):
    token = crear(ANA)
    r = post(cliente, llamada("resumen_costes", {"dias": 7}), token)
    assert r.status_code == 200
    resultado = r.json()["result"]
    assert not resultado.get("isError")
    assert json.loads(resultado["content"][0]["text"])["days"] == 7


def test_a_revoked_token_stops_working_on_the_next_request(equipo, cliente):
    token = crear(ANA)
    assert post(cliente, llamada(), token).status_code == 200
    as_user(ANA, "select public.panel_revocar_token_mcp(%s)", (listar(ANA)[0]["id"],))
    assert post(cliente, llamada(), token).status_code == 401
    assert post(cliente, rpc("tools/list"), token).status_code == 401


def test_tokens_are_independent(equipo, cliente):
    uno, dos = crear(ANA, "uno"), crear(ANA, "dos")
    as_user(ANA, "select public.panel_revocar_token_mcp(%s)", (listar(ANA)[1]["id"],))
    assert post(cliente, llamada(), uno).status_code == 401
    assert post(cliente, llamada(), dos).status_code == 200


def test_the_daily_cap_returns_429_and_the_protocol_calls_do_not_count(
    equipo, cliente, restore_config
):
    db.execute("update config set value = '2'::jsonb where key = 'mcp_llamadas_por_dia'")
    token = crear(ANA)
    # listar herramientas no gasta tope.
    for _ in range(4):
        assert post(cliente, rpc("tools/list"), token).status_code == 200
    assert post(cliente, llamada(), token).status_code == 200
    assert post(cliente, llamada(), token).status_code == 200
    r = post(cliente, llamada(), token)
    assert r.status_code == 429
    assert token not in r.text
    # Pasado el tope sigue pudiendo listar, y el contador no se pasó de 2.
    assert post(cliente, rpc("tools/list"), token).status_code == 200
    assert db.fetch_one("select usos_hoy from mcp_tokens")["usos_hoy"] == 2


def test_a_cap_of_zero_switches_the_remote_mcp_off(equipo, cliente, restore_config):
    db.execute("update config set value = '0'::jsonb where key = 'mcp_llamadas_por_dia'")
    token = crear(ANA)
    assert post(cliente, llamada(), token).status_code == 429


def test_the_cap_is_per_token(equipo, cliente, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'mcp_llamadas_por_dia'")
    uno, dos = crear(ANA, "uno"), crear(BEA, "dos")
    assert post(cliente, llamada(), uno).status_code == 200
    assert post(cliente, llamada(), uno).status_code == 429
    assert post(cliente, llamada(), dos).status_code == 200


def test_the_counter_starts_over_on_a_new_day(equipo, cliente, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'mcp_llamadas_por_dia'")
    token = crear(ANA)
    assert post(cliente, llamada(), token).status_code == 200
    assert post(cliente, llamada(), token).status_code == 429
    db.execute("update mcp_tokens set dia_uso = dia_uso - 1")  # "ayer"
    assert post(cliente, llamada(), token).status_code == 200
    assert db.fetch_one("select usos_hoy from mcp_tokens")["usos_hoy"] == 1


def test_a_batch_counts_every_tool_call(equipo, cliente, restore_config):
    db.execute("update config set value = '2'::jsonb where key = 'mcp_llamadas_por_dia'")
    token = crear(ANA)
    lote = [{**llamada(), "id": i} for i in range(3)]
    r = post(cliente, lote, token)
    assert r.status_code == 429


def test_use_is_recorded_once_per_token_and_day_without_the_token(equipo, cliente):
    token = crear(ANA, "Claude de Ana")
    for _ in range(3):
        assert post(cliente, llamada("resumen_costes"), token).status_code == 200
    filas = db.fetch_all("select action, payload, prospect_id from agent_actions")
    assert len(filas) == 1
    assert filas[0]["action"] == "mcp_sesion"
    assert filas[0]["payload"] == {
        "email": ANA,
        "sufijo": token[-4:],
        "herramienta": "resumen_costes",
    }
    # Al día siguiente, otra sesión.
    db.execute("update mcp_tokens set dia_uso = dia_uso - 1")
    assert post(cliente, llamada("estado_sistema"), token).status_code == 200
    assert db.fetch_one("select count(*) as n from agent_actions")["n"] == 2


def test_last_use_is_updated(equipo, cliente):
    token = crear(ANA)
    assert listar(ANA)[0]["ultimo_uso_at"] is None
    post(cliente, rpc("tools/list"), token)
    assert listar(ANA)[0]["ultimo_uso_at"] is not None


def test_the_token_never_reaches_logs_ledger_or_error_bodies(
    equipo, cliente, caplog, restore_config
):
    caplog.set_level(logging.DEBUG)
    db.execute("update config set value = '1'::jsonb where key = 'mcp_llamadas_por_dia'")
    token = crear(ANA)
    respuestas = [
        post(cliente, llamada(), token),  # 200
        post(cliente, llamada(), token),  # 429
        post(cliente, llamada(), "b" * 64),  # 401 desconocido
        cliente.post("/mcp", content="{", headers={**HEADERS, "Authorization": f"Bearer {token}"}),
    ]
    as_user(ANA, "select public.panel_revocar_token_mcp(%s)", (listar(ANA)[0]["id"],))
    respuestas.append(post(cliente, llamada(), token))  # 401 revocado
    assert token not in caplog.text
    for r in respuestas:
        assert token not in r.text
        assert token not in json.dumps(dict(r.headers))
    volcado = json.dumps(db.fetch_all("select * from agent_actions"), default=str)
    assert token not in volcado
    assert "Authorization" not in volcado


def test_a_body_that_is_not_json_is_rejected_by_the_server_not_a_500(equipo, cliente):
    token = crear(ANA)
    r = cliente.post("/mcp", content="{", headers={**HEADERS, "Authorization": f"Bearer {token}"})
    assert r.status_code < 500


def test_an_oversized_body_is_413(equipo, cliente, monkeypatch):
    token = crear(ANA)
    monkeypatch.setattr(mcp_auth, "CUERPO_MAXIMO", 100)
    r = cliente.post(
        "/mcp", content="x" * 500, headers={**HEADERS, "Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 413


def test_the_other_endpoints_are_untouched(cliente):
    assert cliente.get("/salud").json() == {"ok": True}
    assert cliente.post("/slack/acciones", content=b"x").status_code == 401


def test_without_the_mcp_server_running_a_valid_token_gets_503(equipo):
    """Sin el ciclo de vida (TestClient sin `with`) no hay servidor MCP detrás."""
    token = crear(ANA)
    r = TestClient(web_app.app).post(
        "/mcp",
        content=json.dumps(llamada()),
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 503
