"""Web search through a self-hosted SearXNG.

Self-hosted rather than Tavily/Serper/Exa: no API key, no per-query billing,
and the queries never leave our infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .. import ledger
from ..config import load_settings


class SearchUnavailable(RuntimeError):
    """SearXNG did not answer. Callers should degrade, not crash the pipeline."""


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


def buscar_web(query: str, limit: int = 8, prospect_id: str | None = None) -> list[SearchResult]:
    """Search the public web. Returns at most `limit` results, best first.

    `prospect_id` attributes the action to a person so it shows up in that
    person's history; without it the action is logged but unattributed.
    """
    settings = load_settings()
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

    results = [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("content", ""),
        )
        for item in items[:limit]
    ]
    ledger.record_action(
        action="buscar_web",
        payload={"query": query, "limit": limit},
        result={"count": len(results)},
        prospect_id=prospect_id,
    )
    return results
