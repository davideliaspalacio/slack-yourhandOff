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
    langfuse_base_url: str
    price_input_per_m: float
    price_cached_input_per_m: float
    price_output_per_m: float
    http_timeout_seconds: float
    serper_api_key: str | None
    price_serper_per_query: float
    slack_user_token: str | None
    slack_channel_ids: tuple[str, ...]
    slack_lookback_hours: float
    slack_poll_seconds: int
    alert_webhook_url: str | None
    handoff_bot_token: str | None
    handoff_channel_id: str | None
    slack_signing_secret: str | None
    twilio_account_sid: str | None
    twilio_auth_token: str | None
    twilio_from: str | None
    twilio_to: str | None
    price_twilio_per_sms: float
    resend_api_key: str | None
    digest_from: str | None
    digest_to: tuple[str, ...]
    price_resend_per_email: float
    enrichment_webhook_url: str | None
    enrichment_timeout_seconds: float
    price_enrichment_per_call: float
    panel_url: str | None


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
        # LANGFUSE_BASE_URL es el nombre del SDK 4 y el que muestra el panel de
        # Langfuse; LANGFUSE_HOST, el antiguo, sigue valiendo si es el único.
        # Una variable vacía cuenta como ausente.
        langfuse_base_url=os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or "https://cloud.langfuse.com",
        price_input_per_m=float(os.environ.get("PRICE_INPUT_PER_M", "2.00")),
        price_cached_input_per_m=float(os.environ.get("PRICE_CACHED_INPUT_PER_M", "0.50")),
        price_output_per_m=float(os.environ.get("PRICE_OUTPUT_PER_M", "8.00")),
        http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "30")),
        serper_api_key=os.environ.get("SERPER_API_KEY") or None,
        price_serper_per_query=float(os.environ.get("PRICE_SERPER_PER_QUERY", "0.001")),
        slack_user_token=os.environ.get("SLACK_USER_TOKEN") or None,
        slack_channel_ids=tuple(
            c.strip() for c in os.environ.get("SLACK_CHANNEL_IDS", "").split(",") if c.strip()
        ),
        slack_lookback_hours=float(os.environ.get("SLACK_LOOKBACK_HOURS", "1")),
        slack_poll_seconds=int(os.environ.get("SLACK_POLL_SECONDS", "3600")),
        alert_webhook_url=os.environ.get("HANDOFF_ALERT_WEBHOOK_URL") or None,
        handoff_bot_token=os.environ.get("HANDOFF_SLACK_BOT_TOKEN") or None,
        handoff_channel_id=os.environ.get("HANDOFF_SLACK_CHANNEL_ID") or None,
        slack_signing_secret=os.environ.get("SLACK_SIGNING_SECRET") or None,
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID") or None,
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN") or None,
        twilio_from=os.environ.get("TWILIO_FROM") or None,
        twilio_to=os.environ.get("TWILIO_TO") or None,
        # Verificado contra twilio.com/en-us/sms/pricing (SMS saliente en EE. UU.)
        # el 2026-09-16. Vive en el entorno como el resto de precios (Serper,
        # tokens de OpenAI): así se puede corregir sin tocar código.
        price_twilio_per_sms=float(os.environ.get("PRICE_TWILIO_PER_SMS", "0.0079")),
        resend_api_key=os.environ.get("RESEND_API_KEY") or None,
        digest_from=os.environ.get("DIGEST_FROM") or None,
        digest_to=tuple(
            e.strip().lower() for e in os.environ.get("DIGEST_TO", "").split(",") if e.strip()
        ),
        # Verificado contra resend.com/pricing el 2026-09-18: el nivel
        # gratuito de Resend cubre un email diario de sobra, de ahí el 0.0
        # por defecto -- igual que PRICE_TWILIO_PER_SMS, vive en el entorno
        # para poder corregirse sin tocar código si algún día cambia.
        price_resend_per_email=float(os.environ.get("PRICE_RESEND_PER_EMAIL", "0.0")),
        # Webhook privado de Handoff que enriquece una empresa a partir de su
        # dominio. Repo público: su URL real nunca se comitea, solo vive en el
        # entorno; sin ella, enrich_company se omite (ver tools/enrichment.py).
        enrichment_webhook_url=os.environ.get("ENRICHMENT_WEBHOOK_URL") or None,
        # Visto tardar hasta ~100s y caído por días con un 500 "Error in
        # workflow": el timeout es generoso a propósito.
        enrichment_timeout_seconds=float(os.environ.get("ENRICHMENT_TIMEOUT_SECONDS", "120")),
        # El flujo real llama a GPT-4.1 más un proveedor de datos de empresa;
        # su coste no se conoce todavía, de ahí el 0.0 -- corregir en cuanto
        # se sepa, igual que PRICE_TWILIO_PER_SMS y PRICE_RESEND_PER_EMAIL.
        price_enrichment_per_call=float(os.environ.get("PRICE_ENRICHMENT_PER_CALL", "0.0")),
        # URL pública del panel (Railway o donde se sirva): la tarjeta de Slack
        # la usa para el botón "Open in panel" (ver delivery/card.py). Sin ella,
        # el botón simplemente no aparece -- una cadena vacía cuenta como
        # ausente, y una barra final se recorta para no duplicarla al montar
        # la URL de la persona.
        panel_url=(os.environ.get("PANEL_URL") or "").rstrip("/") or None,
    )
