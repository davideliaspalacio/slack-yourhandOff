import httpx
import pytest
import respx

from handoff_agent.tools import search

SEARXNG = "http://127.0.0.1:8080"


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
