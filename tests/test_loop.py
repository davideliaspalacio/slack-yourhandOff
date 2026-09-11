import pytest

from handoff_agent import guards
from handoff_agent.ingest import loop
from handoff_agent.ingest.research_runner import RunResult
from handoff_agent.ingest.resolver import ResolveResult
from handoff_agent.ingest.watcher import TickResult
from handoff_agent.slack_client import SlackAuthFailed
from tests.slack_fakes import FakeReader


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def wiring(monkeypatch):
    calls = {"watch": 0, "alerts": []}

    def watch(*args, **kwargs):
        calls["watch"] += 1
        return TickResult()

    monkeypatch.setattr(loop, "watch_tick", watch)
    monkeypatch.setattr(loop, "resolve_pending", lambda: ResolveResult())
    monkeypatch.setattr(loop, "run_next_job", lambda reader: None)
    monkeypatch.setattr(loop.ops_alerts, "alert", lambda kind, msg: calls["alerts"].append(kind))
    return calls


def run(clock, max_cycles, poll_seconds=100):
    return loop.run_loop(
        FakeReader(),
        channels=["C1"],
        lookback_hours=1,
        poll_seconds=poll_seconds,
        sleep=clock.sleep,
        clock=clock.clock,
        max_cycles=max_cycles,
    )


def test_slack_is_read_at_start_and_then_once_per_poll_interval(wiring):
    clock = FakeClock()
    run(clock, max_cycles=10)
    # Cola vacía: cada vuelta duerme IDLE_SECONDS (30). En 10 vueltas el reloj
    # llega a 300 s y las lecturas caen en t=0, t=120 y t=240.
    assert wiring["watch"] == 3
    assert clock.sleeps == [loop.IDLE_SECONDS] * 10


def test_a_dead_token_raises_an_alert_and_the_loop_keeps_going(wiring, monkeypatch):
    def watch(*args, **kwargs):
        raise SlackAuthFailed("token_revoked")

    monkeypatch.setattr(loop, "watch_tick", watch)
    assert run(FakeClock(), max_cycles=2) == 2
    assert "slack_auth" in wiring["alerts"]


def test_a_system_stop_raises_an_alert_and_pauses(wiring, monkeypatch):
    def stopped(reader):
        raise guards.MonthlyBudgetExceeded("spent $150 of $150")

    monkeypatch.setattr(loop, "run_next_job", stopped)
    clock = FakeClock()
    run(clock, max_cycles=1)
    assert wiring["alerts"] == ["parada_del_sistema"]
    assert clock.sleeps == [loop.SYSTEM_STOP_PAUSE_SECONDS]


def test_a_busy_queue_does_not_sleep(wiring, monkeypatch):
    monkeypatch.setattr(loop, "run_next_job", lambda reader: RunResult("hecho", "U1"))
    clock = FakeClock()
    run(clock, max_cycles=3)
    assert clock.sleeps == []


def test_should_stop_ends_the_loop_before_any_work(wiring):
    cycles = loop.run_loop(
        FakeReader(),
        channels=["C1"],
        lookback_hours=1,
        poll_seconds=100,
        should_stop=lambda: True,
    )
    assert cycles == 0
    assert wiring["watch"] == 0


def test_reclaim_stale_runs_at_startup_and_on_each_slack_read(wiring, monkeypatch):
    """Carried requirement B: a worker killed mid-research leaves its job
    en_curso; without reclaim_stale() that person can never be queued again."""
    calls = {"n": 0}

    def reclaim():
        calls["n"] += 1
        return 0

    monkeypatch.setattr(loop.queue, "reclaim_stale", reclaim)
    clock = FakeClock()
    run(clock, max_cycles=10)
    # 3 lecturas de Slack (t=0, 120, 240) + la llamada inicial antes del bucle.
    assert calls["n"] == 4


def test_reclaim_stale_logs_only_when_it_recovers_something(wiring, monkeypatch, caplog):
    monkeypatch.setattr(loop.queue, "reclaim_stale", lambda: 2)
    clock = FakeClock()
    with caplog.at_level("INFO", logger=loop.logger.name):
        run(clock, max_cycles=1)
    assert any("2" in record.getMessage() for record in caplog.records)


def test_reclaim_stale_is_silent_when_there_is_nothing_to_recover(wiring, monkeypatch, caplog):
    monkeypatch.setattr(loop.queue, "reclaim_stale", lambda: 0)
    with caplog.at_level("INFO", logger=loop.logger.name):
        run(FakeClock(), max_cycles=1)
    assert not any("recuperadas" in record.getMessage() for record in caplog.records)


def test_a_database_error_does_not_kill_the_worker(wiring, monkeypatch):
    """Postgres parpadea y el proceso muere: Railway lo reinicia en bucle y cada
    reinicio deja otra tarea en_curso huérfana. El ciclo tiene que aguantar."""

    def boom(reader):
        raise RuntimeError("connection pool exhausted")

    monkeypatch.setattr(loop, "run_next_job", boom)
    clock = FakeClock()
    assert run(clock, max_cycles=3) == 3
    assert wiring["alerts"] == ["ciclo_fallido"]
    # Y no gira en caliente contra una base caída.
    assert clock.sleeps == [loop.IDLE_SECONDS] * 3


def test_a_second_failure_streak_alerts_again(wiring, monkeypatch):
    outcomes = [RuntimeError("db"), None, RuntimeError("db")]

    def flaky(reader):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(loop, "run_next_job", flaky)
    run(FakeClock(), max_cycles=3)
    assert wiring["alerts"] == ["ciclo_fallido", "ciclo_fallido"]


def test_reclaim_stale_failing_at_startup_does_not_stop_the_worker(wiring, monkeypatch):
    calls = {"n": 0}

    def reclaim():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection refused")
        return 0

    monkeypatch.setattr(loop.queue, "reclaim_stale", reclaim)
    assert run(FakeClock(), max_cycles=1) == 1
    assert wiring["watch"] == 1
    assert wiring["alerts"] == []
