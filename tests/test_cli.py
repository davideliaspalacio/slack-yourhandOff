from decimal import Decimal

from handoff_agent import cli, guards
from handoff_agent.research.worker import ResearchOutcome
from tests.test_dossier import make_dossier


def outcome(status="investigado", cost="0.05", user="manual:ada-ruiz-acme"):
    return ResearchOutcome(
        "pid", user, status, 1 if status == "investigado" else None, Decimal(cost), None
    )


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
