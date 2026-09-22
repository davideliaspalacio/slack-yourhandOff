import json
import logging
import subprocess
import sys
import time
from decimal import Decimal

import pytest

from handoff_agent import cli, guards
from handoff_agent.research.worker import ResearchOutcome
from tests.slack_fakes import FakeReader
from tests.test_dossier import make_dossier


def outcome(status="investigado", cost="0.05", user="manual:ada-ruiz-acme"):
    return ResearchOutcome(
        "pid", user, status, 1 if status == "investigado" else None, Decimal(cost), None
    )


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """El lote pausa entre filas; en los tests la pausa solo se anota."""
    calls = []
    monkeypatch.setattr(time, "sleep", calls.append)
    return calls


def write_csv(tmp_path, rows):
    path = tmp_path / "empresas.csv"
    path.write_text("nombre,empresa,dominio\n" + "\n".join(rows) + "\n")
    return path


def test_batch_writes_a_report_with_totals(tmp_path, monkeypatch):
    csv = write_csv(tmp_path, ["Ada Ruiz,Acme,acme.com", "Leo Gil,Beta,"])
    monkeypatch.setattr(cli.worker, "research_person", lambda *a, **k: outcome())
    monkeypatch.setattr(
        cli.prospects, "historial_prospecto", lambda uid: {"dossier": {"content": make_dossier()}}
    )
    report = tmp_path / "informe.md"

    assert cli.main(["research-batch", str(csv), "--salida", str(report)]) == 0

    text = report.read_text()
    assert "Ada Ruiz" in text and "Leo Gil" in text
    assert "Investigados: 2" in text
    assert "$0.1000" in text  # coste total
    assert "$0.0500" in text  # coste medio por dossier


def test_batch_keeps_going_after_one_row_fails(tmp_path, monkeypatch):
    csv = write_csv(tmp_path, ["Ada Ruiz,Acme,", "Leo Gil,Beta,"])
    results = iter([RuntimeError("SearXNG caído"), outcome()])

    def research(*a, **k):
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(cli.worker, "research_person", research)
    monkeypatch.setattr(
        cli.prospects, "historial_prospecto", lambda uid: {"dossier": {"content": make_dossier()}}
    )
    report = tmp_path / "informe.md"
    cli.main(["research-batch", str(csv), "--salida", str(report)])
    text = report.read_text()
    assert "SearXNG caído" in text
    assert "Investigados: 1" in text


def test_batch_stops_at_the_monthly_cap(tmp_path, monkeypatch):
    """Seguir con el lote después del tope mensual es exactamente el gasto que
    el tope existe para impedir."""
    csv = write_csv(tmp_path, ["Ada Ruiz,Acme,", "Leo Gil,Beta,", "Eva Sol,Gamma,"])
    calls = []

    def research(*a, **k):
        calls.append(a)
        raise guards.MonthlyBudgetExceeded("spent $150 of $150")

    monkeypatch.setattr(cli.worker, "research_person", research)
    report = tmp_path / "informe.md"
    assert cli.main(["research-batch", str(csv), "--salida", str(report)]) == 1
    assert len(calls) == 1
    assert "detenido" in report.read_text().lower()


def test_single_research_prints_the_outcome(capsys, monkeypatch):
    monkeypatch.setattr(cli.worker, "research_person", lambda *a, **k: outcome())
    monkeypatch.setattr(
        cli.prospects, "historial_prospecto", lambda uid: {"dossier": {"content": make_dossier()}}
    )
    assert cli.main(["research", "Ada Ruiz", "--empresa", "Acme"]) == 0
    out = capsys.readouterr().out
    assert "investigado" in out
    assert "$0.0500" in out


def test_a_dossier_read_failure_is_a_warning_not_an_error(tmp_path, capsys, monkeypatch):
    csv = write_csv(tmp_path, ["Ada Ruiz,Acme,acme.com"])
    monkeypatch.setattr(cli.worker, "research_person", lambda *a, **k: outcome())
    monkeypatch.setattr(
        cli.prospects,
        "historial_prospecto",
        lambda uid: (_ for _ in ()).throw(RuntimeError("db caída")),
    )
    report = tmp_path / "informe.md"

    assert cli.main(["research-batch", str(csv), "--salida", str(report)]) == 0

    text = report.read_text()
    assert "Investigados: 1" in text
    assert "Con error: 0" in text
    assert "Con aviso: 1" in text
    assert "db caída" in text

    out = capsys.readouterr().out
    assert "aviso" in out


