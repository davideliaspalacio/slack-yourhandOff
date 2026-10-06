from datetime import UTC, datetime

import pytest

from handoff_agent import db
from handoff_agent.ingest import queue, resolver
from handoff_agent.tools import prospects


def store(user: str, ts: str, status: str = "nuevo") -> None:
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, status) "
        "values ('C1', %s, %s, 'hola', %s)",
        (ts, user, status),
    )


def person(user: str, state: str) -> None:
    prospects.upsert_prospect(user, full_name="X")
    db.execute("update prospects set state = %s where slack_user_id = %s", (state, user))


def message_status(ts: str) -> str:
    return db.fetch_one("select status from slack_messages where ts = %s", (ts,))["status"]


def test_an_unknown_author_gets_a_research_job(conn):
    store("U1", "1.0")
    result = resolver.resolve_pending()
    assert (result.enqueued, result.to_scoring) == (1, 1)
    assert message_status("1.0") == "pendiente_scoring"
    assert db.fetch_one("select reason from research_jobs")["reason"] == "mensaje"


def test_two_messages_from_the_same_stranger_make_one_job(conn):
    store("U1", "1.0")
    store("U1", "2.0")
    assert resolver.resolve_pending().enqueued == 1
    assert queue.status_counts() == {"pendiente": 1}


def test_a_discarded_author_is_archived_and_never_researched(conn):
    person("U1", "descartado")
    store("U1", "1.0")
    assert resolver.resolve_pending().archived == 1
    assert message_status("1.0") == "archivado"
    assert queue.status_counts() == {}


@pytest.mark.parametrize("state", ["investigado", "contactado", "cliente"])
def test_an_investigated_author_goes_straight_to_scoring(conn, state):
    person("U1", state)
    store("U1", "1.0")
    assert resolver.resolve_pending().enqueued == 0
    assert message_status("1.0") == "pendiente_scoring"


def test_an_incomplete_author_is_researched_again(conn):
    person("U1", "incompleto")
    store("U1", "1.0")
    assert resolver.resolve_pending().enqueued == 1


def test_ignored_messages_are_left_alone(conn):
    store("U1", "1.0", status="ignorado")
    resolver.resolve_pending()
    assert message_status("1.0") == "ignorado"
    assert queue.status_counts() == {}


def test_null_user_id_is_marked_ignorado(conn):
    # Store a message with NULL user_id directly
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, status) "
        "values ('C1', %s, %s, 'hola', %s)",
        ("1.0", None, "nuevo"),
    )
    # Store a normal message alongside it
    store("U1", "2.0")
    result = resolver.resolve_pending()
    # The NULL user_id message should be ignorado, no job created
    assert message_status("1.0") == "ignorado"
    # The normal message should be resolved normally
    assert message_status("2.0") == "pendiente_scoring"
    assert result.enqueued == 1
    assert result.to_scoring == 1
    assert result.failed == 0
    # Only one job created (for U1, not for NULL)
    assert db.fetch_one("select count(*) as cnt from research_jobs")["cnt"] == 1


def test_a_row_with_enqueue_failure_does_not_stop_others(conn, monkeypatch):
    # Create a wrapper that makes enqueue fail for UBAD
    original_enqueue = queue.enqueue

    def failing_enqueue(user_id, reason, not_before=None):
        if user_id == "UBAD":
            raise RuntimeError("db caída")
        return original_enqueue(user_id, reason, not_before=not_before)

    monkeypatch.setattr(queue, "enqueue", failing_enqueue)

    # Store UBAD message first (older ts) and U2 message (newer ts)
    store("UBAD", "1.0")  # Will fail on enqueue
    store("U2", "2.0")  # Should still be processed

    result = resolver.resolve_pending()

    # UBAD message should stay nuevo (due to error)
    assert message_status("1.0") == "nuevo"
    # U2 message should be pendiente_scoring with job created
    assert message_status("2.0") == "pendiente_scoring"
    assert result.enqueued == 1
    assert result.failed == 1
    assert result.to_scoring == 1
    # Only one job (U2's)
    assert db.fetch_one("select count(*) as cnt from research_jobs")["cnt"] == 1


def ahora() -> float:
    """La hora de la base, no la del equipo: los ts de los tests se comparan con
    el now() de Postgres, y el reloj de Docker puede ir desfasado."""
    return float(db.fetch_one("select extract(epoch from now()) as t")["t"])


def store_at(user: str, ts: float, status: str = "nuevo") -> None:
    store(user, f"{ts:.6f}", status)


def not_before_and_now() -> dict:
    return db.fetch_one("select not_before, now() as ahora from research_jobs")


def test_a_message_job_waits_for_the_last_message_plus_the_settle_time(conn):
    hora = ahora()
    store_at("U1", hora - 20)
    store_at("U1", hora - 5)  # el último: manda este
    assert resolver.resolve_pending().enqueued == 1
    row = not_before_and_now()
    esperado = datetime.fromtimestamp(hora - 5 + 60, UTC)
    assert abs((row["not_before"] - esperado).total_seconds()) < 1
    assert queue.claim_next() is None


def test_a_message_job_becomes_claimable_once_the_wait_is_over(conn):
    store_at("U1", ahora() - 120)  # más viejo que los 60 s de espera
    resolver.resolve_pending()
    assert queue.claim_next()["slack_user_id"] == "U1"


def test_the_wait_comes_from_config(restore_config):
    db.execute("update config set value = '300'::jsonb where key = 'espera_mensajes_segundos'")
    hora = ahora()
    store_at("U1", hora)
    resolver.resolve_pending()
    row = not_before_and_now()
    assert abs((row["not_before"] - datetime.fromtimestamp(hora + 300, UTC)).total_seconds()) < 1


def test_ignored_messages_do_not_push_the_wait(conn):
    hora = ahora()
    store_at("U1", hora - 100)
    store_at("U1", hora, status="ignorado")
    resolver.resolve_pending()
    row = not_before_and_now()
    assert row["not_before"] < row["ahora"]


def test_a_second_message_while_the_job_is_pending_makes_no_second_job(conn):
    store_at("U1", ahora() - 10)
    assert resolver.resolve_pending().enqueued == 1
    first = db.fetch_one("select id, not_before from research_jobs")
    store_at("U1", ahora())
    assert resolver.resolve_pending().enqueued == 0
    assert db.fetch_all("select id, not_before from research_jobs") == [first]


@pytest.mark.parametrize("reason", ["miembro_nuevo", "manual", "radar"])
def test_other_reasons_stay_claimable_immediately(conn, reason):
    queue.enqueue("U1", reason)
    assert queue.claim_next()["slack_user_id"] == "U1"
