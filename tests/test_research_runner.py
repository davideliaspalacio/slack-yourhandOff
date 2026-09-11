from decimal import Decimal

import pytest

from handoff_agent import db, guards
from handoff_agent.ingest import queue
from handoff_agent.ingest import research_runner as runner
from handoff_agent.research.worker import ResearchOutcome
from handoff_agent.slack_client import SlackAuthFailed
from tests.slack_fakes import FakeReader


@pytest.fixture
def reader():
    fake = FakeReader()
    fake.add_profile("U1", "Ada Ruiz", title="CEO @ Acme", email="ada@acme.com")
    return fake


def stub_research(monkeypatch, error=None):
    seen = []

    def research(full_name=None, company=None, domain=None, slack_user_id=None, force=False):
        seen.append(
            {
                "full_name": full_name,
                "company": company,
                "domain": domain,
                "slack_user_id": slack_user_id,
            }
        )
        if error:
            raise error
        return ResearchOutcome("pid", slack_user_id, "investigado", 1, Decimal("0.02"), None)

    monkeypatch.setattr(runner.worker, "research_person", research)
    return seen


def job() -> dict:
    return db.fetch_one("select status, attempts, outcome, not_before from research_jobs")


def test_an_empty_queue_returns_none(conn, reader):
    assert runner.run_next_job(reader) is None


def test_a_job_is_researched_with_what_the_profile_tells_us(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    seen = stub_research(monkeypatch)
    assert runner.run_next_job(reader).status == "hecho"
    assert seen == [
        {
            "full_name": "Ada Ruiz",
            "company": "Acme",
            "domain": "acme.com",
            "slack_user_id": "U1",
        }
    ]
    assert job()["status"] == "hecho"
    assert job()["outcome"]["estado"] == "investigado"


@pytest.mark.parametrize("flag", ["is_bot", "deleted"])
def test_bots_and_deleted_users_are_skipped_without_research(conn, reader, monkeypatch, flag):
    reader.add_profile("U2", "Beep", **{flag: True})
    queue.enqueue("U2", "miembro_nuevo")
    seen = stub_research(monkeypatch)
    assert runner.run_next_job(reader).status == "omitido"
    assert seen == []
    assert job()["outcome"]["estado"] == "omitido"


@pytest.mark.parametrize(
    "stop", [guards.MonthlyBudgetExceeded("tope"), guards.KillSwitchActive("off")]
)
def test_a_system_stop_hands_the_job_back_and_propagates(conn, reader, monkeypatch, stop):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch, error=stop)
    with pytest.raises(type(stop)):
        runner.run_next_job(reader)
    assert (job()["status"], job()["attempts"]) == ("pendiente", 0)


def test_a_slack_auth_failure_hands_the_job_back_and_propagates(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch)
    reader.auth_error = SlackAuthFailed("token_revoked")
    with pytest.raises(SlackAuthFailed):
        runner.run_next_job(reader)
    assert (job()["status"], job()["attempts"]) == ("pendiente", 0)


def test_a_research_crash_is_retried_later(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch, error=RuntimeError("SearXNG caído"))
    assert runner.run_next_job(reader).status == "reintento"
    assert job()["status"] == "pendiente"
    assert job()["not_before"] is not None


def test_nobody_to_research_is_given_up_at_once(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch, error=ValueError("hace falta al menos un nombre o una empresa"))
    assert runner.run_next_job(reader).status == "fallido"
    assert job()["status"] == "fallido"


def test_the_hourly_limit_holds_back_new_jobs(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch)
    db.execute("update config set value = '0'::jsonb where key = 'research_por_hora'")
    try:
        assert runner.run_next_job(reader).status == "limitado"
        assert job()["status"] == "pendiente"
    finally:
        db.execute("update config set value = '20'::jsonb where key = 'research_por_hora'")


# -- Requisito B: el runner no puede confundir "ya no era nuestra" con
# "reintento" o "hecho" cuando reclaim_stale se adelantó y otro worker ya
# reclamó la tarea. --


def test_a_stolen_job_is_reported_as_descartado_not_hecho(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    seen = stub_research(monkeypatch)

    real_claim_next = queue.claim_next

    def steal_after_claiming():
        claimed = real_claim_next()
        if claimed is not None:
            db.execute(
                "update research_jobs set started_at = now() - interval '45 minutes' where id = %s",
                (claimed["id"],),
            )
            assert queue.reclaim_stale() == 1
            real_claim_next()  # otro worker se lleva la tarea
        return claimed

    monkeypatch.setattr(queue, "claim_next", steal_after_claiming)

    result = runner.run_next_job(reader)

    assert result.status == "descartado"
    assert seen  # la investigación sí llegó a correr con el perfil de U1
    row = db.fetch_one("select status from research_jobs")
    assert row["status"] == "en_curso"  # la tarea del segundo worker sigue intacta


def test_a_stolen_job_that_would_have_retried_is_reported_as_descartado(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch, error=RuntimeError("SearXNG caído"))

    real_claim_next = queue.claim_next

    def steal_after_claiming():
        claimed = real_claim_next()
        if claimed is not None:
            db.execute(
                "update research_jobs set started_at = now() - interval '45 minutes' where id = %s",
                (claimed["id"],),
            )
            assert queue.reclaim_stale() == 1
            real_claim_next()
        return claimed

    monkeypatch.setattr(queue, "claim_next", steal_after_claiming)

    result = runner.run_next_job(reader)

    assert result.status == "descartado"
    row = db.fetch_one("select status, last_error from research_jobs")
    assert row["status"] == "en_curso"
    assert row["last_error"] is None
