"""Fetch a page and return clean, LLM-ready text.

trafilatura strips navigation, cookie banners and boilerplate, which is most of
what a corporate site is made of. Feeding raw HTML to the model would cost
several times more per page and read worse.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
import trafilatura

from .. import ledger
from ..config import load_settings


class PageUnavailable(RuntimeError):
    """The page could not be fetched or held no extractable text."""


@dataclass(frozen=True)
class PageContent:
    url: str
    title: str
    text: str


def leer_sitio(url: str, max_chars: int = 20_000) -> PageContent:
    """Fetch `url` and return its readable text, truncated to `max_chars`."""
    settings = load_settings()
    try:
        response = httpx.get(
            url,
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "HandoffResearchBot/0.1"},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise PageUnavailable(f"could not fetch {url}: {exc}") from exc

    extracted = trafilatura.extract(response.text, include_comments=False) or ""
    metadata = trafilatura.extract_metadata(response.text)
    title = getattr(metadata, "title", None) or ""

    if not extracted.strip():
        raise PageUnavailable(f"no extractable text at {url}")

    text = extracted[:max_chars]
    ledger.record_action(
        action="leer_sitio", payload={"url": url},
        result={"chars": len(text), "title": title},
    )
    return PageContent(url=url, title=title, text=text)
