from decimal import Decimal

import pytest

from handoff_agent import guards, ledger


def test_kill_switch_passes_when_off(conn):
    guards.check_kill_switch()


def test_kill_switch_raises_when_on(conn):
    with conn.cursor() as cur:
        cur.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    try:
        with pytest.raises(guards.KillSwitchActive):
            guards.check_kill_switch()
    finally:
        with conn.cursor() as cur:
            cur.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")


def test_monthly_budget_raises_once_exceeded(conn):
    with conn.cursor() as cur:
        cur.execute("update config set value = '1.0'::jsonb where key = 'monthly_budget_usd'")
    try:
        ledger.record_cost_event(source="test", cost_usd=2.00)
        with pytest.raises(guards.MonthlyBudgetExceeded):
            guards.check_monthly_budget()
    finally:
        with conn.cursor() as cur:
            cur.execute("update config set value = '150'::jsonb where key = 'monthly_budget_usd'")


def test_run_budget_raises_when_the_cap_is_crossed(conn):
    budget = guards.RunBudget(limit_usd=Decimal("0.10"))
    budget.add(Decimal("0.06"))
    assert budget.spent == Decimal("0.06")
    with pytest.raises(guards.RunBudgetExceeded):
        budget.add(Decimal("0.06"))


def test_run_budget_reports_spend_on_exit(conn):
    with guards.RunBudget(limit_usd=Decimal("1.00")) as budget:
        budget.add(Decimal("0.30"))
    assert budget.spent == Decimal("0.30")
