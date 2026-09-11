from decimal import Decimal

import httpx
import pytest
import respx

from handoff_agent.tools import search

SEARXNG = "http://127.0.0.1:8080"
BRAVE = "https://api.search.brave.com/res/v1/web/search"


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
def brave_key(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")


@respx.mock
def test_brave_is_used_when_its_key_is_configured(conn, brave_key):
    route = respx.get(BRAVE).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Acme",
                            "url": "https://acme.com",
                            "description": "We build things",
                        },
                    ]
                }
            },
        )
    )
    results = search.buscar_web("acme", limit=5)
    assert [r.url for r in results] == ["https://acme.com"]
    assert results[0].snippet == "We build things"
    assert route.calls[0].request.headers["X-Subscription-Token"] == "brave-test-key"


@respx.mock
def test_every_brave_query_lands_in_the_cost_ledger(conn, brave_key):
    respx.get(BRAVE).mock(return_value=httpx.Response(200, json={"web": {"results": []}}))
    search.buscar_web("acme")
    with conn.cursor() as cur:
        cur.execute("select source, cost_usd from cost_events")
        source, cost = cur.fetchone()
    assert source == "brave_search"
    assert Decimal(cost) == Decimal("0.005000")


@respx.mock
def test_brave_asks_for_at_most_twenty_results(conn, brave_key):
    route = respx.get(BRAVE).mock(return_value=httpx.Response(200, json={"web": {"results": []}}))
    search.buscar_web("acme", limit=50)
    assert route.calls[0].request.url.params["count"] == "20"


@respx.mock
def test_a_brave_failure_falls_back_to_searxng_and_is_not_billed(conn, brave_key):
    respx.get(BRAVE).mock(return_value=httpx.Response(503))
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
    assert "brave" in result["fallo_brave"]
    assert billed == 0


@respx.mock
def test_both_providers_down_is_a_search_outage_naming_both(conn, brave_key):
    respx.get(BRAVE).mock(return_value=httpx.Response(503))
    respx.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(502))
    with pytest.raises(search.SearchUnavailable, match="(?s)brave.*SearXNG"):
        search.buscar_web("acme")


@respx.mock
def test_without_a_brave_key_brave_is_never_called(conn):
    brave = respx.get(BRAVE).mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(200, json={"results": []}))
    search.buscar_web("acme")
    assert not brave.called
