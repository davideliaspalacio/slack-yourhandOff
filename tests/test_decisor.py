"""Decisor del radar (spec §6). OpenAI y Unipile, siempre sustituidos."""

import json
import threading
from types import SimpleNamespace

import pytest

from handoff_agent import db, guards, llm
from handoff_agent.accounts import decisor, repo
from handoff_agent.tools import unipile
from handoff_agent.tools.unipile import PersonaLinkedIn, Posicion


def persona(lid, name, headline, company_id="16300", empresa="CODELCO", cargo=None):
    posiciones = (
        ()
        if company_id is None and empresa is None
        else (Posicion(empresa=empresa, company_id=company_id, cargo=cargo or headline),)
    )
    return PersonaLinkedIn(
        linkedin_id=lid,
        full_name=name,
        first_name=None,
        last_name=None,
        public_identifier=None,
        profile_url=f"https://www.linkedin.com/in/{lid}",
        headline=headline,
        location="Santiago, Chile",
        posiciones=posiciones,
    )


# --- ordenar_candidatos (puro) ---------------------------------------------------


def test_a_title_match_ranks_above_a_mere_senior_title():
    gerente = persona("a", "Ana", "Gerente de Operaciones Mina")
    director = persona("b", "Beto", "Director Comercial")
    orden = decisor.ordenar_candidatos(
        [director, gerente], ["Gerente de Operaciones"], company_id="16300", empresa="Codelco"
    )
    assert [(c.persona.linkedin_id, c.rank) for c in orden] == [("a", 1), ("b", 2)]
    assert orden[0].reason == "Title matches 'Gerente de Operaciones'; current role at CODELCO"


def test_seniority_breaks_a_tie_in_title_match():
    analista = persona("a", "Ana", "Operations Analyst")
    manager = persona("b", "Beto", "Operations Manager")
    orden = decisor.ordenar_candidatos([analista, manager], ["Operations"], company_id="16300")
    assert orden[0].persona.linkedin_id == "b"


def test_someone_working_elsewhere_sinks():
    fuera = persona("a", "Ana", "Operations Manager", company_id="999", empresa="BHP")
    dentro = persona("b", "Beto", "Operations Supervisor")
    orden = decisor.ordenar_candidatos(
        [fuera, dentro], ["Operations Manager"], company_id="16300", empresa="Codelco"
    )
    assert orden[0].persona.linkedin_id == "b"
    assert "current role at BHP, not Codelco" in orden[1].reason


def test_accents_and_case_do_not_hide_a_match():
    jefe = persona("a", "Ana", "JEFE DE MANTENCIÓN PLANTA")
    orden = decisor.ordenar_candidatos([jefe], ["Jefe de Mantención"], company_id="16300")
    assert orden[0].reason.startswith("Title matches 'Jefe de Mantención'")


def test_the_company_name_counts_when_linkedin_gives_no_id():
    sin_id = persona("a", "Ana", "Plant Manager", company_id=None, empresa="Codelco")
    orden = decisor.ordenar_candidatos(
        [sin_id], ["Plant Manager"], company_id="16300", empresa="Codelco"
    )
    assert "current role at Codelco" in orden[0].reason


def test_unknown_employer_and_no_match_are_said_plainly():
    nadie = persona("a", "Ana", "Consultant", company_id=None, empresa=None)
    orden = decisor.ordenar_candidatos([nadie], ["VP Operations"])
    assert orden[0].reason == "No title match; current company unknown"


def test_unipile_order_breaks_full_ties():
    a, b = persona("a", "Ana", "Plant Manager"), persona("b", "Beto", "Plant Manager")
    assert [
        c.persona.linkedin_id for c in decisor.ordenar_candidatos([a, b], ["Plant Manager"])
    ] == ["a", "b"]


def test_ordenar_an_empty_list():
    assert decisor.ordenar_candidatos([], ["CEO"]) == []


# --- cargos_decisores (LLM falso) --------------------------------------------------


