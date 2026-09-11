"""Budget guardrails.

An agentic loop with no ceiling can eat a month of budget in one bad night.
These are the three brakes from the spec: kill switch, monthly cap, per-run cap.
Kept apart from ledger.py on purpose — the ledger records, the guards decide,
and recording must never be able to block anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Self

from . import db, ledger


class KillSwitchActive(RuntimeError):
    """The kill_switch row in config is true. Everything stops."""


class MonthlyBudgetExceeded(RuntimeError):
    """Spend this calendar month crossed the configured ceiling."""


class RunBudgetExceeded(RuntimeError):
    """One research run cost more than its cap. It is cut short, not retried."""


def _config_value(key: str, default):
    row = db.fetch_one("select value from config where key = %s", (key,))
    return default if row is None else row["value"]


def check_kill_switch() -> None:
    if _config_value("kill_switch", False) is True:
        raise KillSwitchActive("kill_switch is on; set it to false in the config table")


def monthly_spend() -> Decimal:
    now = datetime.now(UTC)
    return ledger.spend_since(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def check_monthly_budget() -> None:
    limit = Decimal(str(_config_value("monthly_budget_usd", 150)))
    spent = monthly_spend()
    if spent >= limit:
        raise MonthlyBudgetExceeded(f"spent ${spent} of ${limit} this month")


def run_budget_limit() -> Decimal:
    """Per-run cap from the config table, so it can change without a redeploy."""
    return Decimal(str(_config_value("run_budget_usd", 1.0)))


class RunBudget:
    """Caps a single research run. Use as a context manager around the loop."""

    def __init__(self, limit_usd: Decimal, prospect_id: str | None = None) -> None:
        self.limit_usd = limit_usd
        self.prospect_id = prospect_id
        self.spent = Decimal(0)

    def add(self, cost: Decimal) -> None:
        self.spent += cost
        if self.spent > self.limit_usd:
            raise RunBudgetExceeded(f"run spent ${self.spent}, cap is ${self.limit_usd}")

    def __enter__(self) -> Self:
        check_kill_switch()
        check_monthly_budget()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Sin esto el gasto por ejecución no queda en ninguna parte y no se
        # puede responder "¿cuánto costó investigar a esta persona?".
        ledger.record_action(
            action="run_budget",
            payload={"limit_usd": str(self.limit_usd)},
            result={
                "spent_usd": str(self.spent),
                "exceeded": exc_type is RunBudgetExceeded,
            },
            prospect_id=self.prospect_id,
        )
