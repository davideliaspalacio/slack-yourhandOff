from datetime import UTC, datetime, timedelta
from decimal import Decimal

from handoff_agent import ledger


def test_compute_llm_cost_charges_cached_tokens_at_the_lower_rate():
    cost = ledger.compute_llm_cost(1_000_000, 1_000_000, 1_000_000)
    assert cost == Decimal("10.50")


def test_compute_llm_cost_is_zero_for_no_tokens():
    assert ledger.compute_llm_cost(0, 0, 0) == Decimal(0)


def test_record_llm_call_persists_a_row_and_returns_cost(conn):
    cost = ledger.record_llm_call(
        stage="triage", model="gpt-4.1", input_tokens=1000, cached_tokens=0, output_tokens=500
    )
    with conn.cursor() as cur:
        cur.execute("select stage, model, cost_usd from llm_calls")
        row = cur.fetchone()
    assert row[0] == "triage"
    assert row[1] == "gpt-4.1"
    assert Decimal(row[2]) == cost
    assert cost > 0


def test_record_cost_event_persists_non_llm_spend(conn):
    """0.1 + 0.2 no es representable en binario: con float() el valor guardado
    saldría 0.300000000000000044..., así que este caso sí prueba que la
    conversión a Decimal ocurre. Con 0.0079 el test pasaba igual sin ella."""
    ledger.record_cost_event(source="twilio", cost_usd=0.1 + 0.2, description="SMS a Anthony")
    with conn.cursor() as cur:
        cur.execute("select source, cost_usd from cost_events")
        row = cur.fetchone()
    assert row[0] == "twilio"
    assert Decimal(row[1]) == Decimal("0.300000")


def test_record_action_appends_to_the_log(conn):
    ledger.record_action(action="buscar_web", payload={"query": "acme corp"}, result={"hits": 3})
    with conn.cursor() as cur:
        cur.execute("select action, payload, result from agent_actions")
        row = cur.fetchone()
    assert row[0] == "buscar_web"
    assert row[1] == {"query": "acme corp"}
    assert row[2] == {"hits": 3}


def test_spend_since_sums_both_ledgers(conn):
    ledger.record_llm_call(
        stage="research",
        model="gpt-4.1",
        input_tokens=1_000_000,
        cached_tokens=0,
        output_tokens=0,
    )
    ledger.record_cost_event(source="twilio", cost_usd=1.00)
    total = ledger.spend_since(datetime.now(UTC) - timedelta(hours=1))
    assert total == Decimal("3.00")


def test_cost_summary_adds_up_both_ledgers(conn):
    ledger.record_llm_call(
        stage="research", model="gpt-4.1", input_tokens=1_000_000, cached_tokens=0
    )
    ledger.record_cost_event(source="twilio", cost_usd=0.50)
    assert ledger.cost_summary(days=30) == {
        "days": 30,
        "total_usd": "2.50",
        "llm_calls": 1,
        "cost_events": 1,
    }