class FakeOpenAI:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(
            prompt_tokens=300,
            completion_tokens=40,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.texts.pop(0)))],
            usage=usage,
        )


def cargos_json(*cargos):
    return json.dumps({"cargos": list(cargos)})


@pytest.fixture
def openai(monkeypatch):
    fake = FakeOpenAI()
    monkeypatch.setattr(llm, "_client", lambda: fake)
    return fake


def test_cargos_come_back_clean_and_the_cost_is_in_the_ledger(conn, openai):
    openai.texts.append(
        cargos_json(
            " Gerente de  Operaciones ",
            "Superintendente de Mina",
            "gerente de operaciones",
            "VP Operations",
            "HR Manager",
            "Talent Lead",
        )
    )
    cargos = decisor.cargos_decisores("Mining Engineer", "Codelco", "Calama, Chile")
    assert cargos == [
        "Gerente de Operaciones",
        "Superintendente de Mina",
        "VP Operations",
        "HR Manager",
    ]
    llamada = openai.calls[0]
    assert llamada["response_format"]["type"] == "json_schema"
    assert "Mining Engineer" in llamada["messages"][-1]["content"]
    assert "Calama, Chile" in llamada["messages"][-1]["content"]
    assert db.fetch_one("select stage from llm_calls")["stage"] == "decisor_cargos"


def test_no_usable_cargos_raise(conn, openai):
    openai.texts.append(cargos_json("", "  "))
    with pytest.raises(decisor.CargosInvalidos):
        decisor.cargos_decisores("Mining Engineer", "Codelco", None)


def test_cargos_respect_the_kill_switch(restore_config, openai):
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    with pytest.raises(guards.KillSwitchActive):
        decisor.cargos_decisores("Mining Engineer", "Codelco", None)
    assert openai.calls == []


# --- procesar_senal ------------------------------------------------------------------


class Busquedas:
    """Unipile falso: qué personas devuelve cada cargo, y qué se le pidió."""

    def __init__(self, por_cargo=None, fallos=None):
        self.por_cargo = por_cargo or {}
        self.fallos = fallos or {}
        self.calls = []

    def __call__(self, company_id, keywords, limit=5):
        self.calls.append((company_id, keywords, limit))
        if keywords in self.fallos:
            raise self.fallos[keywords]
        return self.por_cargo.get(keywords, [])


@pytest.fixture
def busquedas(monkeypatch, openai):
    fake = Busquedas()
    monkeypatch.setattr(unipile, "buscar_personas", fake)
    monkeypatch.setattr(decisor, "_motivo_sin_unipile", lambda: None)
    return fake


def senal(
    linkedin_id="16300",
    status="pursued",
    title="Mining Engineer",
    score=7,
    nombre="Codelco",
    dominio="codelco.cl",
):
    cuenta = repo.agregar_cuenta(nombre, dominio, linkedin_id)
    return db.fetch_one(
        "insert into hiring_signals (account_id, title, title_key, location, status, score) "
        "values (%s, %s, lower(%s), 'Calama', %s, %s) returning id",
        (cuenta["id"], title, title, status, score),
    )["id"]


def status(signal_id):
    return db.fetch_one("select status from hiring_signals where id = %s", (signal_id,))["status"]


