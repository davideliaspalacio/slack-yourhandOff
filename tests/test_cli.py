import time
from decimal import Decimal

import pytest

from handoff_agent import cli, guards
from handoff_agent.research.worker import ResearchOutcome
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
