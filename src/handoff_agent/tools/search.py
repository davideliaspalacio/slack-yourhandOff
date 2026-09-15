"""Web search: Serper (Google results) when configured, self-hosted SearXNG otherwise.

SearXNG scrapes other engines' result pages from our own IP. Under load those
engines answer with CAPTCHA and SearXNG goes quiet: it happened in the first
real batch (2026-09-10), and from a datacenter IP it happens sooner. Serper
returns Google's results over an API with no CAPTCHA, which is also what finds
a person's public LinkedIn snippet and their company's site most reliably. It
goes first when a key is set, and SearXNG stays as the free fallback.

Serper bills per query, so every successful call writes a cost_events row: no
paid call may bypass the ledger. The key travels in a header, never in the URL,
so an error message that quotes the request cannot leak it.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .. import ledger
from ..config import Settings, load_settings

SERPER_URL = "https://google.serper.dev/search"
# Hasta 10 resultados una búsqueda es un crédito. No está confirmado que pedir
# más cueste lo mismo, y el agente nunca pide más de 8.
SERPER_MAX_NUM = 10


class SearchUnavailable(RuntimeError):
    """No provider answered. Callers should degrade, not crash the pipeline."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


def _describe_engine(entry) -> str:
    """SearXNG da cada motor como un par [motor, motivo]."""
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        return f"{entry[0]} ({entry[1]})"
    return str(entry)


def _search_serper(query: str, limit: int, settings: Settings) -> list[SearchResult]:
    response = httpx.post(
        SERPER_URL,
        json={"q": query, "num": min(limit, SERPER_MAX_NUM)},
        headers={"X-API-KEY": settings.serper_api_key},
        timeout=settings.http_timeout_seconds,
    )
    response.raise_for_status()
    items = response.json().get("organic") or []
    return [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("link", ""),
            snippet=item.get("snippet", ""),
        )
        for item in items[:limit]
    ]


def _search_searxng(query: str, limit: int, settings: Settings) -> list[SearchResult]:
    try:
        response = httpx.get(
            f"{settings.searxng_url}/search",
            params={"q": query, "format": "json"},
            timeout=settings.http_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchUnavailable(f"SearXNG did not answer: {exc}") from exc

    items = payload.get("results") or []
    stalled = payload.get("unresponsive_engines") or []
    if not items and stalled:
        # Con los motores vetados (CAPTCHA) SearXNG contesta 200 y cero
        # resultados. Eso no es "no hay nada": es que no hubo búsqueda.
        raise SearchUnavailable(
            "SearXNG sin resultados; motores sin respuesta: "
            + ", ".join(_describe_engine(entry) for entry in stalled)
        )
    return [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("content", ""),
        )
        for item in items[:limit]
    ]


def buscar_web(query: str, limit: int = 8, prospect_id: str | None = None) -> list[SearchResult]:
    """Search the public web. Returns at most `limit` results, best first.

    `prospect_id` attributes the action (and any cost) to a person so it shows
    up in that person's history; without it the action is logged unattributed.
    """
    settings = load_settings()
    provider = "searxng"
    serper_failure: str | None = None
    results: list[SearchResult] | None = None

    if settings.serper_api_key:
        try:
            results = _search_serper(query, limit, settings)
        except (httpx.HTTPError, ValueError) as exc:
            serper_failure = f"serper: {exc}"
        else:
            provider = "serper"
            ledger.record_cost_event(
                source="serper_search",
                cost_usd=settings.price_serper_per_query,
                description=f"búsqueda web: {query[:80]}",
                prospect_id=prospect_id,
            )

    if results is None:
        try:
            results = _search_searxng(query, limit, settings)
        except SearchUnavailable as exc:
            if serper_failure:
                raise SearchUnavailable(f"{serper_failure}; {exc}") from exc
            raise

    outcome = {"count": len(results)}
    if serper_failure:
        outcome["fallo_serper"] = serper_failure
    ledger.record_action(
        action="buscar_web",
        payload={"query": query, "limit": limit, "proveedor": provider},
        result=outcome,
        prospect_id=prospect_id,
    )
    return results
