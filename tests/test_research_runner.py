from decimal import Decimal

import pytest

from handoff_agent import db, guards
from handoff_agent.ingest import queue
from handoff_agent.ingest import research_runner as runner
from handoff_agent.research.worker import ResearchOutcome
from handoff_agent.slack_client import SlackAuthFailed
from handoff_agent.tools import prospects
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


def test_a_panel_override_beats_the_email_domain(conn, reader, monkeypatch):
    """`company_domain_override` (puesto a mano desde el panel, ver
    ingest/research_runner.py `_domain_override`) gana al dominio del correo:
    alguien ya lo corrigió a propósito tras ver una adivinanza equivocada."""
    prospects.upsert_prospect("U1", full_name="Ada Ruiz")
    db.execute(
        "update prospects set company_domain_override = 'override.example' "
        "where slack_user_id = 'U1'"
    )
    queue.enqueue("U1", "mensaje")
    seen = stub_research(monkeypatch)
    assert runner.run_next_job(reader).status == "hecho"
    assert seen[0]["domain"] == "override.example"


def test_the_email_domain_is_used_when_there_is_no_override(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    seen = stub_research(monkeypatch)
    assert runner.run_next_job(reader).status == "hecho"
    assert seen[0]["domain"] == "acme.com"


def test_no_override_and_no_work_email_leaves_the_domain_to_be_guessed(conn, monkeypatch):
    reader = FakeReader()
    reader.add_profile("U3", "Bob Nadie", title="CEO @ Nadie", email="bob@gmail.com")
    queue.enqueue("U3", "mensaje")
    seen = stub_research(monkeypatch)
    assert runner.run_next_job(reader).status == "hecho"
    # Sin override y con un correo personal (gmail), domain_from_email
    # descarta el dominio: research/gather.py es quien adivina, no el runner.
    assert seen[0]["domain"] is None


def test_a_manual_job_forces_research_but_a_mensaje_job_does_not(conn, reader, monkeypatch):
    """Corrección 8: sin `force`, un job manual sobre alguien con dossier
    vigente termina en "omitido: dossier vigente" y el botón "Investigar más"
    (o "Volver a investigar" del panel) no hace nada."""
    seen_force = []

    def research(full_name=None, company=None, domain=None, slack_user_id=None, force=False):
        seen_force.append(force)
        return ResearchOutcome("pid", slack_user_id, "investigado", 1, Decimal("0.02"), None)

    monkeypatch.setattr(runner.worker, "research_person", research)

    queue.enqueue("U1", "mensaje")
    assert runner.run_next_job(reader).status == "hecho"

    queue.enqueue("U1", "manual")
    assert runner.run_next_job(reader).status == "hecho"

    assert seen_force == [False, True]


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


def test_a_finished_research_is_delivered(conn, reader, monkeypatch):
    """Corrección 2: `run_next_job` entrega justo después de un `complete()`
    con éxito, para un estado y versión entregables."""
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch)
    seen = []
    monkeypatch.setattr(
        runner,
        "deliver_for",
        lambda prospect_id, passed_reader: seen.append((prospect_id, passed_reader)),
    )
    assert runner.run_next_job(reader).status == "hecho"
    assert seen == [("pid", reader)]


def test_delivery_failing_does_not_turn_a_finished_job_into_a_failure(conn, reader, monkeypatch):
    """La investigación ya está pagada y guardada: un fallo de entrega (un
    error de base de datos, un bug) nunca debe convertir un `hecho` en un
    reintento o un fallo."""
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch)

    def boom(prospect_id, passed_reader):
        raise RuntimeError("entrega caída")

    monkeypatch.setattr(runner, "deliver_for", boom)
    result = runner.run_next_job(reader)
    assert result.status == "hecho"
    assert job()["status"] == "hecho"


def test_a_high_band_delivery_triggers_an_sms(conn, reader, monkeypatch):
    """Task 7: el SMS solo se dispara cuando `deliver_for` de verdad publicó
    la tarjeta y la banda resultante es 'alta'."""
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch)
    monkeypatch.setattr(runner, "deliver_for", lambda prospect_id, passed_reader: "alta")
    seen = []
    monkeypatch.setattr(
        runner.sms, "maybe_send", lambda prospect_id, band: seen.append((prospect_id, band))
    )
    assert runner.run_next_job(reader).status == "hecho"
    assert seen == [("pid", "alta")]


def test_a_media_or_baja_delivery_never_triggers_an_sms(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch)
    monkeypatch.setattr(runner, "deliver_for", lambda prospect_id, passed_reader: "media")
    seen = []
    monkeypatch.setattr(
        runner.sms, "maybe_send", lambda prospect_id, band: seen.append((prospect_id, band))
    )
    assert runner.run_next_job(reader).status == "hecho"
    assert seen == []


def test_an_sms_failure_does_not_turn_a_finished_job_into_a_failure(conn, reader, monkeypatch):
    """Corrección 4: el SMS corre dentro de la misma protección que la
    tarjeta -- un problema de Twilio nunca puede tumbar un `hecho`."""
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch)
    monkeypatch.setattr(runner, "deliver_for", lambda prospect_id, passed_reader: "alta")

    def boom(prospect_id, band):
        raise RuntimeError("twilio caído")

    monkeypatch.setattr(runner.sms, "maybe_send", boom)
    result = runner.run_next_job(reader)
    assert result.status == "hecho"
    assert job()["status"] == "hecho"


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
