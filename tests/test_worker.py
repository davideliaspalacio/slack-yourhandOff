import hashlib
import json
from decimal import Decimal

import httpx
import openai
import pytest

from handoff_agent import db, guards, llm
from handoff_agent.dossier import DossierInvalid
from handoff_agent.research import worker as w
from handoff_agent.research.gather import Evidence, Gathered
from handoff_agent.research.synthesize import Synthesis
from handoff_agent.tools import jobs, prospects, search, web
from handoff_agent.tools.jobs import JobPosting
from handoff_agent.tools.search import SearchResult
from tests.test_dossier import make_dossier
from tests.test_gather import fake_page, fake_search
from tests.test_synthesize import ScriptedOpenAI


def stub_gather(pid, full_name, company, domain=None, extra_links=(), notes=None):
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
    def fake(pid, full_name, company, domain=None, extra_links=(), notes=None):
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


def state_of(pid):
    return db.fetch_one("select state from prospects where id = %s", (pid,))["state"]


def test_a_contacted_person_keeps_their_state_when_re_researched(conn, pipeline):
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier(), make_dossier()))
    first = w.research_person("Ada Ruiz", "Acme")
    db.execute("update prospects set state = 'contactado' where id = %s", (first.prospect_id,))
    again = w.research_person("Ada Ruiz", "Acme", force=True)
    assert again.version == 2
    assert state_of(again.prospect_id) == "contactado"


def test_a_failed_forced_re_research_does_not_downgrade_an_investigated_person(conn, pipeline):
    pipeline.setattr(
        w, "synthesize", synth_returning(make_dossier(), DossierInvalid(["mal"], Decimal("0.02")))
    )
    first = w.research_person("Ada Ruiz", "Acme")
    again = w.research_person("Ada Ruiz", "Acme", force=True)
    assert again.status == "incompleto"
    assert state_of(first.prospect_id) == "investigado"


def test_a_person_discarded_mid_run_stays_discarded(conn, pipeline):
    def synth(pid, full_name, company, gathered, budget=None):
        # Alguien descarta a la persona desde el panel mientras el research corre.
        db.execute("update prospects set state = 'descartado' where id = %s", (pid,))
        return Synthesis(dossier=make_dossier(), cost_usd=Decimal("0.02"), attempts=1)

    pipeline.setattr(w, "synthesize", synth)
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.version == 1
    assert state_of(outcome.prospect_id) == "descartado"


def test_stored_sources_record_their_provenance(conn, pipeline):
    def gather_with_a_job(pid, full_name, company, domain=None, extra_links=(), notes=None):
        return Gathered(
            "acme.com",
            [Evidence("home", "https://acme.com/", "Acme", "t")],
            [JobPosting("Support Lead", "Acme", "Remote", "https://jobs.example/1", "linkedin")],
            [],
        )

    pipeline.setattr(w, "gather", gather_with_a_job)
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier()))
    outcome = w.research_person("Ada Ruiz", "Acme")
    stored = db.fetch_one(
        "select sources from dossiers where prospect_id = %s", (outcome.prospect_id,)
    )
    assert stored["sources"] == [
        {"url": "https://acme.com/", "kind": "home", "title": "Acme"},
        {"url": "https://jobs.example/1", "kind": "vacante", "title": "Support Lead"},
    ]


# --- Integración: el cableado real, con dobles solo en el borde -------------

FOLLOWUP_URL = "https://news.example/acme-ronda"
FOLLOWUP_QUERY = "ronda acme 2026"


@pytest.fixture
def real_wiring(monkeypatch):
    """gather, synthesize y run_followup de verdad; solo se falsean las
    herramientas de red y el cliente de OpenAI."""
    monkeypatch.setattr(
        search,
        "buscar_web",
        fake_search(
            {
                "linkedin.com/in": [
                    SearchResult(
                        "Ada Ruiz - CEO - Acme", "https://www.linkedin.com/in/adaruiz", "CEO"
                    )
                ],
                "funding OR raises": [
                    SearchResult("Acme contrata", "https://news.example/acme-hiring", "Soporte")
                ],
                FOLLOWUP_QUERY: [SearchResult("Acme levanta una ronda", FOLLOWUP_URL, "Serie A")],
            }
        ),
    )
    monkeypatch.setattr(web, "leer_sitio", fake_page())
    monkeypatch.setattr(
        jobs,
        "buscar_ofertas",
        lambda company, limit=20, prospect_id=None: [
            JobPosting("Support Lead", "Acme", "Remote", "https://jobs.example/1", "linkedin")
        ],
    )

    def script(*dossiers):
        fake = ScriptedOpenAI(*(json.dumps(d) for d in dossiers))
        monkeypatch.setattr(llm, "_client", lambda: fake)
        return fake

    return script


