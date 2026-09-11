"""Web search: Brave Search API when configured, self-hosted SearXNG otherwise.

SearXNG scrapes other engines' result pages from our own IP. Under load those
engines answer with CAPTCHA and SearXNG goes quiet: it happened in the first
real batch (2026-09-10), and from a datacenter IP it happens sooner. Brave's
official API has no CAPTCHA and a predictable price, so it goes first when a
key is set, and SearXNG stays as the free fallback.

Brave bills per query, so every successful call writes a cost_events row: no
paid call may bypass the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .. import ledger
from ..config import Settings, load_settings

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_MAX_COUNT = 20


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


def _search_brave(query: str, limit: int, settings: Settings) -> list[SearchResult]:
    response = httpx.get(
        BRAVE_URL,
        params={"q": query, "count": min(limit, BRAVE_MAX_COUNT)},
        headers={
            "X-Subscription-Token": settings.brave_search_api_key,
            "Accept": "application/json",
        },
        timeout=settings.http_timeout_seconds,
    )
    response.raise_for_status()
    items = (response.json().get("web") or {}).get("results") or []
    return [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("description", ""),
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
    brave_failure: str | None = None
    results: list[SearchResult] | None = None

    if settings.brave_search_api_key:
        try:
            results = _search_brave(query, limit, settings)
        except (httpx.HTTPError, ValueError) as exc:
            brave_failure = f"brave: {exc}"
        else:
            provider = "brave"
            ledger.record_cost_event(
                source="brave_search",
                cost_usd=settings.price_brave_per_query,
                description=f"búsqueda web: {query[:80]}",
                prospect_id=prospect_id,
            )

    if results is None:
        try:
            results = _search_searxng(query, limit, settings)
        except SearchUnavailable as exc:
            if brave_failure:
                raise SearchUnavailable(f"{brave_failure}; {exc}") from exc
            raise

    outcome = {"count": len(results)}
    if brave_failure:
        outcome["fallo_brave"] = brave_failure
    ledger.record_action(
        action="buscar_web",
        payload={"query": query, "limit": limit, "proveedor": provider},
        result=outcome,
        prospect_id=prospect_id,
    )
    return results