def test_a_pursued_signal_ends_with_its_decisor_queued_for_research(conn, busquedas, openai):
    openai.texts.append(cargos_json("Gerente de Operaciones", "Superintendente de Mina"))
    rosa = persona("ACo1", "Rosa Díaz", "Gerente de Operaciones Mina")
    luis = persona("ACo2", "Luis Soto", "Superintendente de Mina")
    otro = persona("ACo3", "Otro", "Geólogo")
    busquedas.por_cargo = {
        "Gerente de Operaciones": [otro, rosa],
        "Superintendente de Mina": [luis, rosa],
    }
    sid = senal()

    resultado = decisor.procesar_senal(sid)

    assert resultado.estado == "researching"
    assert busquedas.calls == [
        ("16300", "Gerente de Operaciones", 5),
        ("16300", "Superintendente de Mina", 5),
    ]
    filas = db.fetch_all(
        "select linkedin_id, rank, chosen, reason, prospect_id from decision_candidates "
        "where signal_id = %s order by rank",
        (sid,),
    )
    assert [f["linkedin_id"] for f in filas] == ["ACo1", "ACo2", "ACo3"]
    assert [f["chosen"] for f in filas] == [True, False, False]
    assert filas[0]["reason"].startswith("Title matches 'Gerente de Operaciones'")
    prospect = db.fetch_one("select * from prospects where slack_user_id = 'li:ACo1'")
    assert (prospect["full_name"], prospect["company_name"], prospect["company_domain"]) == (
        "Rosa Díaz",
        "Codelco",
        "codelco.cl",
    )
    assert filas[0]["prospect_id"] == prospect["id"]
    assert filas[1]["prospect_id"] is None
    job = db.fetch_one("select slack_user_id, reason, status from research_jobs")
    assert (job["slack_user_id"], job["reason"], job["status"]) == ("li:ACo1", "radar", "pendiente")
    assert status(sid) == "researching"
    assert [c["linkedin_id"] for c in resultado.candidatos][:1] == ["ACo1"]


def test_without_a_linkedin_company_id_the_signal_waits(conn, busquedas, openai):
    sid = senal(linkedin_id=None)
    resultado = decisor.procesar_senal(sid)
    assert resultado.estado == "sin_linkedin_id"
    assert status(sid) == "pursued"
    assert openai.calls == [] and busquedas.calls == []
    accion = db.fetch_one("select result from agent_actions where action = 'decisor_senal'")
    assert accion["result"]["estado"] == "sin_linkedin_id"


@pytest.mark.parametrize(
    "error",
    [
        unipile.UnipileFueraDeHorario("fuera de horario"),
        unipile.UnipileTopeDiario("25 búsquedas hoy"),
        unipile.UnipilePausado("pausado"),
        unipile.UnipileNoConfigurado("sin claves"),
    ],
)
def test_a_unipile_guardrail_leaves_the_signal_for_the_next_cycle(conn, busquedas, openai, error):
    openai.texts.append(cargos_json("Gerente de Operaciones", "VP Operations"))
    busquedas.fallos = {"Gerente de Operaciones": error}
    sid = senal()
    resultado = decisor.procesar_senal(sid)
    assert resultado.estado == "pospuesta"
    assert status(sid) == "pursued"
    assert db.fetch_one("select count(*) as n from decision_candidates")["n"] == 0


def test_a_guardrail_midway_keeps_what_was_already_found(conn, busquedas, openai):
    openai.texts.append(cargos_json("Gerente de Operaciones", "VP Operations"))
    busquedas.por_cargo = {
        "Gerente de Operaciones": [persona("ACo1", "Rosa", "Gerente de Operaciones")]
    }
    busquedas.fallos = {"VP Operations": unipile.UnipileTopeDiario("tope")}
    sid = senal()
    assert decisor.procesar_senal(sid).estado == "researching"
    assert status(sid) == "researching"


def test_unipile_unavailable_is_checked_before_paying_the_llm(conn, monkeypatch, openai):
    monkeypatch.setattr(decisor, "_motivo_sin_unipile", lambda: "outside LinkedIn hours")
    sid = senal()
    resultado = decisor.procesar_senal(sid)
    assert (resultado.estado, resultado.detalle) == ("pospuesta", "outside LinkedIn hours")
    assert openai.calls == []


def test_nobody_found_is_recorded_and_the_signal_stays(conn, busquedas, openai):
    openai.texts.append(cargos_json("Gerente de Operaciones", "VP Operations"))
    sid = senal()
    assert decisor.procesar_senal(sid).estado == "sin_candidatos"
    assert status(sid) == "pursued"


