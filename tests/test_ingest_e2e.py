import json

from handoff_agent import db, llm
from handoff_agent.ingest import queue
from handoff_agent.ingest.research_runner import run_next_job
from handoff_agent.ingest.resolver import resolve_pending
from handoff_agent.ingest.watcher import watch_tick
from handoff_agent.tools import jobs, search, web
from handoff_agent.tools.search import SearchResult
from handoff_agent.tools.web import PageContent, PageUnavailable
from tests.slack_fakes import FakeReader
from tests.test_dossier import make_dossier
from tests.test_synthesize import ScriptedOpenAI

NOW = 1_726_000_000.0
LINKEDIN = "https://www.linkedin.com/in/adaruiz"
HOME = "https://acme.com/"


def fake_tools(monkeypatch):
    def buscar(query, limit=8, prospect_id=None):
        if "linkedin.com/in" in query:
            return [SearchResult("Ada Ruiz - CEO - Acme", LINKEDIN, "CEO at Acme")]
        return []

    def leer(url, max_chars=20_000, prospect_id=None):
        if url == HOME:
            return PageContent(
                url, HOME, "Acme", "Acme builds logistics software. We are hiring support."
            )
        raise PageUnavailable(f"404 at {url}")

    monkeypatch.setattr(search, "buscar_web", buscar)
    monkeypatch.setattr(web, "leer_sitio", leer)
    monkeypatch.setattr(jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])


def grounded_dossier() -> str:
    return json.dumps(
        make_dossier(
            persona={"nombre": "Ada Ruiz", "cargo": "CEO", "fuente": LINKEDIN},
            empresa={**make_dossier()["empresa"], "fuentes": [HOME]},
            contratacion={
                "vacantes_abiertas": None,
                "roles": [],
                "roles_deslocalizables": [],
                "fuentes": [],
            },
            senales_contexto=[{"hecho": "Contrata soporte", "fuente": HOME}],
            busquedas_sugeridas=[],
        )
    )


def test_a_stranger_who_writes_ends_up_with_a_dossier(conn, monkeypatch):
    reader = FakeReader()
    reader.set_members("C1", "U1", "UBOT")
    reader.add_profile("U1", "Ada Ruiz", title="CEO @ Acme", email="ada@acme.com")
    reader.post("C1", "U1", "Our support queue is out of control", f"{NOW - 60:.6f}")
    reader.post("C1", "UBOT", "beep", f"{NOW - 50:.6f}", bot_id="B1")
    fake_tools(monkeypatch)
    monkeypatch.setattr(llm, "_client", lambda: ScriptedOpenAI(grounded_dossier()))

    tick = watch_tick(reader, ["C1"], lookback_hours=1, now=lambda: NOW)
    assert (tick.messages_new, tick.messages_ignored, tick.members_new) == (1, 1, 0)
    assert resolve_pending().enqueued == 1
    assert run_next_job(reader).status == "hecho"

    person = db.fetch_one(
        "select id, state, company_domain from prospects where slack_user_id = 'U1'"
    )
    assert person["state"] == "investigado"
    assert person["company_domain"] == "acme.com"
    assert (
        db.fetch_one("select version from dossiers where prospect_id = %s", (person["id"],))[
            "version"
        ]
        == 1
    )
    assert queue.status_counts() == {"hecho": 1}
    assert (
        db.fetch_one("select count(*) as n from research_jobs where slack_user_id = 'UBOT'")["n"]
        == 0
    )


def test_a_new_member_after_the_baseline_is_queued_once(conn):
    reader = FakeReader()
    reader.set_members("C1", "U1")
    watch_tick(reader, ["C1"], lookback_hours=1, now=lambda: NOW)
    reader.set_members("C1", "U1", "U2")
    watch_tick(reader, ["C1"], lookback_hours=1, now=lambda: NOW)
    watch_tick(reader, ["C1"], lookback_hours=1, now=lambda: NOW)
    assert db.fetch_all("select slack_user_id, reason from research_jobs") == [
        {"slack_user_id": "U2", "reason": "miembro_nuevo"}
    ]
