"""The only way this codebase talks to an LLM.

Every call goes through here so that three things always happen: the kill
switch is honoured, the cost lands in the ledger, and the call shows up in
Langfuse. Calling the OpenAI SDK directly anywhere else is a bug.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from openai import OpenAI

from . import guards, ledger, tracing
from .config import load_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    cost_usd: Decimal
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    latency_ms: int


class OpenAINotConfigured(RuntimeError):
    """OPENAI_API_KEY is missing. Only reaches callers that actually need an LLM."""


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    settings = load_settings()
    if not settings.openai_api_key:
        raise OpenAINotConfigured(
            "OPENAI_API_KEY is not set; add it to .env before using LLM-backed tools"
        )
    return OpenAI(api_key=settings.openai_api_key, timeout=settings.http_timeout_seconds)


def complete(
    prompt: str,
    *,
    stage: str,
    system: str | None = None,
    prospect_id: str | None = None,
    json_mode: bool = False,
) -> LLMResponse:
    guards.check_kill_switch()
    # El tope mensual solo existe si alguien lo comprueba, y esta es la única
    # puerta por la que se gasta dinero en LLM.
    guards.check_monthly_budget()
    settings = load_settings()

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {"model": settings.openai_model, "messages": messages}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    # La traza se abre antes de llamar: Langfuse mide la latencia por su duración.
    generation = tracing.start_generation(
        stage=stage, model=settings.openai_model, prompt=prompt, prospect_id=prospect_id
    )
    started = time.perf_counter()
    try:
        response = _client().chat.completions.create(**kwargs)
    except Exception as exc:
        tracing.finish_generation(generation, error=exc)
        raise
    latency_ms = int((time.perf_counter() - started) * 1000)

    usage = response.usage
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    # OpenAI reports cached tokens inside prompt_tokens. Split them so the
    # cheaper rate applies to exactly the tokens that were cached.
    fresh_input = max(usage.prompt_tokens - cached, 0)
    text = response.choices[0].message.content or ""

    trace_id = tracing.finish_generation(
        generation,
        completion=text,
        usage={"input": fresh_input, "cached": cached, "output": usage.completion_tokens},
    )

    try:
        cost = ledger.record_llm_call(
            stage=stage,
            model=settings.openai_model,
            input_tokens=fresh_input,
            cached_tokens=cached,
            output_tokens=usage.completion_tokens,
            latency_ms=latency_ms,
            prospect_id=prospect_id,
            trace_id=trace_id,
        )
    except Exception:
        # La llamada ya está pagada. Reventar aquí perdería el dinero y además
        # la respuesta, así que se registra en el log con todo lo necesario
        # para reconstruir la fila a mano y se sigue. Es un agujero contable
        # visible, no uno silencioso.
        cost = ledger.compute_llm_cost(fresh_input, cached, usage.completion_tokens)
        logger.critical(
            "LEDGER GAP: llamada a %s cobrada pero no registrada — "
            "stage=%s input=%d cached=%d output=%d cost_usd=%s prospect_id=%s trace_id=%s",
            settings.openai_model,
            stage,
            fresh_input,
            cached,
            usage.completion_tokens,
            cost,
            prospect_id,
            trace_id,
            exc_info=True,
        )

    return LLMResponse(
        text=text,
        cost_usd=cost,
        input_tokens=fresh_input,
        cached_tokens=cached,
        output_tokens=usage.completion_tokens,
        latency_ms=latency_ms,
    )
