"""Cliente de Unipile: solo lectura, con topes, horario y freno. Nunca llama a
Unipile de verdad (respx) ni duerme de verdad."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from handoff_agent import db, guards
from handoff_agent.tools import unipile

BASE = "https://api4.unipile.com:13460/api/v1"
ACCOUNT_URL = f"{BASE}/accounts/acc-1"
PARAMS_URL = f"{BASE}/linkedin/search/parameters"
SEARCH_URL = f"{BASE}/linkedin/search"
NY = ZoneInfo("America/New_York")
# Martes, 10:00 en Nueva York: dentro del horario por defecto (8-19, lun-vie).
TUESDAY_10 = datetime(2026, 9, 22, 10, 0, tzinfo=NY)

HEALTHY = {"id": "acc-1", "type": "LINKEDIN", "sources": [{"id": "s1", "status": "OK"}]}
CODELCO = {"items": [{"title": "CODELCO – Corporación Nacional del Cobre", "id": "16300"}]}
PEOPLE = {
    "object": "LinkedinSearch",
    "items": [
        {
            "type": "PEOPLE",
            "id": "ACwAAB123",
            "name": "Ana Pérez",
            "first_name": "Ana",
            "last_name": "Pérez",
            "public_identifier": "ana-perez",
            "public_profile_url": "https://www.linkedin.com/in/ana-perez",
            "headline": "Gerente de operaciones en CODELCO",
            "location": "Chile",
            "current_positions": [
                {"company": "CODELCO", "company_id": "16300", "role": "Gerente de operaciones"}
            ],
        }
    ],
    "paging": {"start": 0, "page_count": 1},
    "cursor": "abc",
}


@pytest.fixture
def env(monkeypatch, restore_config):
    monkeypatch.setenv("UNIPILE_API_KEY", "secret-unipile-key")
    monkeypatch.setenv("UNIPILE_DSN", "api4.unipile.com:13460")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "acc-1")
    sleeps = []
    alerts = []
    monkeypatch.setattr(unipile, "_dormir", sleeps.append)
    monkeypatch.setattr(unipile, "_azar", lambda a, b: 10.0)
    monkeypatch.setattr(unipile, "_ahora", lambda zona: TUESDAY_10.astimezone(zona))
    monkeypatch.setattr(unipile, "_ultima_llamada", None)
    monkeypatch.setattr(unipile.ops_alerts, "alert", lambda kind, msg: alerts.append((kind, msg)))
    return {"sleeps": sleeps, "alerts": alerts}


def set_config(key, value):
    db.execute(
        "insert into config (key, value) values (%s, %s::jsonb) "
        "on conflict (key) do update set value = excluded.value",
        (key, json.dumps(value)),
    )


def paused():
    return db.fetch_one("select value from config where key = 'unipile_pausado'")["value"]


def actions(action=None):
    if action:
        return db.fetch_all(
            "select action, payload, result from agent_actions where action = %s", (action,)
        )
    return db.fetch_all("select action, payload, result from agent_actions")


# --- estado de la cuenta ---------------------------------------------------------


@respx.mock
def test_a_healthy_account_is_ok_and_the_key_goes_in_the_header(env):
    route = respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=HEALTHY))
    assert unipile.estado_cuenta() is True
    assert route.calls[0].request.headers["X-API-KEY"] == "secret-unipile-key"
    assert paused() is False
    assert actions("unipile_estado")[0]["result"]["sana"] is True


@respx.mock
def test_an_account_not_ok_pauses_unipile_and_alerts_once(env):
    bad = {**HEALTHY, "sources": [{"id": "s1", "status": "CREDENTIALS"}]}
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=bad))
    assert unipile.estado_cuenta() is False
    assert unipile.estado_cuenta() is False
    assert paused() is True
    assert [kind for kind, _ in env["alerts"]] == ["unipile_pausado"]
    assert "CREDENTIALS" in env["alerts"][0][1]


@respx.mock
def test_an_account_without_sources_is_not_healthy(env):
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json={"id": "acc-1"}))
    assert unipile.estado_cuenta() is False
    assert paused() is True


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_pauses_unipile(env, status):
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(status, json={"status": status}))
    assert unipile.estado_cuenta() is False
    assert paused() is True


@respx.mock
def test_a_server_error_on_the_status_is_an_error_not_a_pause(env):
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(unipile.UnipileError):
        unipile.estado_cuenta()
    assert paused() is False


@respx.mock
def test_a_network_failure_is_an_error_not_a_pause(env):
    respx.get(ACCOUNT_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(unipile.UnipileError):
        unipile.estado_cuenta()
    assert paused() is False


def test_without_credentials_nothing_is_called(conn, monkeypatch):
    for name in ("UNIPILE_API_KEY", "UNIPILE_DSN", "UNIPILE_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(unipile.UnipileNoConfigurado):
        unipile.estado_cuenta()
    with pytest.raises(unipile.UnipileNoConfigurado):
        unipile.resolver_empresa("Codelco")


# --- llamadas de LinkedIn --------------------------------------------------------


@respx.mock
def test_resolver_empresa_returns_ids_and_titles(env):
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=HEALTHY))
    route = respx.get(PARAMS_URL).mock(return_value=httpx.Response(200, json=CODELCO))
    assert unipile.resolver_empresa("Codelco") == [
        ("16300", "CODELCO – Corporación Nacional del Cobre")
    ]
    params = route.calls[0].request.url.params
    assert (params["account_id"], params["type"], params["keywords"]) == (
        "acc-1",
        "COMPANY",
        "Codelco",
    )
    assert params["limit"] == "5"
    logged = actions("unipile_busqueda")
    assert len(logged) == 1
    assert logged[0]["payload"]["keywords"] == "Codelco"
    assert logged[0]["result"] == {"status": 200, "items": 1}


@respx.mock
def test_buscar_personas_posts_a_sales_navigator_search(env):
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=HEALTHY))
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=PEOPLE))
    people = unipile.buscar_personas("16300", "gerente operaciones", limit=5)
    request = route.calls[0].request
    assert request.url.params["account_id"] == "acc-1"
    assert request.url.params["limit"] == "5"
    assert json.loads(request.content) == {
        "api": "sales_navigator",
        "category": "people",
        "keywords": "gerente operaciones",
        "company": {"include": ["16300"]},
    }
    assert people == [
        unipile.PersonaLinkedIn(
            linkedin_id="ACwAAB123",
            full_name="Ana Pérez",
            first_name="Ana",
            last_name="Pérez",
            public_identifier="ana-perez",
            profile_url="https://www.linkedin.com/in/ana-perez",
            headline="Gerente de operaciones en CODELCO",
            location="Chile",
            posiciones=(
                unipile.Posicion(
                    empresa="CODELCO", company_id="16300", cargo="Gerente de operaciones"
                ),
            ),
        )
    ]


@respx.mock
def test_a_profile_url_outside_linkedin_is_dropped(env):
    odd = {"items": [{**PEOPLE["items"][0], "public_profile_url": "https://evil.example/in/x"}]}
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=HEALTHY))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=odd))
    assert unipile.buscar_personas("16300", "ceo")[0].profile_url is None


def test_a_company_id_must_be_numeric(env):
    with pytest.raises(ValueError):
        unipile.buscar_personas("16300 OR 1", "ceo")


@respx.mock
def test_the_api_key_never_reaches_the_ledger(env):
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=HEALTHY))
    respx.get(PARAMS_URL).mock(return_value=httpx.Response(200, json=CODELCO))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=PEOPLE))
    unipile.resolver_empresa("Codelco")
    unipile.buscar_personas("16300", "ceo")
    dumped = json.dumps([dict(a) for a in actions()])
    assert "secret-unipile-key" not in dumped
    # Tampoco el cuerpo entero: ni nombres ni titulares de las personas.
    assert "Ana" not in dumped


@respx.mock
def test_calls_are_spaced_by_a_random_pause(env):
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=HEALTHY))
    respx.get(PARAMS_URL).mock(return_value=httpx.Response(200, json=CODELCO))
    unipile.resolver_empresa("Codelco")
    assert env["sleeps"] == []
    unipile.resolver_empresa("Codelco")
    assert len(env["sleeps"]) == 1
    assert 4 <= env["sleeps"][0] <= 15


def test_the_pause_range_is_four_to_fifteen_seconds():
    assert (unipile.PAUSA_MIN_SEGUNDOS, unipile.PAUSA_MAX_SEGUNDOS) == (4, 15)


# --- guardarraíles ---------------------------------------------------------------


@respx.mock
def test_the_kill_switch_stops_linkedin_calls(env):
    set_config("kill_switch", True)
    with pytest.raises(guards.KillSwitchActive):
        unipile.resolver_empresa("Codelco")
    assert respx.calls.call_count == 0


@respx.mock
def test_a_paused_unipile_stops_linkedin_calls(env):
    set_config("unipile_pausado", True)
    with pytest.raises(unipile.UnipilePausado):
        unipile.buscar_personas("16300", "ceo")
    assert respx.calls.call_count == 0


@respx.mock
@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 9, 22, 7, 59, tzinfo=NY),
        datetime(2026, 9, 22, 19, 0, tzinfo=NY),
        datetime(2026, 9, 26, 11, 0, tzinfo=NY),  # sábado
        datetime(2026, 9, 27, 11, 0, tzinfo=NY),  # domingo
    ],
)
def test_outside_human_hours_nothing_touches_linkedin(env, monkeypatch, moment):
    monkeypatch.setattr(unipile, "_ahora", lambda zona: moment.astimezone(zona))
    with pytest.raises(unipile.UnipileFueraDeHorario):
        unipile.resolver_empresa("Codelco")
    assert respx.calls.call_count == 0


@respx.mock
def test_the_hours_are_read_in_the_configured_zone(env, monkeypatch):
    """Las 10:00 de Nueva York son las 16:00 en Madrid: fuera de 8-15 allí."""
    set_config("unipile_zona", "Europe/Madrid")
    set_config("unipile_hora_fin", 15)
    with pytest.raises(unipile.UnipileFueraDeHorario):
        unipile.resolver_empresa("Codelco")


@respx.mock
def test_the_daily_cap_counts_todays_searches(env):
    set_config("unipile_busquedas_por_dia", 2)
    yesterday = TUESDAY_10 - timedelta(days=1)
    db.execute(
        "insert into agent_actions (action, payload, created_at) "
        "values ('unipile_busqueda', '{}', %s)",
        (yesterday,),
    )
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=HEALTHY))
    search = respx.get(PARAMS_URL).mock(return_value=httpx.Response(200, json=CODELCO))
    unipile.resolver_empresa("Codelco")
    unipile.resolver_empresa("Codelco")
    with pytest.raises(unipile.UnipileTopeDiario):
        unipile.resolver_empresa("Codelco")
    assert search.call_count == 2


@respx.mock
def test_an_unhealthy_account_stops_the_search_before_linkedin(env):
    bad = {**HEALTHY, "sources": [{"id": "s1", "status": "CHECKPOINT"}]}
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=bad))
    search = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=PEOPLE))
    with pytest.raises(unipile.UnipilePausado):
        unipile.buscar_personas("16300", "ceo")
    assert search.call_count == 0
    assert paused() is True


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"status": 401, "type": "errors/invalid_credentials"}),
        httpx.Response(403, json={"status": 403, "type": "errors/forbidden"}),
        httpx.Response(
            422, json={"status": 422, "type": "errors/disconnected_account", "title": "x"}
        ),
        httpx.Response(500, json={"status": 500, "type": "errors/checkpoint_error"}),
    ],
)
def test_a_linkedin_answer_that_means_trouble_pauses_everything(env, response):
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=HEALTHY))
    respx.post(SEARCH_URL).mock(return_value=response)
    with pytest.raises(unipile.UnipilePausado):
        unipile.buscar_personas("16300", "ceo")
    assert paused() is True
    assert [kind for kind, _ in env["alerts"]] == ["unipile_pausado"]
    assert len(actions("unipile_busqueda")) == 1


@respx.mock
def test_an_ordinary_server_error_does_not_pause(env):
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=HEALTHY))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(500, json={"type": "errors/unknown"}))
    with pytest.raises(unipile.UnipileError) as caught:
        unipile.buscar_personas("16300", "ceo")
    assert not isinstance(caught.value, unipile.UnipilePausado)
    assert paused() is False
    assert actions("unipile_busqueda")[0]["result"] == {"status": 500}


def test_the_client_has_no_way_to_write_to_linkedin():
    """Solo lectura, por construcción: ni invitar, ni escribir, ni InMail."""
    forbidden = ("invit", "enviar", "send", "message", "mensaje", "inmail", "post_", "escribir")
    public = [name for name in dir(unipile) if not name.startswith("_")]
    assert [n for n in public if any(word in n.lower() for word in forbidden)] == []


# --- uso de hoy ----------------------------------------------------------------


def test_usage_summary_reports_caps_and_hours(env):
    db.execute("insert into agent_actions (action, payload) values ('unipile_busqueda', '{}')")
    resumen = unipile.resumen_uso()
    assert resumen["busquedas_hoy"] == 1
    assert resumen["tope_busquedas"] == 25
    assert resumen["tope_perfiles"] == 40
    assert resumen["en_horario"] is True
    assert resumen["pausado"] is False
    assert resumen["zona"] == "America/New_York"
