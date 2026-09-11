"""Cost ledger. Every paid call lands here before anything else happens.

Supabase is the source of truth for spend because the metric that matters —
cost per closed deal — is a JOIN against prospects, feedback and deliveries.
Langfuse holds the execution trees; it cannot answer that question.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from . import db
from .config import load_settings

MILLION = Decimal(1_000_000)


def compute_llm_cost(input_tokens: int, cached_tokens: int, output_tokens: int) -> Decimal:
    settings = load_settings()
    return (
        Decimal(input_tokens) / MILLION * Decimal(str(settings.price_input_per_m))
        + Decimal(cached_tokens) / MILLION * Decimal(str(settings.price_cached_input_per_m))
        + Decimal(output_tokens) / MILLION * Decimal(str(settings.price_output_per_m))
    )


def record_llm_call(
    stage: str,
    model: str,
    input_tokens: int,
    cached_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int | None = None,
    prospect_id: str | None = None,
    trace_id: str | None = None,
) -> Decimal:
    cost = compute_llm_cost(input_tokens, cached_tokens, output_tokens)
    db.execute(
        """
        insert into llm_calls
            (prospect_id, stage, model, input_tokens, cached_tokens,
             output_tokens, cost_usd, latency_ms, trace_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            prospect_id,
            stage,
            model,
            input_tokens,
            cached_tokens,
            output_tokens,
            cost,
            latency_ms,
            trace_id,
        ),
    )
    return cost


def record_cost_event(
    source: str,
    cost_usd: Decimal | float,
    description: str | None = None,
    prospect_id: str | None = None,
) -> None:
    db.execute(
        "insert into cost_events (prospect_id, source, description, cost_usd) "
        "values (%s, %s, %s, %s)",
        (prospect_id, source, description, Decimal(str(cost_usd))),
    )


def record_action(
    action: str,
    payload: dict,
    result: dict | None = None,
    prospect_id: str | None = None,
) -> None:
    db.execute(
        "insert into agent_actions (prospect_id, action, payload, result) values (%s, %s, %s, %s)",
        (
            prospect_id,
            action,
            json.dumps(payload),
            json.dumps(result) if result is not None else None,
        ),
    )


def spend_since(since: datetime) -> Decimal:
    row = db.fetch_one(
        """
        select coalesce(
            (select sum(cost_usd) from llm_calls   where created_at >= %s), 0
        ) + coalesce(
            (select sum(cost_usd) from cost_events where created_at >= %s), 0
        ) as total
        """,
        (since, since),
    )
    return Decimal(row["total"])


def cost_summary(days: int = 30) -> dict:
    """Gasto de los últimos N días, sumando llamadas a LLM y costes externos."""
    since = datetime.now(UTC) - timedelta(days=days)
    total = spend_since(since)
    counts = db.fetch_one(
        "select (select count(*) from llm_calls where created_at >= %s) as llm_calls, "
        "       (select count(*) from cost_events where created_at >= %s) as cost_events",
        (since, since),
    )
    return {
        "days": days,
        "total_usd": f"{total:.2f}",
        "llm_calls": counts["llm_calls"],
        "cost_events": counts["cost_events"],
    }
