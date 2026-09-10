"""Optional Langfuse tracing.

Supabase holds the numbers; Langfuse holds the execution tree that explains
them. Tracing is best-effort by design: if Langfuse is unreachable or
unconfigured, the pipeline carries on and the ledger is still complete.
"""

from __future__ import annotations

import logging

from .config import load_settings

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    settings = load_settings()
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def trace_llm_call(
    *,
    stage: str,
    model: str,
    prompt: str,
    completion: str,
    usage: dict,
    latency_ms: int,
    prospect_id: str | None = None,
) -> str | None:
    """Send one generation to Langfuse. Returns its trace id, or None."""
    if not is_enabled():
        return None

    settings = load_settings()
    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        trace = client.trace(name=stage, metadata={"prospect_id": prospect_id})
        trace.generation(
            name=stage,
            model=model,
            input=prompt,
            output=completion,
            usage_details={
                "input": usage.get("input", 0),
                "cache_read_input_tokens": usage.get("cached", 0),
                "output": usage.get("output", 0),
            },
            metadata={"latency_ms": latency_ms},
        )
        client.flush()
        return trace.id
    except Exception as exc:  # noqa: BLE001 - tracing never breaks the pipeline
        logger.warning("langfuse tracing failed, continuing: %s", exc)
        return None