def test_any_other_failure_is_recorded_without_raising(conn, busquedas, openai):
    openai.texts.append("esto no es json")
    sid = senal()
    resultado = decisor.procesar_senal(sid)
    assert resultado.estado == "error"
    assert "CargosInvalidos" in resultado.detalle
    assert status(sid) == "pursued"
    accion = db.fetch_one("select result from agent_actions where action = 'decisor_senal'")
    assert accion["result"]["estado"] == "error"


def test_the_monthly_cap_is_not_swallowed(conn, busquedas, monkeypatch):
    def tope(*a, **k):
        raise guards.MonthlyBudgetExceeded("spent $150 of $150")

    monkeypatch.setattr(decisor.llm, "complete", tope)
    with pytest.raises(guards.MonthlyBudgetExceeded):
        decisor.procesar_senal(senal())


def test_a_signal_already_researching_is_left_alone(conn, busquedas, openai):
    sid = senal(status="researching")
    assert decisor.procesar_senal(sid).estado == "omitida"
    assert openai.calls == []


def test_an_unknown_signal_raises_lookup_error(conn, busquedas):
    with pytest.raises(LookupError):
        decisor.procesar_senal("00000000-0000-0000-0000-000000000000")


def test_two_processes_never_work_the_same_signal(conn, busquedas, openai):
    sid = senal()
    tomada, soltar = threading.Event(), threading.Event()

    def retener():
        with db.transaction() as cur:
            cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"decisor:{sid}",))
            tomada.set()
            soltar.wait(5)

    hilo = threading.Thread(target=retener)
    hilo.start()
    tomada.wait(5)
    try:
        assert decisor.procesar_senal(sid).estado == "ocupada"
    finally:
        soltar.set()
        hilo.join()
    assert openai.calls == []


def test_reprocessing_keeps_a_single_chosen_candidate(conn, busquedas, openai):
    openai.texts += [cargos_json("Plant Manager", "VP Operations")] * 2
    busquedas.por_cargo = {
        "Plant Manager": [persona("ACo1", "Rosa", "Plant Manager"), persona("ACo2", "Luis", "COO")]
    }
    sid = senal(status="new")
    decisor.procesar_senal(sid)
    db.execute("update hiring_signals set status = 'pursued' where id = %s", (sid,))
    busquedas.por_cargo = {"Plant Manager": [persona("ACo2", "Luis", "Plant Manager")]}
    decisor.procesar_senal(sid)
    elegidos = db.fetch_all(
        "select linkedin_id from decision_candidates where signal_id = %s and chosen", (sid,)
    )
    assert [e["linkedin_id"] for e in elegidos] == ["ACo2"]


# --- procesar_pendientes -------------------------------------------------------------


def test_pendientes_processes_pursued_signals_by_score(conn, busquedas, openai, monkeypatch):
    vistas = []
    monkeypatch.setattr(
        decisor,
        "procesar_senal",
        lambda sid: vistas.append(sid) or decisor.ResultadoDecisor(sid, "researching"),
    )
    baja = senal(score=7, nombre="Acme", dominio="acme.com", linkedin_id="1")
    alta = senal(score=9, nombre="Beta", dominio="beta.com", linkedin_id="2")
    senal(score=10, nombre="Sin Id", dominio="sinid.com", linkedin_id=None)
    senal(score=10, nombre="Nueva", dominio="nueva.com", linkedin_id="3", status="new")
    decisor.procesar_pendientes(limite=3)
    assert vistas == [str(alta), str(baja)]


def test_pendientes_respects_the_limit(conn, busquedas, monkeypatch):
    vistas = []
    monkeypatch.setattr(
        decisor,
        "procesar_senal",
        lambda sid: vistas.append(sid) or decisor.ResultadoDecisor(sid, "researching"),
    )
    senal(nombre="Acme", dominio="acme.com", linkedin_id="1")
    senal(nombre="Beta", dominio="beta.com", linkedin_id="2")
    decisor.procesar_pendientes(limite=1)
    assert len(vistas) == 1