def stored_dossier(pid):
    return db.fetch_one(
        "select content, sources from dossiers where prospect_id = %s "
        "order by version desc limit 1",
        (pid,),
    )


def test_a_followup_source_reaches_the_stored_dossier(conn, real_wiring):
    fake = real_wiring(
        make_dossier(busquedas_sugeridas=[FOLLOWUP_QUERY]),
        make_dossier(senales_contexto=[{"hecho": "Levantó una Serie A", "fuente": FOLLOWUP_URL}]),
    )
    outcome = w.research_person("Ada Ruiz", "Acme", domain="acme.com")
    assert outcome.status == "investigado"
    assert len(fake.calls) == 2
    stored = stored_dossier(outcome.prospect_id)
    assert stored["content"]["senales_contexto"][0]["fuente"] == FOLLOWUP_URL
    assert {"url": FOLLOWUP_URL, "kind": "seguimiento", "title": "Acme levanta una ronda"} in (
        stored["sources"]
    )


def test_an_ungrounded_second_pass_falls_back_to_the_first_dossier(conn, real_wiring):
    invented = make_dossier(
        senales_contexto=[{"hecho": "Levantó $20M", "fuente": "https://inventada.example/x"}]
    )
    fake = real_wiring(make_dossier(busquedas_sugeridas=[FOLLOWUP_QUERY]), invented, invented)
    outcome = w.research_person("Ada Ruiz", "Acme", domain="acme.com")
    assert outcome.status == "investigado"
    assert outcome.version == 1
    assert len(fake.calls) == 3
    stored = stored_dossier(outcome.prospect_id)
    assert stored["content"]["busquedas_sugeridas"] == [FOLLOWUP_QUERY]
    assert {s["url"] for s in stored["sources"]} == {
        "https://acme.com/",
        "https://acme.com/careers",
        "https://www.linkedin.com/in/adaruiz",
        "https://news.example/acme-hiring",
        "https://jobs.example/1",
    }


def test_a_name_with_no_ascii_letters_still_gets_a_stable_id():
    """NFKD a ASCII deja "李雷" en nada; sin respaldo, todos esos nombres
    compartirían el id "manual:" y se pisarían el dossier."""
    expected = "manual:" + hashlib.sha1("李雷".encode()).hexdigest()[:12]
    assert w.manual_user_id("李雷", None) == expected
    assert w.manual_user_id("王芳", None) != expected


# --- Lo que devuelve el modelo no es de fiar en tipo ---------------------------


def test_a_non_list_suggestion_field_keeps_the_first_dossier(conn, pipeline):
    first = make_dossier(busquedas_sugeridas=3, resumen="Primera pasada.")
    pipeline.setattr(w, "synthesize", synth_returning(first))
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "investigado"
    assert outcome.version == 1
    assert stored_resumen(outcome.prospect_id) == "Primera pasada."
    assert state_of(outcome.prospect_id) == "investigado"


def test_a_string_suggestion_field_runs_no_searches(conn, pipeline):
    followups = []
    pipeline.setattr(
        w, "run_followup", lambda pid, gathered, queries: followups.append(queries) or gathered
    )
    synth = synth_returning(make_dossier(busquedas_sugeridas="abc"))
    pipeline.setattr(w, "synthesize", synth)
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "investigado"
    assert followups == []
    assert len(synth.calls) == 1


def test_a_crash_reading_the_suggestions_keeps_the_first_dossier(conn, pipeline):
    """Leer las búsquedas sugeridas ya es segunda pasada: si falla, vale el primero."""

    def broken(dossier):
        raise TypeError("busquedas_sugeridas ilegible")

    pipeline.setattr(w, "suggested_queries", broken)
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier(resumen="Primera pasada.")))
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "investigado"
    assert stored_resumen(outcome.prospect_id) == "Primera pasada."
    action = db.fetch_one(
        "select payload from agent_actions where action = 'seguimiento_descartado'"
    )
    assert action["payload"]["motivo"].startswith("TypeError")


@pytest.mark.parametrize("nombre", [123, ["Acme Corp"], "   ", None])
def test_a_company_name_that_is_not_a_string_falls_back_to_the_input(conn, pipeline, nombre):
    dossier = make_dossier(empresa={**make_dossier()["empresa"], "nombre": nombre})
    pipeline.setattr(w, "synthesize", synth_returning(dossier))
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "investigado"
    person = db.fetch_one(
        "select state, company_name from prospects where id = %s", (outcome.prospect_id,)
    )
    assert person == {"state": "investigado", "company_name": "Acme"}


