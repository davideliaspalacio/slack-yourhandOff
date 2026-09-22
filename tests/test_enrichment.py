"""normalise() nunca toca la red; enrich_company() nunca llama al webhook real:
respx intercepta cada POST, igual que en tests/test_search.py."""

import json
from pathlib import Path

import httpx
import respx

from handoff_agent import db
from handoff_agent.tools import enrichment

WEBHOOK = "https://n8n.example/webhook/enrich-empresa"
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "enrichment_stripe.json").read_text())


# --- normalise(): pura, sin red -----------------------------------------------


def test_normalise_extracts_the_expected_fields_from_the_stripe_fixture():
    result = enrichment.normalise(FIXTURE)
    assert result["empleados_linkedin"] == 16679
    assert result["empleados_crm"] == 8000
    assert result["rango_empleados"] == {"min": 5001, "max": 10000}
    assert result["ingresos_anuales_usd"] == 6935000000
    assert result["anio_fundacion"] == 2010
    assert result["empleados_por_area"]["support"] == 334
    assert len(result["evolucion_mensual"]) == 25
    assert result["evolucion_mensual"][-1] == {"mes": "2026-09", "empleados": 16679}
    assert len(result["crecimiento"]) == 3
    assert result["crecimiento"][1] == {"meses": 12, "cambio_neto": 24, "porcentaje": 0.1439}
    assert result["sede"] == {
        "ciudad": "South San Francisco",
        "region": "California",
        "pais": "United States",
    }
    assert result["ubicacion_linkedin"] == {"ciudad": "Tokyo", "region": "Shibuya-ku", "pais": "JP"}
    assert result["linkedin_url"] == "https://www.linkedin.com/company/stripe/"
    assert result["seguidores_linkedin"] == 1719406
    assert result["antiguedad_media"] == "2 years"
    assert result["fuente"] == "proveedor externo (sin verificar)"
    assert "consultado" in result


def test_normalise_drops_what_we_already_research_or_should_never_keep():
    result = enrichment.normalise(FIXTURE)
    dumped = json.dumps(result)
    for forbidden in ("googleNews", "googleJobs", "prompt", "aiSummary", "Phone", "Street"):
        assert forbidden not in dumped


def test_normalise_drops_bools_disguised_as_ints():
    raw = {"linkedIn": {"employeeCount": True, "followerCount": False}}
    assert enrichment.normalise(raw) is None


def test_normalise_drops_entries_with_invalid_dates():
    raw = {
        "linkedIn": {
            "monthlyEmployeeCounts": [
                {"count": 100, "date": "2024-13-40"},
                {"count": 200, "date": "not-a-date"},
                {"count": 300, "date": "2024-01-01"},
            ]
        }
    }
    result = enrichment.normalise(raw)
    assert result["evolucion_mensual"] == [{"mes": "2024-01", "empleados": 300}]


def test_normalise_keeps_only_the_last_25_months():
    months = [{"count": i, "date": f"2020-{(i % 12) + 1:02d}-01"} for i in range(1, 40)]
    result = enrichment.normalise({"linkedIn": {"monthlyEmployeeCounts": months}})
    assert len(result["evolucion_mensual"]) == 25
    assert result["evolucion_mensual"][-1]["empleados"] == 39


def test_normalise_drops_bad_area_keys():
    raw = {
        "Company": {
            "departmentalHeadCount": {
                "Sales": 10,
                "sales-team": 5,
                "ok_area": 7,
                "123numeric": 3,
            }
        }
    }
    result = enrichment.normalise(raw)
    assert result["empleados_por_area"] == {"ok_area": 7}


def test_normalise_drops_non_linkedin_company_urls():
    raw = {"linkedIn": {"linkedinUrl": "https://example.com/stripe", "employeeCount": 10}}
    result = enrichment.normalise(raw)
    assert "linkedin_url" not in result
    assert result["empleados_linkedin"] == 10


def test_normalise_caps_long_strings():
    raw = {"linkedIn": {"avgTenure": "x" * 500, "employeeCount": 5}}
    result = enrichment.normalise(raw)
    assert len(result["antiguedad_media"]) == 200


def test_normalise_rejects_a_founding_year_out_of_range():
    raw = {"crm": {"companyYearFounded": 1500}}
    assert enrichment.normalise(raw) is None
    raw = {"crm": {"companyYearFounded": 3000}}
    assert enrichment.normalise(raw) is None