def fake_history(uid):
    return {"dossier": {"content": make_dossier()}}


def test_batch_pauses_between_rows_but_not_after_the_last(tmp_path, monkeypatch, sleeps):
    csv = write_csv(tmp_path, ["Ada Ruiz,Acme,", "Leo Gil,Beta,", "Eva Sol,Gamma,"])
    monkeypatch.setattr(cli.worker, "research_person", lambda *a, **k: outcome())
    monkeypatch.setattr(cli.prospects, "historial_prospecto", fake_history)
    report = tmp_path / "informe.md"
    cli.main(["research-batch", str(csv), "--salida", str(report), "--pausa", "2.5"])
    assert sleeps == [2.5, 2.5]


def test_batch_pause_defaults_to_fifteen_seconds(tmp_path, monkeypatch, sleeps):
    csv = write_csv(tmp_path, ["Ada Ruiz,Acme,", "Leo Gil,Beta,"])
    monkeypatch.setattr(cli.worker, "research_person", lambda *a, **k: outcome())
    monkeypatch.setattr(cli.prospects, "historial_prospecto", fake_history)
    cli.main(["research-batch", str(csv), "--salida", str(tmp_path / "informe.md")])
    assert sleeps == [15.0]


def degraded_outcome():
    return ResearchOutcome(
        "pid",
        "manual:ada-ruiz-acme",
        "incompleto",
        1,
        Decimal("0.02"),
        "búsqueda degradada: ninguna de las 2 búsquedas devolvió resultados",
        ("linkedin: motores vetados", "prensa: motores vetados"),
    )


def test_single_research_prints_the_errors(capsys, monkeypatch):
    monkeypatch.setattr(cli.worker, "research_person", lambda *a, **k: degraded_outcome())
    monkeypatch.setattr(cli.prospects, "historial_prospecto", fake_history)
    cli.main(["research", "Ada Ruiz", "--empresa", "Acme"])
    out = capsys.readouterr().out
    assert "linkedin: motores vetados" in out
    assert "prensa: motores vetados" in out


def test_the_report_shows_errors_and_marks_degraded_rows(tmp_path, monkeypatch):
    csv = write_csv(tmp_path, ["Ada Ruiz,Acme,", "Leo Gil,Beta,"])
    results = iter([degraded_outcome(), outcome(user="manual:leo-gil-beta")])
    monkeypatch.setattr(cli.worker, "research_person", lambda *a, **k: next(results))
    monkeypatch.setattr(cli.prospects, "historial_prospecto", fake_history)
    report = tmp_path / "informe.md"
    cli.main(["research-batch", str(csv), "--salida", str(report)])
    text = report.read_text()
    assert "Degradados: 1" in text
    assert "incompleto (búsqueda degradada)" in text
    assert "linkedin: motores vetados" in text


@pytest.mark.parametrize(
    "stop",
    [guards.KillSwitchActive("kill_switch is on"), guards.MonthlyBudgetExceeded("spent $150")],
)
def test_single_research_reports_a_system_stop(capsys, monkeypatch, stop):
    def research(*a, **k):
        raise stop

    monkeypatch.setattr(cli.worker, "research_person", research)
    assert cli.main(["research", "Ada Ruiz"]) == 1
    assert f"detenido: {stop}" in capsys.readouterr().out


def test_unknown_openings_show_as_a_dash_and_zero_stays_zero(tmp_path, monkeypatch):
    csv = write_csv(tmp_path, ["Ada Ruiz,Acme,", "Leo Gil,Beta,"])
    empty = {"roles": [], "roles_deslocalizables": []}
    dossiers = {
        "manual:ada-ruiz-acme": make_dossier(
            contratacion={**empty, "vacantes_abiertas": None, "fuentes": []}
        ),
        "manual:leo-gil-beta": make_dossier(
            contratacion={**empty, "vacantes_abiertas": 0, "fuentes": ["https://jobs.example/1"]}
        ),
    }
    results = iter([outcome(), outcome(user="manual:leo-gil-beta")])
    monkeypatch.setattr(cli.worker, "research_person", lambda *a, **k: next(results))
    monkeypatch.setattr(
        cli.prospects, "historial_prospecto", lambda uid: {"dossier": {"content": dossiers[uid]}}
    )
    report = tmp_path / "informe.md"
    cli.main(["research-batch", str(csv), "--salida", str(report)])
    text = report.read_text()
    assert "| Ada Ruiz | Acme | investigado | $0.0500 | 2 | — | 1 |" in text
    assert "| Leo Gil | Beta | investigado | $0.0500 | 2 | 0 | 1 |" in text