# --- B: un dossier degradado se marca y nunca se congela ----------------------

HEALTHY = {"attempted": 2, "answered": 2}
DEGRADED = {"attempted": 2, "answered": 0}


def age_dossiers(pid, days=200):
    db.execute(
        "update dossiers set created_at = now() - make_interval(days => %s) where prospect_id = %s",
        (days, pid),
    )


def dossier_content(pid, version):
    return db.fetch_one(
        "select content from dossiers where prospect_id = %s and version = %s", (pid, version)
    )["content"]


def test_a_healthy_dossier_carries_no_degraded_marker_and_stays_fresh(conn, pipeline):
    pipeline.setattr(w, "gather", gather_with([], **HEALTHY))
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier()))
    first = w.research_person("Ada Ruiz", "Acme")
    assert "degradado" not in dossier_content(first.prospect_id, 1)
    again = w.research_person("Ada Ruiz", "Acme")
    assert again.status == "omitido"
    assert again.reason == "dossier vigente"


def test_a_degraded_new_person_is_incomplete_and_marked(conn, pipeline):
    pipeline.setattr(w, "gather", gather_with(["prensa: vetado"], **DEGRADED))
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier()))
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert outcome.status == "incompleto"
    assert outcome.reason.startswith("búsqueda degradada")
    assert outcome.errors == ("prensa: vetado",)
    assert dossier_content(outcome.prospect_id, 1)["degradado"] is True


def test_the_model_cannot_mark_a_healthy_dossier_as_degraded(conn, pipeline):
    pipeline.setattr(w, "gather", gather_with([], **HEALTHY))
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier(degradado=True)))
    outcome = w.research_person("Ada Ruiz", "Acme")
    assert "degradado" not in dossier_content(outcome.prospect_id, 1)
    assert w.research_person("Ada Ruiz", "Acme").status == "omitido"


@pytest.mark.parametrize("state", ["investigado", "contactado", "cliente"])
def test_a_degraded_re_research_keeps_the_state_but_never_freezes(conn, pipeline, state):
    """D2 + I4: la persona conserva su estado, pero la versión pobre no se
    congela 180 días: la próxima ejecución investiga otra vez sin --forzar."""
    pipeline.setattr(w, "gather", gather_with([], **HEALTHY))
    pipeline.setattr(w, "synthesize", synth_returning(*[make_dossier()] * 3))
    first = w.research_person("Ada Ruiz", "Acme")
    pid = first.prospect_id
    db.execute("update prospects set state = %s where id = %s", (state, pid))
    age_dossiers(pid)

    pipeline.setattr(w, "gather", gather_with(["prensa: vetado"], **DEGRADED))
    degraded = w.research_person("Ada Ruiz", "Acme")
    assert degraded.version == 2
    assert state_of(pid) == state
    assert degraded.status == "investigado"
    assert degraded.reason.startswith("búsqueda degradada")
    assert f"se conserva el estado {state}" in degraded.reason
    assert degraded.errors == ("prensa: vetado",)
    assert dossier_content(pid, 2)["degradado"] is True

    pipeline.setattr(w, "gather", gather_with([], **HEALTHY))
    healthy = w.research_person("Ada Ruiz", "Acme")
    assert healthy.status == "investigado"
    assert healthy.version == 3
    assert "degradado" not in dossier_content(pid, 3)
    assert state_of(pid) == state


# --- F: la parada en la segunda pasada dice qué versión se guardó -------------


def test_a_second_pass_stop_carries_the_saved_version(conn, pipeline):
    first = make_dossier(busquedas_sugeridas=["acme funding"], resumen="Primera pasada.")
    pipeline.setattr(w, "synthesize", synth_returning(first, guards.KillSwitchActive("apagado")))
    with pytest.raises(guards.KillSwitchActive) as info:
        w.research_person("Ada Ruiz", "Acme")
    assert info.value.saved_version == 1


def test_a_first_pass_stop_saves_nothing_and_says_so(conn, pipeline):
    pipeline.setattr(w, "synthesize", synth_returning(guards.MonthlyBudgetExceeded("tope")))
    with pytest.raises(guards.MonthlyBudgetExceeded) as info:
        w.research_person("Ada Ruiz", "Acme")
    assert getattr(info.value, "saved_version", None) is None