def test_normalise_returns_none_for_empty_or_garbage_input():
    assert enrichment.normalise({}) is None
    assert enrichment.normalise({"foo": "bar"}) is None
    assert enrichment.normalise("garbage") is None
    assert enrichment.normalise(None) is None
    assert enrichment.normalise([1, 2, 3]) is None


# --- enrich_company(): red simulada con respx --------------------------------


def test_enrich_company_returns_none_and_makes_no_call_when_unset(conn, monkeypatch):
    monkeypatch.delenv("ENRICHMENT_WEBHOOK_URL", raising=False)
    assert enrichment.enrich_company("acme.com") is None
    assert db.fetch_one("select count(*) as n from cost_events")["n"] == 0
    assert db.fetch_one("select count(*) as n from agent_actions")["n"] == 0


@respx.mock
def test_enrich_company_posts_the_expected_payload(conn, monkeypatch):
    monkeypatch.setenv("ENRICHMENT_WEBHOOK_URL", WEBHOOK)
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json=FIXTURE))
    enrichment.enrich_company("stripe.com")
    assert route.called
    sent = json.loads(route.calls[0].request.content)
    assert sent == {"companyUrl": "https://stripe.com"}


@respx.mock
def test_enrich_company_success_returns_data_and_writes_one_cost_row(conn, monkeypatch):
    monkeypatch.setenv("ENRICHMENT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json=FIXTURE))
    result = enrichment.enrich_company("stripe.com")
    assert result["empleados_linkedin"] == 16679
    rows = db.fetch_all("select source, cost_usd from cost_events")
    assert len(rows) == 1
    assert rows[0]["source"] == "enriquecimiento_empresa"
    assert db.fetch_one("select count(*) as n from agent_actions")["n"] == 0


@respx.mock
def test_enrich_company_500_records_the_failure_with_status_and_no_cost_row(conn, monkeypatch):
    monkeypatch.setenv("ENRICHMENT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(return_value=httpx.Response(500, text="Error in workflow"))
    result = enrichment.enrich_company("acme.com", prospect_id=None)
    assert result is None
    action = db.fetch_one(
        "select payload from agent_actions where action = 'enriquecimiento_fallido'"
    )
    assert action["payload"]["motivo"] == "HTTPStatusError"
    assert action["payload"]["status"] == 500
    assert db.fetch_one("select count(*) as n from cost_events")["n"] == 0


@respx.mock
def test_enrich_company_timeout_returns_none(conn, monkeypatch):
    monkeypatch.setenv("ENRICHMENT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(side_effect=httpx.TimeoutException("timed out"))
    result = enrichment.enrich_company("acme.com")
    assert result is None
    action = db.fetch_one(
        "select payload from agent_actions where action = 'enriquecimiento_fallido'"
    )
    assert action["payload"]["motivo"] == "TimeoutException"
    assert db.fetch_one("select count(*) as n from cost_events")["n"] == 0


@respx.mock
def test_enrich_company_non_json_response_returns_none(conn, monkeypatch):
    monkeypatch.setenv("ENRICHMENT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, text="not json"))
    result = enrichment.enrich_company("acme.com")
    assert result is None
    action = db.fetch_one(
        "select payload from agent_actions where action = 'enriquecimiento_fallido'"
    )
    assert "motivo" in action["payload"]
    assert db.fetch_one("select count(*) as n from cost_events")["n"] == 0


@respx.mock
def test_enrich_company_failure_log_never_contains_the_url_or_body(conn, monkeypatch, caplog):
    monkeypatch.setenv("ENRICHMENT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(return_value=httpx.Response(500, text="Error in workflow"))
    with caplog.at_level("WARNING"):
        enrichment.enrich_company("acme.com")
    log_text = caplog.text
    assert WEBHOOK not in log_text
    assert "Error in workflow" not in log_text


@respx.mock
def test_enrich_company_attributes_the_action_to_the_prospect(conn, monkeypatch):
    monkeypatch.setenv("ENRICHMENT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(return_value=httpx.Response(500, text="down"))
    from handoff_agent.tools import prospects

    person = prospects.upsert_prospect("U_ENRICH")
    enrichment.enrich_company("acme.com", prospect_id=str(person["id"]))
    action = db.fetch_one(
        "select prospect_id from agent_actions where action = 'enriquecimiento_fallido'"
    )
    assert str(action["prospect_id"]) == str(person["id"])