def test_pendientes_waits_for_unipile_without_paying(conn, openai, monkeypatch):
    monkeypatch.setattr(decisor, "_motivo_sin_unipile", lambda: "outside LinkedIn hours")
    sid = senal()
    assert decisor.procesar_pendientes() == []
    assert openai.calls == []
    assert status(sid) == "pursued"


def test_a_recent_failure_is_not_retried_every_cycle(conn, busquedas, openai):
    openai.texts.append("no json")
    sid = senal()
    assert decisor.procesar_pendientes()[0].estado == "error"
    assert decisor.procesar_pendientes() == []
    db.execute("update agent_actions set created_at = now() - interval '2 hours'")
    openai.texts.append("no json")
    assert decisor.procesar_pendientes()[0].signal_id == str(sid)


def _investigada(conn, busquedas, openai):
    openai.texts.append(cargos_json("Plant Manager", "VP Operations"))
    busquedas.por_cargo = {
        "Plant Manager": [persona("ACo1", "Rosa", "Plant Manager"), persona("ACo2", "Luis", "COO")]
    }
    sid = senal()
    decisor.procesar_senal(sid)
    return sid


def _dossier(slack_user_id):
    pid = db.fetch_one("select id from prospects where slack_user_id = %s", (slack_user_id,))["id"]
    db.execute(
        "insert into dossiers (prospect_id, version, content) values (%s, 1, '{}'::jsonb)", (pid,)
    )


def test_a_signal_is_ready_once_its_decisor_has_a_dossier(conn, busquedas, openai):
    sid = _investigada(conn, busquedas, openai)
    _dossier("li:ACo1")
    decisor.procesar_pendientes()
    assert status(sid) == "researching"  # el research sigue abierto
    db.execute("update research_jobs set status = 'hecho'")
    decisor.procesar_pendientes()
    assert status(sid) == "ready"


def test_a_new_choice_from_the_panel_is_researched_next_cycle(conn, busquedas, openai):
    sid = _investigada(conn, busquedas, openai)
    _dossier("li:ACo1")
    db.execute("update research_jobs set status = 'hecho'")
    decisor.procesar_pendientes()
    assert status(sid) == "ready"

    # Lo que hace panel_elegir_candidato (0012), en sus dos sentencias.
    db.execute("update decision_candidates set chosen = false where signal_id = %s", (sid,))
    db.execute(
        "update decision_candidates set chosen = true where signal_id = %s and linkedin_id = 'ACo2'",
        (sid,),
    )
    decisor.procesar_pendientes()

    assert status(sid) == "researching"
    job = db.fetch_one("select reason from research_jobs where slack_user_id = 'li:ACo2'")
    assert job["reason"] == "radar"
    elegido = db.fetch_one("select prospect_id from decision_candidates where linkedin_id = 'ACo2'")
    assert elegido["prospect_id"] is not None


def test_pendientes_stops_on_the_kill_switch(restore_config, busquedas):
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    with pytest.raises(guards.KillSwitchActive):
        decisor.procesar_pendientes()


def test_motivo_sin_unipile_reads_the_guardrails(restore_config, monkeypatch):
    assert decisor._motivo_sin_unipile() == "Unipile is not configured"
    monkeypatch.setenv("UNIPILE_API_KEY", "k")
    monkeypatch.setenv("UNIPILE_DSN", "api.example:1")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "acc")
    uso = {
        "pausado": False,
        "en_horario": True,
        "busquedas_hoy": 0,
        "tope_busquedas": 25,
        "franja": "lun-vie 8:00-19:00",
        "zona": "America/New_York",
    }
    monkeypatch.setattr(unipile, "resumen_uso", lambda: uso)
    assert decisor._motivo_sin_unipile() is None
    uso["busquedas_hoy"] = 25
    assert "cap" in decisor._motivo_sin_unipile()
    uso["en_horario"] = False
    assert "outside" in decisor._motivo_sin_unipile()
    uso["pausado"] = True
    assert "paused" in decisor._motivo_sin_unipile()