# --- datos_proveedor: código-propio, nunca lo pone el modelo ------------------


def gather_with_provider(proveedor):
    def fake(pid, full_name, company, domain=None, extra_links=(), notes=None):
        return Gathered(
            "acme.com",
            [Evidence("home", "https://acme.com/", "Acme", "t")],
            [],
            [],
            proveedor=proveedor,
        )

    return fake


def test_store_writes_the_gathered_provider_data(conn, pipeline):
    proveedor = {"empleados_linkedin": 16679, "fuente": "proveedor externo (sin verificar)"}
    pipeline.setattr(w, "gather", gather_with_provider(proveedor))
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier()))
    outcome = w.research_person("Ada Ruiz", "Acme")
    stored = db.fetch_one(
        "select content from dossiers where prospect_id = %s", (outcome.prospect_id,)
    )
    assert stored["content"]["datos_proveedor"] == proveedor


def test_store_drops_a_model_supplied_datos_proveedor(conn, pipeline):
    pipeline.setattr(w, "gather", gather_with_provider(None))
    pipeline.setattr(
        w, "synthesize", synth_returning(make_dossier(datos_proveedor={"empleados": 999999}))
    )
    outcome = w.research_person("Ada Ruiz", "Acme")
    stored = db.fetch_one(
        "select content from dossiers where prospect_id = %s", (outcome.prospect_id,)
    )
    assert "datos_proveedor" not in stored["content"]


def test_store_omits_datos_proveedor_when_there_is_none(conn, pipeline):
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier()))
    outcome = w.research_person("Ada Ruiz", "Acme")
    stored = db.fetch_one(
        "select content from dossiers where prospect_id = %s", (outcome.prospect_id,)
    )
    assert "datos_proveedor" not in stored["content"]


# --- empresa_no_confirmada: código-propio, nunca lo pone el modelo -----------


def gather_with_unconfirmed_domain(unconfirmed_domain):
    def fake(pid, full_name, company, domain=None, extra_links=(), notes=None):
        return Gathered(
            None,
            [Evidence("home", "https://acme.com/", "Acme", "t")],
            [],
            [],
            unconfirmed_domain=unconfirmed_domain,
        )

    return fake


def test_store_writes_the_unconfirmed_domain_marker(conn, pipeline):
    pipeline.setattr(w, "gather", gather_with_unconfirmed_domain("handoff.ai"))
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier()))
    outcome = w.research_person("David", "Handoff")
    stored = db.fetch_one(
        "select content from dossiers where prospect_id = %s", (outcome.prospect_id,)
    )
    assert stored["content"]["empresa_no_confirmada"] == {"dominio_adivinado": "handoff.ai"}


def test_store_drops_a_model_supplied_empresa_no_confirmada(conn, pipeline):
    pipeline.setattr(w, "gather", gather_with_unconfirmed_domain(None))
    pipeline.setattr(
        w,
        "synthesize",
        synth_returning(make_dossier(empresa_no_confirmada={"dominio_adivinado": "evil.example"})),
    )
    outcome = w.research_person("Ada Ruiz", "Acme")
    stored = db.fetch_one(
        "select content from dossiers where prospect_id = %s", (outcome.prospect_id,)
    )
    assert "empresa_no_confirmada" not in stored["content"]


def test_store_omits_empresa_no_confirmada_when_there_is_none(conn, pipeline):
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier()))
    outcome = w.research_person("Ada Ruiz", "Acme")
    stored = db.fetch_one(
        "select content from dossiers where prospect_id = %s", (outcome.prospect_id,)
    )
    assert "empresa_no_confirmada" not in stored["content"]


def test_a_discarded_guess_does_not_overwrite_the_stored_company_domain(conn, pipeline):
    """`gathered.domain` es None cuando se descarta el dominio adivinado:
    _set_state usa coalesce, así que no debe borrar un company_domain previo."""
    pipeline.setattr(w, "synthesize", synth_returning(make_dossier(), make_dossier()))
    first = w.research_person("Ada Ruiz", "Acme")
    assert (
        db.fetch_one("select company_domain from prospects where id = %s", (first.prospect_id,))[
            "company_domain"
        ]
        == "acme.com"
    )

    pipeline.setattr(w, "gather", gather_with_unconfirmed_domain("evil.example"))
    again = w.research_person("Ada Ruiz", "Acme", force=True)
    assert again.status == "investigado"
    person = db.fetch_one(
        "select company_domain from prospects where id = %s", (first.prospect_id,)
    )
    assert person["company_domain"] == "acme.com"
