from types import SimpleNamespace

import pytest

from handoff_agent import guards
from handoff_agent.ingest import loop
from handoff_agent.ingest.research_runner import RunResult
from handoff_agent.ingest.resolver import ResolveResult
from handoff_agent.ingest.watcher import TickResult
from handoff_agent.slack_client import SlackAuthFailed
from tests.slack_fakes import FakeReader

REAL_REGISTRAR_OK = loop.estado_lector.registrar_ok
REAL_REGISTRAR_FALLO = loop.estado_lector.registrar_fallo


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
    calls = {
        "watch": 0,
        "alerts": [],
        "radar": 0,
        "decisor": 0,
        "senales": [],
        "rastro": [],
        "texto": [],
        "resumenes": 0,
        "avisos": 0,
        "ok": [],
        "fallos": [],
    }

    def radar_tick(**kwargs):
        calls["radar"] += 1
        return []

    def decisor_tick(**kwargs):
        calls["decisor"] += 1
        return []

    def watch(*args, **kwargs):
        calls["watch"] += 1
        return TickResult()

    monkeypatch.setattr(loop, "watch_tick", watch)
    monkeypatch.setattr(loop, "resolve_pending", lambda: ResolveResult())
    monkeypatch.setattr(loop, "run_next_job", lambda reader: None)
    # Nunca JobSpy de verdad desde el bucle en los tests.
    monkeypatch.setattr(loop.radar, "escanear_pendientes", radar_tick)
    # Ni OpenAI ni Unipile de verdad: el decisor también se sustituye.
    monkeypatch.setattr(loop.decisor, "procesar_pendientes", decisor_tick)
    # Ni OpenAI ni Slack de verdad: las señales y el aviso de credencial también.
    monkeypatch.setattr(
        loop.alertas,
        "publicar_pendientes",
        lambda reader=None: calls["senales"].append(reader) or 0,
    )
    monkeypatch.setattr(
        loop.aviso_lector,
        "avisar_credencial_caducada",
        lambda: calls.__setitem__("avisos", calls["avisos"] + 1) or True,
    )
    # Ni Slack ni la base de verdad para el rastro de los DMs y su resumen.
    monkeypatch.setattr(
        loop.dms,
        "rastro_tick",
        lambda reader: calls["rastro"].append(reader) or loop.dms.ResultadoRastro(),
    )
    # Ni Slack ni la base para el texto de los DMs (0031).
    monkeypatch.setattr(
        loop.dm_texto,
        "pasada",
        lambda reader: calls["texto"].append(reader) or loop.dm_texto.Resultado(),
    )
    monkeypatch.setattr(
        loop.seguimiento,
        "publicar_resumen",
        lambda: calls.__setitem__("resumenes", calls["resumenes"] + 1) or 0,
    )
    monkeypatch.setattr(loop.ops_alerts, "alert", lambda kind, msg: calls["alerts"].append(kind))
    # El latido no escribe en la base desde estos tests: se anota lo que se habría escrito.
    monkeypatch.setattr(loop.estado_lector, "registrar_ok", lambda *a: calls["ok"].append(a))
    monkeypatch.setattr(loop.estado_lector, "registrar_fallo", lambda e: calls["fallos"].append(e))
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


def test_a_channels_callable_is_resolved_on_every_poll(wiring, monkeypatch):
    """Los canales del panel cambian sin reiniciar: el bucle los pide en cada sondeo."""
    seen = []
    monkeypatch.setattr(
        loop,
        "watch_tick",
        lambda reader, channels, hours, **k: seen.append(channels) or TickResult(),
    )
    lists = iter([["C1"], ["C1", "C2"], ["C3"]])
    clock = FakeClock()
    loop.run_loop(
        FakeReader(),
        channels=lambda: next(lists),
        lookback_hours=1,
        poll_seconds=100,
        sleep=clock.sleep,
        clock=clock.clock,
        max_cycles=10,
    )
    assert seen == [["C1"], ["C1", "C2"], ["C3"]]


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


