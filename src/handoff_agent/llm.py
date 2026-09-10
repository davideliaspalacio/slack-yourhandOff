"""The only way this codebase talks to an LLM.

Every call goes through here so that three things always happen: the kill
switch is honoured, the cost lands in the ledger, and the call shows up in
Langfuse. Calling the OpenAI SDK directly anywhere else is a bug.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from openai import OpenAI

from . import guards, ledger, tracing
from .config import load_settings


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
    settings = load_settings()

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {"model": settings.openai_model, "messages": messages}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    started = time.perf_counter()
    response = _client().chat.completions.create(**kwargs)
    latency_ms = int((time.perf_counter() - started) * 1000)

    usage = response.usage
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    # OpenAI reports cached tokens inside prompt_tokens. Split them so the
    # cheaper rate applies to exactly the tokens that were cached.
    fresh_input = max(usage.prompt_tokens - cached, 0)
    text = response.choices[0].message.content or ""

    trace_id = tracing.trace_llm_call(
        stage=stage,
        model=settings.openai_model,
        prompt=prompt,
        completion=text,
        usage={"input": fresh_input, "cached": cached, "output": usage.completion_tokens},
        latency_ms=latency_ms,
        prospect_id=prospect_id,
    )

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

    return LLMResponse(
        text=text,
        cost_usd=cost,
        input_tokens=fresh_input,
        cached_tokens=cached,
        output_tokens=usage.completion_tokens,
        latency_ms=latency_ms,
    )
