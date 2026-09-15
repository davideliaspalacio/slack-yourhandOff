import json
from decimal import Decimal

import httpx
import pytest
import respx

from handoff_agent.tools import search

SEARXNG = "http://127.0.0.1:8080"
SERPER = "https://google.serper.dev/search"


@respx.mock
def test_buscar_web_maps_searxng_results(conn):
    respx.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Acme Corp", "url": "https://acme.com", "content": "We build things"},
                    {
                        "title": "Acme on LinkedIn",
                        "url": "https://linkedin.com/company/acme",
                        "content": "",
                    },
                ]
            },
        )
    )
    results = search.buscar_web("acme corp")
    assert [r.url for r in results] == ["https://acme.com", "https://linkedin.com/company/acme"]
    assert results[0].snippet == "We build things"


@respx.mock
def test_buscar_web_respects_the_limit(conn):
    respx.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": f"r{i}", "url": f"https://e{i}.com", "content": ""} for i in range(20)
                ]
            },
        )
    )
    assert len(search.buscar_web("algo", limit=3)) == 3


@respx.mock
def test_buscar_web_logs_the_action(conn):
    respx.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(200, json={"results": []}))
    search.buscar_web("acme")
    with conn.cursor() as cur:
        cur.execute("select action, payload from agent_actions")
        row = cur.fetchone()
    assert row[0] == "buscar_web"
    assert row[1]["query"] == "acme"


@respx.mock
def test_buscar_web_raises_a_clear_error_when_searxng_is_down(conn):
    respx.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(502))
    with pytest.raises(search.SearchUnavailable, match="SearXNG"):
        search.buscar_web("acme")


@respx.mock
def test_buscar_web_works_without_an_openai_key(conn, monkeypatch):
    """buscar_web no toca ningún LLM; exigir OPENAI_API_KEY aquí acoplaría
    integraciones que no tienen nada que ver."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    respx.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Acme", "url": "https://acme.com", "content": ""},
                ]
            },
        )
    )
    assert len(search.buscar_web("acme")) == 1


@respx.mock
def test_zero_results_with_unresponsive_engines_is_an_outage(conn):
    """Con los motores vetados por CAPTCHA, SearXNG contesta 200 con cero
    resultados: eso no es "no hay nada", es que no hubo búsqueda."""
    respx.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [],
                "unresponsive_engines": [["google", "CAPTCHA"], ["duckduckgo", "timeout"]],
            },
        )
    )
    with pytest.raises(search.SearchUnavailable) as info:
        search.buscar_web("acme")
    message = str(info.value)
    assert "google" in message and "CAPTCHA" in message
    assert "duckduckgo" in message and "timeout" in message


@pytest.fixture
def serper_key(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "serper-test-key")


def _sent(route) -> dict:
    """El cuerpo JSON de la primera petición que recibió la ruta."""
    return json.loads(route.calls[0].request.content)


@respx.mock
def test_serper_is_used_when_its_key_is_configured(conn, serper_key):
    route = respx.post(SERPER).mock(
        return_value=httpx.Response(
            200,
            json={
                "organic": [
                    {"title": "Acme", "link": "https://acme.com", "snippet": "We build things"},
                ]
            },
        )
    )
    results = search.buscar_web("acme", limit=5)
    assert [r.url for r in results] == ["https://acme.com"]
    assert results[0].snippet == "We build things"
    assert route.calls[0].request.headers["X-API-KEY"] == "serper-test-key"
    assert _sent(route)["q"] == "acme"


@respx.mock
def test_every_serper_query_lands_in_the_cost_ledger(conn, serper_key):
    respx.post(SERPER).mock(return_value=httpx.Response(200, json={"organic": []}))
    search.buscar_web("acme")
    with conn.cursor() as cur:
        cur.execute("select source, cost_usd from cost_events")
        source, cost = cur.fetchone()
    assert source == "serper_search"
    assert Decimal(cost) == Decimal("0.001000")


@respx.mock
def test_serper_asks_for_at_most_ten_results(conn, serper_key):
    """Hasta 10 resultados, una búsqueda es un crédito. No está confirmado que
    pedir más cueste lo mismo, y el agente nunca pide más de 8."""
    route = respx.post(SERPER).mock(return_value=httpx.Response(200, json={"organic": []}))
    search.buscar_web("acme", limit=50)
    assert _sent(route)["num"] == 10


@respx.mock
def test_a_serper_failure_falls_back_to_searxng_and_is_not_billed(conn, serper_key):
    respx.post(SERPER).mock(return_value=httpx.Response(403))
    respx.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Acme", "url": "https://acme.com", "content": ""},
                ]
            },
        )
    )
    assert [r.url for r in search.buscar_web("acme")] == ["https://acme.com"]
    with conn.cursor() as cur:
        cur.execute("select payload, result from agent_actions where action = 'buscar_web'")
        payload, result = cur.fetchone()
        cur.execute("select count(*) from cost_events")
        billed = cur.fetchone()[0]
    assert payload["proveedor"] == "searxng"
    assert "serper" in result["fallo_serper"]
    assert billed == 0


@respx.mock
def test_both_providers_down_is_a_search_outage_naming_both(conn, serper_key):
    respx.post(SERPER).mock(return_value=httpx.Response(503))
    respx.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(502))
    with pytest.raises(search.SearchUnavailable, match="(?s)serper.*SearXNG"):
        search.buscar_web("acme")


@respx.mock
def test_without_a_serper_key_serper_is_never_called(conn):
    serper = respx.post(SERPER).mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(200, json={"results": []}))
    search.buscar_web("acme")
    assert not serper.called


@respx.mock
def test_a_leftover_brave_key_no_longer_calls_brave(conn, monkeypatch):
    """Brave se quitó del todo. Una clave vieja olvidada en el entorno no puede
    volver a activarlo: respx falla ante cualquier petición que no esté mockeada."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-leftover-key")
    respx.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"title": "Acme", "url": "https://acme.com", "content": ""}]},
        )
    )
    assert [r.url for r in search.buscar_web("acme")] == ["https://acme.com"]
