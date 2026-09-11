from decimal import Decimal

import httpx
import openai
import pytest

from handoff_agent import db, guards
from handoff_agent.dossier import DossierInvalid
from handoff_agent.research import worker as w
from handoff_agent.research.gather import Evidence, Gathered
from handoff_agent.research.synthesize import Synthesis
from handoff_agent.tools import prospects
from tests.test_dossier import make_dossier


def stub_gather(pid, full_name, company, domain=None):
    return Gathered("acme.com", [Evidence("home", "https://acme.com/", "Acme", "t")], [], [])


def synth_returning(*results):
    """Cada llamada consume el siguiente resultado: un dossier o una excepción."""
    queue = list(results)
    calls = []

    def synth(pid, full_name, company, gathered, budget=None):
        calls.append(gathered)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if budget is not None:
            budget.add(Decimal("0.02"))
        return Synthesis(dossier=item, cost_usd=Decimal("0.02"), attempts=1)

    synth.calls = calls
    return synth


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setattr(w, "gather", stub_gather)
    monkeypatch.setattr(w, "run_followup", lambda pid, gathered, queries: gathered)
    return monkeypatch


def test_manual_ids_are_stable_ascii_slugs():
    assert w.manual_user_id("José Pérez", "Acme, Inc.") == "manual:jose-perez-acme-inc"
    assert w.manual_user_id(None, "Acme") == "manual:acme"


def test_needs_a_name_or_a_company():
    with pytest.raises(ValueError):
        w.research_person()


def test_a_new_person_ends_up_investigated(conn, pipeline):
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier()))
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "investigado"
    assert outcome.version == 1
    assert outcome.cost_usd == Decimal("0.02")
    person = db.fetch_one(
        "select state, company_domain, company_name from prospects where id = %s",
        (outcome.prospect_id,),
    )
    assert person["state"] == "investigado"
    assert person["company_domain"] == "acme.com"
    assert person["company_name"] == "Acme"


def test_a_discarded_person_is_never_researched(conn, pipeline):
    person = prospects.upsert_prospect("manual:ada-ruiz-acme", full_name="Ada Ruiz")
    db.execute("update prospects set state = 'descartado' where id = %s", (person["id"],))
    synth = synth_returning()
    pipeline.setattr(w, "synthesize", synth)
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "omitido"
    assert synth.calls == []


def test_a_fresh_dossier_is_not_paid_for_twice(conn, pipeline):
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier(), make_dossier()))
    w.research_person("Ada Ruiz", "Acme")
    again = w.research_person("Ada Ruiz", "Acme")
    assert again.status == "omitido"
    forced = w.research_person("Ada Ruiz", "Acme", force=True)
    assert forced.status == "investigado"
    assert forced.version == 2


def test_an_invalid_dossier_marks_the_person_incomplete(conn, pipeline):
    pipeline.setattr(
        w, "synthesize", synth_returning(DossierInvalid(["fuente inventada"], Decimal("0.04")))
    )
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "incompleto"
    state = db.fetch_one("select state from prospects where id = %s", (outcome.prospect_id,))[
        "state"
    ]
    assert state == "incompleto"
    action = db.fetch_one("select action from agent_actions where action = 'research_incompleto'")
    assert action is not None


def test_a_blown_run_budget_marks_the_person_incomplete(conn, pipeline):
    pipeline.setattr(w, "synthesize", synth_returning(guards.RunBudgetExceeded("run spent $2")))
    assert w.research_person("Ada Ruiz", "Acme").status == "incompleto"


def test_followup_searches_feed_a_second_synthesis(conn, pipeline):
    first = make_dossier(busquedas_sugeridas=["acme funding"])
    second = make_dossier(resumen="Versión enriquecida.")
    synth = synth_returning(first, second)
    pipeline.setattr(w, "synthesize", synth)
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert len(synth.calls) == 2
    stored = db.fetch_one(
        "select content from dossiers where prospect_id = %s", (outcome.prospect_id,)
    )
    assert stored["content"]["resumen"] == "Versión enriquecida."


@pytest.mark.parametrize(
    "failure",
    [
        DossierInvalid(["mal"], Decimal("0.02")),
        guards.RunBudgetExceeded("run spent $2"),
    ],
)
def test_a_failed_second_pass_keeps_the_paid_first_dossier(conn, pipeline, failure):
    first = make_dossier(busquedas_sugeridas=["acme funding"], resumen="Primera pasada.")
    pipeline.setattr(w, "synthesize", synth_returning(first, failure))
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "investigado"
    stored = db.fetch_one(
        "select content from dossiers where prospect_id = %s", (outcome.prospect_id,)
    )
    assert stored["content"]["resumen"] == "Primera pasada."


