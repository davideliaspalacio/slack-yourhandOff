import pytest

from handoff_agent import db
from handoff_agent.ingest import queue, watcher
from handoff_agent.slack_client import SlackAuthFailed
from tests.slack_fakes import FakeReader

NOW = 1_726_000_000.0


def ts(seconds_ago: float) -> str:
    return f"{NOW - seconds_ago:.6f}"


@pytest.fixture
def reader():
    return FakeReader()


def tick(reader, channels=("C1",)):
    return watcher.watch_tick(reader, list(channels), lookback_hours=1, now=lambda: NOW)


def test_the_first_read_only_looks_back_the_configured_hours(conn, reader):
    """Sin backfill: lo de antes de la ventana no se lee."""
    reader.post("C1", "U1", "reciente", ts(600))
    reader.post("C1", "U2", "de hace dos horas", ts(7200))
    assert tick(reader).messages_new == 1
    assert db.fetch_one("select user_id from slack_messages")["user_id"] == "U1"


def test_later_reads_start_after_the_last_stored_message(conn, reader):
    reader.post("C1", "U1", "a", ts(600))
    tick(reader)
    reader.post("C1", "U2", "b", ts(300))
    assert tick(reader).messages_new == 1
    assert reader.history_calls[-1][1] == ts(600)


@pytest.mark.parametrize(
    ("user", "extra"),
    [
        ("U1", {"bot_id": "B1"}),
        ("U1", {"subtype": "channel_join"}),
        ("U1", {"subtype": "bot_message"}),
        ("UOWNER", {}),
    ],
)
def test_bots_system_messages_and_the_owner_are_ignored(conn, reader, user, extra):
    reader.post("C1", user, "x", ts(60), **extra)
    result = tick(reader)
    assert (result.messages_new, result.messages_ignored) == (0, 1)
    assert db.fetch_one("select status from slack_messages")["status"] == "ignorado"


def test_a_message_with_a_file_is_a_real_message(conn, reader):
    reader.post("C1", "U1", "mira esto", ts(60), subtype="file_share")
    assert tick(reader).messages_new == 1


def test_the_first_member_list_is_a_baseline_and_queues_nobody(conn, reader):
    """El primer padrón nunca encola a todos los miembros: sería el backfill
    de 1.400 personas que se descartó."""
    reader.set_members("C1", "U1", "U2", "U3")
    assert tick(reader).members_new == 0
    assert queue.status_counts() == {}
    assert db.fetch_one("select count(*) as n from member_snapshots")["n"] == 1


def test_new_members_are_queued_on_later_reads(conn, reader):
    reader.set_members("C1", "U1")
    tick(reader)
    reader.set_members("C1", "U1", "U2", "UOWNER")
    assert tick(reader).members_new == 1
    job = db.fetch_one("select slack_user_id, reason from research_jobs")
    assert job == {"slack_user_id": "U2", "reason": "miembro_nuevo"}


def test_one_channel_down_does_not_stop_the_others(conn, reader):
    reader.unavailable_channels.add("C1")
    reader.post("C2", "U1", "hola", ts(60))
    result = tick(reader, channels=("C1", "C2"))
    assert result.messages_new == 1
    assert len(result.errors) == 1 and "C1" in result.errors[0]


def test_an_auth_failure_propagates(conn, reader):
    reader.auth_error = SlackAuthFailed("Slack rechazó el token: token_revoked")
    with pytest.raises(SlackAuthFailed):
        tick(reader)