def test_recovering_a_job_raises_an_alert(wiring, monkeypatch):
    """Un reclamo puede significar que se pagó el research dos veces: un log no
    basta, tiene que avisar."""
    recuperadas = iter([2])
    monkeypatch.setattr(loop.queue, "reclaim_stale", lambda: next(recuperadas, 0))
    run(FakeClock(), max_cycles=1)
    # El reclamo del arranque encuentra las dos; el de la primera lectura ya no
    # encuentra nada, y entonces no vuelve a avisar.
    assert wiring["alerts"] == ["tareas_recuperadas"]


def test_reclaim_stale_is_silent_when_there_is_nothing_to_recover(wiring, monkeypatch):
    monkeypatch.setattr(loop.queue, "reclaim_stale", lambda: 0)
    run(FakeClock(), max_cycles=1)
    assert wiring["alerts"] == []


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


def test_the_radar_runs_once_per_cycle(wiring):
    run(FakeClock(), max_cycles=4)
    assert wiring["radar"] == 4


def test_a_radar_failure_does_not_stop_the_research(wiring, monkeypatch):
    """JobSpy o Postgres pueden fallar en el radar: la cola de research sigue."""
    researched = []

    def boom(**kwargs):
        raise RuntimeError("jobspy exploded")

    monkeypatch.setattr(loop.radar, "escanear_pendientes", boom)
    monkeypatch.setattr(
        loop, "run_next_job", lambda reader: researched.append(1) or RunResult("hecho", "U1")
    )
    clock = FakeClock()
    assert run(clock, max_cycles=2) == 2
    assert researched == [1, 1]
    assert wiring["alerts"] == []


def test_the_kill_switch_in_the_radar_is_not_a_failure(wiring, monkeypatch):
    def stopped(**kwargs):
        raise guards.KillSwitchActive("kill_switch is on")

    monkeypatch.setattr(loop.radar, "escanear_pendientes", stopped)
    assert run(FakeClock(), max_cycles=1) == 1
    assert wiring["alerts"] == []


def test_the_decisor_runs_once_per_cycle_after_the_radar(wiring):
    run(FakeClock(), max_cycles=3)
    assert wiring["decisor"] == 3


def test_a_decisor_failure_does_not_stop_the_research(wiring, monkeypatch):
    researched = []

    def boom(**kwargs):
        raise RuntimeError("unipile exploded")

    monkeypatch.setattr(loop.decisor, "procesar_pendientes", boom)
    monkeypatch.setattr(
        loop, "run_next_job", lambda reader: researched.append(1) or RunResult("hecho", "U1")
    )
    assert run(FakeClock(), max_cycles=2) == 2
    assert researched == [1, 1]
    assert wiring["alerts"] == []


@pytest.mark.parametrize(
    "stop",
    [guards.KillSwitchActive("kill_switch is on"), guards.MonthlyBudgetExceeded("$150 of $150")],
)
def test_a_system_stop_in_the_decisor_is_not_a_cycle_failure(wiring, monkeypatch, stop):
    def stopped(**kwargs):
        raise stop

    monkeypatch.setattr(loop.decisor, "procesar_pendientes", stopped)
    assert run(FakeClock(), max_cycles=1) == 1
    assert "ciclo_fallido" not in wiring["alerts"]


# -- latido del lector (estado_lector) --


def test_a_successful_poll_writes_the_heartbeat_with_user_team_and_channels(wiring, monkeypatch):
    monkeypatch.setattr(
        loop,
        "watch_tick",
        lambda *a, **k: TickResult(user_id="UANTHONY", team="Handoff", channels_read=3),
    )
    run(FakeClock(), max_cycles=1)
    assert wiring["ok"] == [("UANTHONY", "Handoff", 3)]
    assert wiring["fallos"] == []


@pytest.mark.parametrize(
    "error",
    [
        SlackAuthFailed("invalid_auth"),
        UnicodeEncodeError("latin-1", "…", 0, 1, "ordinal not in range"),
    ],
)
def test_a_failing_poll_writes_the_heartbeat_and_still_follows_the_usual_path(
    wiring, monkeypatch, error
):
    def boom(*a, **k):
        raise error

    monkeypatch.setattr(loop, "watch_tick", boom)
    run(FakeClock(), max_cycles=1)
    assert wiring["fallos"] == [error]
    assert wiring["ok"] == []
    # SlackAuthFailed sigue avisando; lo demás, por la vía del ciclo fallido.
    expected = "slack_auth" if isinstance(error, SlackAuthFailed) else "ciclo_fallido"
    assert wiring["alerts"] == [expected]