def test_the_kill_switch_stops_the_system_without_blaming_the_person(conn, pipeline):
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier()))
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    try:
        with pytest.raises(guards.KillSwitchActive):
            w.research_person("Ada Ruiz", "Acme")
    finally:
        db.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")
    state = db.fetch_one(
        "select state from prospects where slack_user_id = 'manual:ada-ruiz-acme'"
    )["state"]
    assert state == "nuevo"


def stored_resumen(pid):
    return db.fetch_one(
        "select content from dossiers where prospect_id = %s order by version desc limit 1",
        (pid,),
    )["content"]["resumen"]


def test_an_openai_timeout_in_the_second_pass_keeps_the_first_dossier(conn, pipeline):
    first = make_dossier(busquedas_sugeridas=["acme funding"], resumen="Primera pasada.")
    timeout = openai.APITimeoutError(
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    )
    pipeline.setattr(w, "synthesize", synth_returning(first, timeout))
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "investigado"
    assert outcome.version == 1
    assert stored_resumen(outcome.prospect_id) == "Primera pasada."
    action = db.fetch_one(
        "select payload from agent_actions where action = 'seguimiento_descartado'"
    )
    assert action["payload"]["motivo"].startswith("APITimeoutError")


def test_a_system_stop_in_the_second_pass_saves_the_first_and_propagates(conn, pipeline):
    first = make_dossier(busquedas_sugeridas=["acme funding"], resumen="Primera pasada.")
    pipeline.setattr(w, "synthesize", synth_returning(first, guards.MonthlyBudgetExceeded("tope")))
    with pytest.raises(guards.MonthlyBudgetExceeded):
        w.research_person("Ada Ruiz", "Acme")
    person = db.fetch_one(
        "select id, state from prospects where slack_user_id = 'manual:ada-ruiz-acme'"
    )
    assert person["state"] == "investigado"
    assert stored_resumen(person["id"]) == "Primera pasada."
    assert db.fetch_one("select 1 from agent_actions where action = 'seguimiento_cortado'")


def test_a_followup_crash_keeps_the_first_dossier(conn, pipeline):
    first = make_dossier(busquedas_sugeridas=["acme funding"], resumen="Primera pasada.")
    pipeline.setattr(w, "synthesize", synth_returning(first))

    def broken_followup(pid, gathered, queries):
        raise RuntimeError("la BD se cayó")

    pipeline.setattr(w, "run_followup", broken_followup)
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "investigado"
    assert stored_resumen(outcome.prospect_id) == "Primera pasada."


def gather_with(errors, attempted, answered):
    def fake(pid, full_name, company, domain=None):
        return Gathered(
            "acme.com",
            [Evidence("home", "https://acme.com/", "Acme", "t")],
            [],
            list(errors),
            searches_attempted=attempted,
            searches_answered=answered,
        )

    return fake


def test_gathering_errors_are_recorded_and_returned(conn, pipeline):
    pipeline.setattr(w, "gather", gather_with(["about: 404"], attempted=2, answered=2))
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier()))
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "investigado"
    assert outcome.errors == ("about: 404",)
    action = db.fetch_one("select payload from agent_actions where action = 'research_errores'")
    assert action["payload"]["errores"] == ["about: 404"]


def test_a_degraded_search_saves_the_dossier_but_leaves_the_person_incomplete(conn, pipeline):
    """D2: se guarda lo que haya, pero sin congelar: la próxima vez se repite."""
    errors = ["linkedin: motores vetados", "prensa: motores vetados"]
    pipeline.setattr(w, "gather", gather_with(errors, attempted=2, answered=0))
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier(), make_dossier()))

    degraded = w.research_person("Ada Ruiz", "Acme")
    assert degraded.status == "incompleto"
    assert degraded.version == 1
    assert degraded.reason.startswith("búsqueda degradada")
    assert degraded.errors == tuple(errors)
    state = db.fetch_one("select state from prospects where id = %s", (degraded.prospect_id,))
    assert state["state"] == "incompleto"

    pipeline.setattr(w, "gather", gather_with([], attempted=2, answered=2))
    healthy = w.research_person("Ada Ruiz", "Acme")
    assert healthy.status == "investigado"
    assert healthy.version == 2
    state = db.fetch_one("select state from prospects where id = %s", (healthy.prospect_id,))
    assert state["state"] == "investigado"
