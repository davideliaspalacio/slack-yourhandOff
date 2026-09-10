"""Fetch a page and return clean, LLM-ready text.

trafilatura strips navigation, cookie banners and boilerplate, which is most of
what a corporate site is made of. Feeding raw HTML to the model would cost
several times more per page and read worse.

Everything this module fetches is attacker-influenced: URLs arrive from web
search results and from links other people paste into a third-party Slack. So
the fetch is deliberately hostile-input-shaped — scheme allowlist, per-hop
address checks, a byte ceiling and a total deadline — rather than a plain GET.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import trafilatura

from .. import ledger
from ..config import load_settings

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_RESPONSE_BYTES = 5_000_000
MAX_REDIRECTS = 5
DNS_TIMEOUT_SECONDS = 5.0


class PageUnavailable(RuntimeError):
    """The page could not be fetched, was refused, or held no extractable text."""


@dataclass(frozen=True)
class PageContent:
    url: str
    final_url: str
    title: str
    text: str


def _resolve(host: str) -> list[str]:
    """Split out so tests can substitute it and stay off the network.

    getaddrinfo ignores every timeout Python offers and blocks for the OS
    resolver's own limit. A global default keeps a hanging resolver from
    holding a worker for that limit on each of up to six hops.
    """
    socket.setdefaulttimeout(DNS_TIMEOUT_SECONDS)
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _assert_publicly_routable(url: str) -> None:
    """Refuse anything that points inside our own infrastructure.

    On Railway an unchecked fetch reaches the instance metadata endpoint; on any
    host it reaches Supabase, Studio and SearXNG on loopback. This runs per
    redirect hop, because checking only the URL the caller passed in leaves the
    redirect target unchecked.

    Residual risk: DNS rebinding between this check and the connect. Closing it
    needs the resolved IP pinned into the connection, which httpx does not
    expose; the byte ceiling and deadline below bound the damage.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise PageUnavailable(f"refusing scheme {parsed.scheme!r} for {url}")
    host = parsed.hostname
    if not host:
        raise PageUnavailable(f"no host in {url}")

    try:
        addresses = _resolve(host)
    except socket.gaierror as exc:
        raise PageUnavailable(f"could not resolve {host}: {exc}") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address)
        # `not is_global` en vez de una lista de is_private/is_loopback/etc.:
        # esa lista deja pasar CGNAT 100.64.0.0/10 —que es exactamente lo que
        # usan Fly.io, muchos clústeres de k8s y Tailscale— y también 240/4 y
        # los rangos de documentación. is_global los cubre todos de una vez.
        if not ip.is_global:
            raise PageUnavailable(
                f"refusing to fetch {url}: {host} resolves to non-public address {ip}"
            )


def _fetch(url: str, deadline: float) -> tuple[str, str]:
    """Follow redirects by hand, vetting every hop. Returns (final_url, body)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if time.monotonic() >= deadline:
            raise PageUnavailable(f"deadline exceeded fetching {url}")
        _assert_publicly_routable(current)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PageUnavailable(f"deadline exceeded fetching {url}")

        with httpx.stream(
            "GET",
            current,
            timeout=remaining,
            follow_redirects=False,
            headers={"User-Agent": "HandoffResearchBot/0.1"},
        ) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise PageUnavailable(f"redirect without location at {current}")
                current = str(response.url.join(location))
                continue

            response.raise_for_status()

            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise PageUnavailable(
                        f"{current} exceeded {MAX_RESPONSE_BYTES} bytes; refusing to buffer it"
                    )
                if time.monotonic() > deadline:
                    raise PageUnavailable(f"deadline exceeded reading {current}")

            try:
                return current, body.decode(response.encoding or "utf-8", errors="replace")
            except LookupError:
                # Un charset= inventado en la cabecera no es un httpx.HTTPError,
                # así que sin esto sale como LookupError crudo y tumba al
                # llamante en vez de degradar.
                return current, body.decode("utf-8", errors="replace")

    raise PageUnavailable(f"too many redirects starting at {url}")


def leer_sitio(url: str, max_chars: int = 20_000, prospect_id: str | None = None) -> PageContent:
    """Fetch `url` and return its readable text, truncated to `max_chars`."""
    settings = load_settings()
    deadline = time.monotonic() + settings.http_timeout_seconds

    try:
        final_url, html = _fetch(url, deadline)
    except httpx.HTTPError as exc:
        raise PageUnavailable(f"could not fetch {url}: {exc}") from exc

    extracted = trafilatura.extract(html, include_comments=False) or ""
    metadata = trafilatura.extract_metadata(html)
    title = getattr(metadata, "title", None) or ""

    if not extracted.strip():
        raise PageUnavailable(f"no extractable text at {url}")

    text = extracted[:max_chars]
    ledger.record_action(
        action="leer_sitio",
        # final_url is recorded alongside the requested one: a redirect means
        # the text came from somewhere other than the domain we asked for, and
        # the dossier's sources have to say where it actually came from.
        payload={"url": url, "final_url": final_url},
        result={"chars": len(text), "title": title},
        prospect_id=prospect_id,
    )
    return PageContent(url=url, final_url=final_url, title=title, text=text)