def test_a_heartbeat_write_that_raises_does_not_break_the_loop(wiring, monkeypatch):
    # Con el escritor real y la base rota: el sondeo, la cola y el radar siguen.
    monkeypatch.setattr(loop.estado_lector, "registrar_ok", REAL_REGISTRAR_OK)
    monkeypatch.setattr(loop.estado_lector, "registrar_fallo", REAL_REGISTRAR_FALLO)

    def broken(*a, **k):
        raise RuntimeError("base caída")

    # Solo el módulo del latido ve la base rota; el resto del bucle no la toca.
    monkeypatch.setattr(loop.estado_lector, "db", SimpleNamespace(execute=broken))
    assert run(FakeClock(), max_cycles=2) == 2
    assert wiring["alerts"] == []
    assert wiring["radar"] == 2


# -- señales en los mensajes --


def test_the_signals_run_once_per_cycle_with_the_reader(wiring):
    run(FakeClock(), max_cycles=3)
    assert len(wiring["senales"]) == 3
    assert all(isinstance(r, FakeReader) for r in wiring["senales"])


def test_the_signals_run_after_the_radar_and_the_decisor(wiring, monkeypatch):
    orden = []
    monkeypatch.setattr(loop.radar, "escanear_pendientes", lambda **k: orden.append("radar") or [])
    monkeypatch.setattr(
        loop.decisor, "procesar_pendientes", lambda **k: orden.append("decisor") or []
    )
    monkeypatch.setattr(
        loop.alertas, "publicar_pendientes", lambda reader=None: orden.append("senales") or 0
    )
    monkeypatch.setattr(loop, "run_next_job", lambda reader: orden.append("research") or None)
    run(FakeClock(), max_cycles=1)
    assert orden == ["radar", "decisor", "senales", "research"]


def test_a_signals_failure_does_not_stop_the_research_or_alert(wiring, monkeypatch):
    researched = []

    def boom(reader=None):
        raise RuntimeError("openai exploded")

    monkeypatch.setattr(loop.alertas, "publicar_pendientes", boom)
    monkeypatch.setattr(
        loop, "run_next_job", lambda reader: researched.append(1) or RunResult("hecho", "U1")
    )
    assert run(FakeClock(), max_cycles=2) == 2
    assert researched == [1, 1]
    assert wiring["alerts"] == []
    assert wiring["radar"] == 2 and wiring["decisor"] == 2


# -- aviso cuando caduca la credencial del lector --


def test_a_dead_token_in_the_poll_also_notifies_slack(wiring, monkeypatch):
    def watch(*args, **kwargs):
        raise SlackAuthFailed("token_revoked")

    monkeypatch.setattr(loop, "watch_tick", watch)
    run(FakeClock(), max_cycles=1)
    assert wiring["alerts"] == ["slack_auth"]
    assert wiring["avisos"] == 1


def test_a_dead_token_in_the_research_also_notifies_slack(wiring, monkeypatch):
    def dead(reader):
        raise SlackAuthFailed("invalid_auth")

    monkeypatch.setattr(loop, "run_next_job", dead)
    run(FakeClock(), max_cycles=1)
    assert wiring["alerts"] == ["slack_auth"]
    assert wiring["avisos"] == 1


def test_a_notice_that_blows_up_does_not_break_the_loop(wiring, monkeypatch):
    def watch(*args, **kwargs):
        raise SlackAuthFailed("token_revoked")

    def boom():
        raise RuntimeError("slack down")

    monkeypatch.setattr(loop, "watch_tick", watch)
    monkeypatch.setattr(loop.aviso_lector, "avisar_credencial_caducada", boom)
    assert run(FakeClock(), max_cycles=2) == 2
    assert wiring["alerts"] == ["slack_auth"]
    assert "ciclo_fallido" not in wiring["alerts"]
    assert wiring["radar"] == 2


def test_a_healthy_poll_sends_no_notice(wiring):
    run(FakeClock(), max_cycles=3)
    assert wiring["avisos"] == 0


