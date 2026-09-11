from decimal import Decimal

import pytest

from handoff_agent import ledger, mcp_server
from handoff_agent.research.worker import ResearchOutcome


@pytest.mark.asyncio
async def test_server_exposes_the_expected_tools():
    tools = await mcp_server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {
        "buscar_web",
        "leer_sitio",
        "buscar_ofertas",
        "historial_prospecto",
        "resumen_costes",
        "investigar_persona",
    } <= names


@pytest.mark.asyncio
async def test_every_tool_has_a_description():
    tools = await mcp_server.mcp.list_tools()
    undocumented = [t.name for t in tools if not t.description]
    assert undocumented == [], f"herramientas sin descripción: {undocumented}"


def test_resumen_costes_reports_zero_on_an_empty_ledger(conn):
    summary = mcp_server.resumen_costes(days=30)
    assert summary["total_usd"] == "0.00"
    assert summary["llm_calls"] == 0


def test_resumen_costes_adds_up_both_ledgers(conn):
    ledger.record_llm_call(
        stage="research",
        model="gpt-4.1",
        input_tokens=1_000_000,
        cached_tokens=0,
        output_tokens=0,
    )
    ledger.record_cost_event(source="twilio", cost_usd=0.50)
    summary = mcp_server.resumen_costes(days=30)
    assert summary["total_usd"] == "2.50"
    assert summary["llm_calls"] == 1


def test_investigar_persona_returns_outcome_and_dossier(conn, monkeypatch):
    monkeypatch.setattr(
        mcp_server.worker,
        "research_person",
        lambda *a, **k: ResearchOutcome(
            "pid", "manual:ada-acme", "investigado", 1, Decimal("0.031"), None
        ),
    )
    monkeypatch.setattr(
        mcp_server.prospects,
        "historial_prospecto",
        lambda uid: {
            "prospect": None,
            "dossier": {"version": 1, "content": {"resumen": "ok"}},
            "actions": [],
        },
    )
    result = mcp_server.investigar_persona(nombre="Ada", empresa="Acme")
    assert result["estado"] == "investigado"
    assert result["coste_usd"] == "0.0310"
    assert result["dossier"]["content"] == {"resumen": "ok"}


def test_investigar_persona_returns_the_errors(conn, monkeypatch):
    monkeypatch.setattr(
        mcp_server.worker,
        "research_person",
        lambda *a, **k: ResearchOutcome(
            "pid", "manual:ada-acme", "investigado", 1, Decimal("0.03"), None, ("about: 404",)
        ),
    )
    monkeypatch.setattr(
        mcp_server.prospects,
        "historial_prospecto",
        lambda uid: {"prospect": None, "dossier": None, "actions": []},
    )
    assert mcp_server.investigar_persona(nombre="Ada", empresa="Acme")["errores"] == ["about: 404"]
