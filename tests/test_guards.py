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


def test_run_budget_honours_the_kill_switch_on_entry(conn):
    """El __enter__ era un no-op verificado por nadie: se podía vaciar entero
    y la suite seguía verde."""
    with conn.cursor() as cur:
        cur.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    try:
        with pytest.raises(guards.KillSwitchActive), guards.RunBudget(limit_usd=Decimal("1.00")):
            pass
    finally:
        with conn.cursor() as cur:
            cur.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")


def test_run_budget_checks_the_monthly_cap_on_entry(conn):
    with conn.cursor() as cur:
        cur.execute("update config set value = '1.0'::jsonb where key = 'monthly_budget_usd'")
    ledger.record_cost_event(source="test", cost_usd=2.00)
    try:
        with (
            pytest.raises(guards.MonthlyBudgetExceeded),
            guards.RunBudget(limit_usd=Decimal("1.00")),
        ):
            pass
    finally:
        with conn.cursor() as cur:
            cur.execute("update config set value = '150'::jsonb where key = 'monthly_budget_usd'")


def test_run_budget_records_what_the_run_spent(conn):
    """Sin esto no se puede responder cuánto costó investigar a una persona."""
    from handoff_agent.tools import prospects

    person = prospects.upsert_prospect("U_BUDGET")
    with guards.RunBudget(limit_usd=Decimal("1.00"), prospect_id=person["id"]) as budget:
        budget.add(Decimal("0.30"))
    with conn.cursor() as cur:
        cur.execute("select result, prospect_id from agent_actions where action = 'run_budget'")
        result, prospect_id = cur.fetchone()
    assert result["spent_usd"] == "0.30"
    assert result["exceeded"] is False
    assert str(prospect_id) == str(person["id"])


def test_run_budget_records_that_the_cap_was_blown(conn):
    with (
        pytest.raises(guards.RunBudgetExceeded),
        guards.RunBudget(limit_usd=Decimal("0.10")) as budget,
    ):
        budget.add(Decimal("0.50"))
    with conn.cursor() as cur:
        cur.execute("select result from agent_actions where action = 'run_budget'")
        assert cur.fetchone()[0]["exceeded"] is True