def test_another_failure_of_the_poll_does_not_notify_about_the_credential(wiring, monkeypatch):
    monkeypatch.setattr(
        loop, "watch_tick", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db"))
    )
    run(FakeClock(), max_cycles=1)
    assert wiring["avisos"] == 0


# --- rastro de los mensajes directos y su resumen ---------------------------------


def test_the_dm_trace_runs_at_start_and_then_on_its_own_interval(wiring):
    """No en cada vuelta: una conversación que se enfría no tiene prisa."""
    clock = FakeClock()
    run(clock, max_cycles=10)
    # Cada vuelta duerme 30 s: en 10 vueltas el reloj llega a 300 s, y la
    # pasada toca cada 15 min (900 s). Solo la del arranque.
    assert len(wiring["rastro"]) == 1
    assert wiring["resumenes"] == 1


def test_the_dm_trace_runs_again_once_the_interval_passes(wiring, monkeypatch):
    monkeypatch.setattr(loop.dms, "MINUTOS_ENTRE_PASADAS", 1)
    clock = FakeClock()
    run(clock, max_cycles=10)
    # 300 s de reloj y una pasada por minuto: t=0, 60, 120, 180, 240.
    assert len(wiring["rastro"]) == 5
    assert wiring["resumenes"] == 5


def test_the_dm_trace_gets_the_reader(wiring):
    run(FakeClock(), max_cycles=1)
    assert isinstance(wiring["rastro"][0], FakeReader)


def test_a_failing_dm_trace_does_not_stop_research_or_delivery(wiring, monkeypatch):
    def boom(reader):
        raise RuntimeError("Slack raro")

    monkeypatch.setattr(loop.dms, "rastro_tick", boom)
    jobs = []
    monkeypatch.setattr(loop, "run_next_job", lambda reader: jobs.append(1) or None)
    assert run(FakeClock(), max_cycles=3) == 3
    # El research sigue corriendo y nadie avisa de una parada del sistema.
    assert len(jobs) == 3
    assert wiring["alerts"] == []


def test_a_dead_credential_in_the_dm_trace_warns_the_person_who_pastes_the_token(
    wiring, monkeypatch
):
    def caducada(reader):
        raise SlackAuthFailed("token_revoked")

    monkeypatch.setattr(loop.dms, "rastro_tick", caducada)
    assert run(FakeClock(), max_cycles=1) == 1
    assert "slack_auth" in wiring["alerts"]
    assert wiring["avisos"] == 1


def test_a_failing_summary_does_not_stop_the_loop(wiring, monkeypatch):
    def boom():
        raise RuntimeError("ledger caído")

    monkeypatch.setattr(loop.seguimiento, "publicar_resumen", boom)
    assert run(FakeClock(), max_cycles=2) == 2
    assert wiring["alerts"] == []


# --- texto de los mensajes directos (0031) ----------------------------------------


def test_the_dm_text_runs_right_after_the_trace_and_before_the_alerts(wiring, monkeypatch):
    """El rastro acaba de decir qué se movió; los avisos deciden después, con todo fresco."""
    orden = []
    monkeypatch.setattr(
        loop.dms, "rastro_tick", lambda r: orden.append("rastro") or loop.dms.ResultadoRastro()
    )
    monkeypatch.setattr(
        loop.dm_texto, "pasada", lambda r: orden.append("texto") or loop.dm_texto.Resultado()
    )
    monkeypatch.setattr(loop.seguimiento, "publicar_avisos", lambda: orden.append("avisos") or 0)
    run(FakeClock(), max_cycles=1)
    assert orden == ["rastro", "texto", "avisos"]


def test_the_dm_text_follows_the_trace_cadence(wiring, monkeypatch):
    monkeypatch.setattr(loop.dms, "MINUTOS_ENTRE_PASADAS", 1)
    run(FakeClock(), max_cycles=10)
    assert len(wiring["texto"]) == len(wiring["rastro"]) == 5
    assert isinstance(wiring["texto"][0], FakeReader)


def test_the_dm_text_runs_once_at_start_with_the_usual_interval(wiring):
    run(FakeClock(), max_cycles=10)
    assert len(wiring["texto"]) == 1


