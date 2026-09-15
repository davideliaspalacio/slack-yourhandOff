"""Optional Langfuse tracing.

Supabase holds the numbers; Langfuse holds the execution tree that explains
them. Tracing is best-effort by design: if Langfuse is unreachable, misconfigured
or its SDK changes under us, the pipeline carries on and the ledger is still
complete.

Written against the Langfuse SDK v4, which records OpenTelemetry observations.
Langfuse shows an observation's duration as the call's latency, so a generation
is opened just before the model call and closed right after it: opening it once
the call has returned would report every call as instant.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from .config import load_settings

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    settings = load_settings()
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


@lru_cache(maxsize=1)
def _client():
    # Un solo cliente por proceso. El SDK 4 comparte su exportador entre los
    # clientes de una misma clave pública: crear uno por llamada no aísla nada
    # y solo añade hilos. Envía en segundo plano y vacía lo pendiente al salir.
    from langfuse import Langfuse

    settings = load_settings()
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
    )


def start_generation(
    *, stage: str, model: str, prompt: str, prospect_id: str | None = None
) -> Any | None:
    """Open a generation just before calling the model. None when tracing is off
    or Langfuse fails; finish_generation accepts that None."""
    if not is_enabled():
        return None
    try:
        return _client().start_observation(
            name=stage,
            as_type="generation",
            model=model,
            input=prompt,
            metadata={"prospect_id": prospect_id} if prospect_id else None,
        )
    except Exception as exc:  # noqa: BLE001 - el trazado nunca rompe el pipeline
        logger.warning("langfuse: no se pudo abrir la traza, se sigue sin ella: %s", exc)
        return None


def finish_generation(
    generation: Any | None,
    *,
    completion: str | None = None,
    usage: dict | None = None,
    error: BaseException | None = None,
) -> str | None:
    """Close a generation with the model's answer, or as an error. Returns its
    trace id for the ledger, or None."""
    if generation is None:
        return None
    try:
        if error is not None:
            generation.update(level="ERROR", status_message=f"{type(error).__name__}: {error}")
        else:
            generation.update(
                output=completion,
                usage_details=(
                    {
                        "input": usage.get("input", 0),
                        "cache_read_input_tokens": usage.get("cached", 0),
                        "output": usage.get("output", 0),
                    }
                    if usage
                    else None
                ),
            )
        generation.end()
        return generation.trace_id
    except Exception as exc:  # noqa: BLE001 - el trazado nunca rompe el pipeline
        logger.warning("langfuse: no se pudo cerrar la traza, se sigue sin ella: %s", exc)
        return None