def test_costes_prints_the_summary(conn, capsys):
    assert cli.main(["costes", "--dias", "7"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {"days": 7, "total_usd": "0.00", "llm_calls": 0, "cost_events": 0}


def test_the_cli_does_not_load_the_mcp_server():
    code = "import sys, handoff_agent.cli; print('handoff_agent.mcp_server' in sys.modules)"
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert run.stdout.strip() == "False"


def test_a_stop_after_saving_a_dossier_is_told_in_the_report(tmp_path, monkeypatch):
    """Si la parada llega en la segunda pasada, el primer dossier ya se guardó:
    la fila no puede quedarse en un simple "error"."""
    csv = write_csv(tmp_path, ["Ada Ruiz,Acme,", "Leo Gil,Beta,"])

    def research(*a, **k):
        stop = guards.KillSwitchActive("kill_switch is on")
        stop.saved_version = 2
        raise stop

    monkeypatch.setattr(cli.worker, "research_person", research)
    report = tmp_path / "informe.md"
    assert cli.main(["research-batch", str(csv), "--salida", str(report)]) == 1
    text = report.read_text()
    assert "dossier v2 guardado antes de la parada" in text
    assert "Con aviso: 1" in text
    assert "**Lote detenido:** kill_switch is on" in text


def test_a_stop_before_any_save_has_no_warning(tmp_path, monkeypatch):
    csv = write_csv(tmp_path, ["Ada Ruiz,Acme,"])

    def research(*a, **k):
        raise guards.MonthlyBudgetExceeded("spent $150")

    monkeypatch.setattr(cli.worker, "research_person", research)
    report = tmp_path / "informe.md"
    cli.main(["research-batch", str(csv), "--salida", str(report)])
    assert "Con aviso: 0" in report.read_text()


def test_vigilar_without_a_token_explains_what_is_missing(capsys):
    assert cli.main(["vigilar"]) == 1
    assert "SLACK_USER_TOKEN" in capsys.readouterr().out


def test_vigilar_reads_once_and_resolves(conn, monkeypatch, capsys):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C1")
    fake = FakeReader()
    fake.post("C1", "U1", "hola", "9999999999.000001")
    monkeypatch.setattr(cli, "FoundersClubReader", lambda token: fake)
    monkeypatch.setattr(cli, "watch_tick", lambda reader, channels, lookback_hours: cli_tick())
    assert cli.main(["vigilar"]) == 0
    assert "mensajes nuevos" in capsys.readouterr().out


def cli_tick():
    from handoff_agent.ingest.watcher import TickResult

    return TickResult(messages_new=1)


def test_cola_prints_the_queue_counts(conn, capsys):
    from handoff_agent.ingest import queue

    queue.enqueue("U1", "mensaje")
    assert cli.main(["cola"]) == 0
    assert '"pendiente": 1' in capsys.readouterr().out


def test_worker_without_channels_explains_what_is_missing(monkeypatch, capsys):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    assert cli.main(["worker"]) == 1
    assert "SLACK_CHANNEL_IDS" in capsys.readouterr().out


def test_worker_runs_the_loop_with_the_settings(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C1,C2")
    monkeypatch.setenv("SLACK_POLL_SECONDS", "900")
    seen = {}
    monkeypatch.setattr(cli, "FoundersClubReader", lambda token: FakeReader())
    monkeypatch.setattr(cli, "run_loop", lambda reader, **kwargs: seen.update(kwargs) or 0)
    assert cli.main(["worker"]) == 0
    assert seen["channels"] == ["C1", "C2"]
    assert seen["poll_seconds"] == 900


def test_worker_leaves_the_httpx_logger_at_warning(monkeypatch):
    """Carried requirement A: httpx logs full request URLs at INFO, and the
    alert webhook URL is a credential -- it must never reach the logs."""
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C1")
    monkeypatch.setattr(cli, "FoundersClubReader", lambda token: FakeReader())
    monkeypatch.setattr(cli, "run_loop", lambda reader, **kwargs: 0)
    assert cli.main(["worker"]) == 0
    assert logging.getLogger("httpx").level == logging.WARNING


def test_worker_restores_the_previous_signal_handlers_when_the_loop_returns(monkeypatch):
    """Carried requirement C: cmd_worker must not leave SIGINT/SIGTERM handlers
    installed once run_loop returns -- the test suite runs long after this."""
    import signal

    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C1")
    monkeypatch.setattr(cli, "FoundersClubReader", lambda token: FakeReader())
    monkeypatch.setattr(cli, "run_loop", lambda reader, **kwargs: 0)

    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    assert cli.main(["worker"]) == 0
    assert signal.getsignal(signal.SIGINT) == previous_int
    assert signal.getsignal(signal.SIGTERM) == previous_term


def test_worker_restores_the_previous_signal_handlers_when_the_loop_raises(monkeypatch):
    """El finally también tiene que correr cuando run_loop revienta; si no, el
    proceso que siga en marcha se queda con nuestros manejadores puestos."""
    import signal

    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C1")
    monkeypatch.setattr(cli, "FoundersClubReader", lambda token: FakeReader())

    def boom(reader, **kwargs):
        raise RuntimeError("el bucle se rompió")

    monkeypatch.setattr(cli, "run_loop", boom)

    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    with pytest.raises(RuntimeError):
        cli.main(["worker"])
    assert signal.getsignal(signal.SIGINT) == previous_int
    assert signal.getsignal(signal.SIGTERM) == previous_term


def test_digest_prints_the_status_and_exits_zero_on_a_quiet_day(capsys, monkeypatch):
    monkeypatch.setattr(cli.digest, "send_daily", lambda: "nada")
    assert cli.main(["digest"]) == 0
    assert capsys.readouterr().out.strip() == "nada"


@pytest.mark.parametrize("status", ["enviado", "ya_enviado", "sin_configurar", "detenido"])
def test_digest_exits_zero_for_every_status_except_fallido(capsys, monkeypatch, status):
    monkeypatch.setattr(cli.digest, "send_daily", lambda: status)
    assert cli.main(["digest"]) == 0
    assert capsys.readouterr().out.strip() == status


def test_digest_exits_one_on_failure(capsys, monkeypatch):
    monkeypatch.setattr(cli.digest, "send_daily", lambda: "fallido")
    assert cli.main(["digest"]) == 1
    assert capsys.readouterr().out.strip() == "fallido"


def test_cmd_web_serves_uvicorn_with_the_port_from_the_environment(monkeypatch):
    """Railway no expande `$PORT` en un startCommand sin shell (`"handoff
    web"`): el puerto tiene que leerse del entorno en tiempo de ejecución."""
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setenv("PORT", "4321")

    assert cli.main(["web"]) == 0

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("handoff_agent.web.app:app",)
    assert kwargs == {"host": "0.0.0.0", "port": 4321}


def test_cmd_web_defaults_to_port_8000(monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.delenv("PORT", raising=False)

    assert cli.main(["web"]) == 0
    assert calls[0][1]["port"] == 8000


def test_a_stop_signal_wakes_the_worker_from_its_sleep(monkeypatch):
    """Railway manda SIGTERM y, al poco, SIGKILL. Si el worker está en una de
    sus esperas (30 s entre vueltas, 10 min tras una parada del sistema) con un
    sleep que no se entera de la señal, lo matan a mitad en vez de pararse
    solo. La señal tiene que despertarlo."""
    import os
    import signal

    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C1")
    monkeypatch.setattr(cli, "FoundersClubReader", lambda token: FakeReader())
    seen = {}

    def fake_loop(reader, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        started = time.monotonic()
        kwargs["sleep"](5)
        seen["slept"] = time.monotonic() - started
        seen["stopped"] = kwargs["should_stop"]()
        return 0

    monkeypatch.setattr(cli, "run_loop", fake_loop)
    assert cli.main(["worker"]) == 0
    assert seen["stopped"] is True
    assert seen["slept"] < 1