def test_a_failing_dm_text_pass_does_not_stop_research_or_alert(wiring, monkeypatch):
    def boom(reader):
        raise RuntimeError("Postgres raro")

    monkeypatch.setattr(loop.dm_texto, "pasada", boom)
    avisos = []
    monkeypatch.setattr(loop.seguimiento, "publicar_avisos", lambda: avisos.append(1) or 0)
    jobs = []
    monkeypatch.setattr(loop, "run_next_job", lambda reader: jobs.append(1) or None)
    assert run(FakeClock(), max_cycles=3) == 3
    assert len(jobs) == 3
    # Lo que va después en la misma pasada sigue corriendo.
    assert avisos == [1]
    assert wiring["resumenes"] == 1
    assert wiring["alerts"] == []


def test_a_dead_credential_in_the_dm_text_warns_the_person_who_pastes_the_token(
    wiring, monkeypatch
):
    def caducada(reader):
        raise SlackAuthFailed("token_revoked")

    monkeypatch.setattr(loop.dm_texto, "pasada", caducada)
    assert run(FakeClock(), max_cycles=1) == 1
    assert "slack_auth" in wiring["alerts"]
    assert wiring["avisos"] == 1


def test_the_dm_text_pass_logs_its_errors_without_raising(wiring, monkeypatch, caplog):
    resultado = loop.dm_texto.Resultado(conversaciones=2, guardados=3, errores=["DBBB: caído"])
    monkeypatch.setattr(loop.dm_texto, "pasada", lambda r: resultado)
    with caplog.at_level("INFO", logger=loop.__name__):
        assert run(FakeClock(), max_cycles=1) == 1
    assert any("2 conversaciones, 3 mensajes nuevos" in r.getMessage() for r in caplog.records)
    assert any("DBBB: caído" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------------
# Modo automático de canales (0028): el descubrimiento y de dónde salen los canales.
# ---------------------------------------------------------------------------------


def test_the_usual_path_never_discovers_channels(wiring, monkeypatch):
    """Apagado el modo automático, el bucle no le pregunta a Slack qué canales hay:
    usa la lista del panel, como siempre."""
    monkeypatch.setattr(loop.canales, "modo_todos", lambda: False)
    reader = FakeReader()
    clock = FakeClock()
    loop.run_loop(
        reader,
        channels=["C00001"],
        lookback_hours=1,
        poll_seconds=100,
        sleep=clock.sleep,
        clock=clock.clock,
        max_cycles=10,
    )
    assert reader.workspace == []  # nadie pidió la lista
    assert wiring["watch"] >= 1


def test_the_automatic_mode_takes_its_channels_from_the_table(wiring, monkeypatch):
    seen = []
    monkeypatch.setattr(
        loop,
        "watch_tick",
        lambda reader, channels, hours, **k: seen.append((channels, k)) or TickResult(),
    )
    monkeypatch.setattr(loop.canales, "modo_todos", lambda: True)
    monkeypatch.setattr(
        loop.descubrimiento, "guardar_descubrimiento", lambda *a: {"vistos": 2, "dentro": 2}
    )
    monkeypatch.setattr(loop.canales, "por_sondear", lambda: ["C00007", "C00008"])
    clock = FakeClock()
    loop.run_loop(
        FakeReader(),
        # La lista del panel está puesta y se ignora a propósito: manda la tabla.
        channels=["C00001"],
        lookback_hours=1,
        poll_seconds=100,
        sleep=clock.sleep,
        clock=clock.clock,
        max_cycles=3,
    )
    assert seen[0][0] == ["C00007", "C00008"]
    assert seen[0][1] == {"rotando": True}


def test_discovery_failing_does_not_stop_the_poll(wiring, monkeypatch):
    """Si Slack no contesta a la lista de canales, se sigue con los que la tabla ya sabe."""
    monkeypatch.setattr(loop.canales, "modo_todos", lambda: True)
    monkeypatch.setattr(loop.canales, "por_sondear", lambda: ["C00007"])
    reader = FakeReader()
    reader.canales_error = RuntimeError("slack caído")
    clock = FakeClock()
    assert (
        loop.run_loop(
            reader,
            channels=["C00001"],
            lookback_hours=1,
            poll_seconds=100,
            sleep=clock.sleep,
            clock=clock.clock,
            max_cycles=2,
        )
        == 2
    )
    assert wiring["watch"] >= 1
