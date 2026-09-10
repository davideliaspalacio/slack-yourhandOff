"""Environment-backed settings.

Only DATABASE_URL is required to load: without it nothing in this codebase can
run. Third-party credentials are validated at the point of use instead, so that
a tool which needs no LLM — buscar_web, leer_sitio, buscar_ofertas — keeps
working while the other integrations are still being wired up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Loaded once at import so every entry point — workers, MCP server, tests run
# against a real database — sees the same environment without extra ceremony.
load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    openai_api_key: str | None
    openai_model: str
    searxng_url: str
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_host: str
    price_input_per_m: float
    price_cached_input_per_m: float
    price_output_per_m: float
    http_timeout_seconds: float


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def load_settings() -> Settings:
    return Settings(
        database_url=_required("DATABASE_URL"),
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4.1"),
        searxng_url=os.environ.get("SEARXNG_URL", "http://127.0.0.1:8080"),
        langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY") or None,
        langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY") or None,
        langfuse_host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        price_input_per_m=float(os.environ.get("PRICE_INPUT_PER_M", "2.00")),
        price_cached_input_per_m=float(os.environ.get("PRICE_CACHED_INPUT_PER_M", "0.50")),
        price_output_per_m=float(os.environ.get("PRICE_OUTPUT_PER_M", "8.00")),
        http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "30")),
    )
