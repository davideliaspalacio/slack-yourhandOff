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

    def failing_enqueue(user_id, reason):
        if user_id == "UBAD":
            raise RuntimeError("db caída")
        return original_enqueue(user_id, reason)

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
