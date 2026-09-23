"""Los subcomandos del radar: cuentas, radar y unipile."""

import pytest

from handoff_agent import cli, db, guards
from handoff_agent.accounts import radar, repo
from handoff_agent.accounts.radar import ResultadoEscaneo
from handoff_agent.tools import unipile


def test_cuentas_agregar_adds_an_account(conn, capsys):
    assert cli.main(["cuentas", "agregar", "Codelco", "--dominio", "https://www.codelco.cl/"]) == 0
    row = db.fetch_one("select name, domain, source from target_accounts")
    assert (row["name"], row["domain"], row["source"]) == ("Codelco", "codelco.cl", "manual")
    assert "codelco.cl" in capsys.readouterr().out


def test_cuentas_agregar_with_a_linkedin_id(conn):
    assert cli.main(["cuentas", "agregar", "Codelco", "--linkedin-id", "16300"]) == 0
    assert (
        db.fetch_one("select linkedin_company_id from target_accounts")["linkedin_company_id"]
        == "16300"
    )


def test_cuentas_agregar_rejects_a_bad_domain(conn, capsys):
    assert cli.main(["cuentas", "agregar", "Acme", "--dominio", "no es un dominio"]) == 1
    assert "dominio inválido" in capsys.readouterr().out
    assert db.fetch_one("select count(*) as n from target_accounts")["n"] == 0


def test_cuentas_importar_reads_a_csv(conn, tmp_path, capsys):
    path = tmp_path / "cuentas.csv"
    path.write_text("nombre,dominio,linkedin_id\nCodelco,codelco.cl,16300\n,x.com,\n")
    assert cli.main(["cuentas", "importar", str(path)]) == 1
    out = capsys.readouterr().out
    assert "agregadas: 1" in out
    assert "línea 3" in out
    assert db.fetch_one("select source from target_accounts")["source"] == "csv"


def test_cuentas_importar_a_clean_csv_exits_zero(conn, tmp_path):
    path = tmp_path / "cuentas.csv"
    path.write_text("nombre,dominio\nAcme,acme.com\n")
    assert cli.main(["cuentas", "importar", str(path)]) == 0


def test_cuentas_importar_without_the_nombre_column(conn, tmp_path, capsys):
    path = tmp_path / "cuentas.csv"
    path.write_text("empresa\nAcme\n")
    assert cli.main(["cuentas", "importar", str(path)]) == 1
    assert "nombre" in capsys.readouterr().out


def test_cuentas_listar_shows_roles_and_errors(conn, capsys):
    acme = repo.agregar_cuenta("Acme", "acme.com")
    repo.agregar_cuenta("Beta")
    db.execute(
        "insert into hiring_signals (account_id, title, title_key, score) "
        "values (%s, 'Ops', 'ops', 6)",
        (acme["id"],),
    )
    db.execute("update target_accounts set last_scan_error = 'indeed: blocked' where name = 'Beta'")
    assert cli.main(["cuentas", "listar"]) == 0
    out = capsys.readouterr().out
    assert "Acme" in out and "acme.com" in out and "1 abiertas" in out and "score 6" in out
    assert "indeed: blocked" in out


def test_cuentas_listar_when_empty(conn, capsys):
    assert cli.main(["cuentas", "listar"]) == 0
    assert "no hay cuentas" in capsys.readouterr().out


def test_radar_scans_the_pending_accounts(conn, monkeypatch, capsys):
    seen = {}

    def pendientes(forzar=False, limite=5):
        seen.update(forzar=forzar, limite=limite)
        return [ResultadoEscaneo("id-1", "Codelco", vistas=3, nuevas=2, auto_pursued=1)]

    monkeypatch.setattr(radar, "escanear_pendientes", pendientes)
    assert cli.main(["radar", "--forzar"]) == 0
    assert seen["forzar"] is True
    assert seen["limite"] > 1000  # desde la terminal, todas las que toquen
    out = capsys.readouterr().out
    assert "Codelco" in out and "2 nuevas" in out and "1 auto-pursued" in out


def test_radar_with_nothing_to_do(conn, monkeypatch, capsys):
    monkeypatch.setattr(radar, "escanear_pendientes", lambda **kw: [])
    assert cli.main(["radar"]) == 0
    assert "ninguna cuenta" in capsys.readouterr().out


def test_radar_one_account(conn, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(
        radar,
        "escanear_por_id",
        lambda account_id: seen.append(account_id) or ResultadoEscaneo(account_id, "Acme"),
    )
    uid = "7d9f1c2e-0000-4000-8000-000000000001"
    assert cli.main(["radar", "--cuenta", uid]) == 0
    assert seen == [uid]


def test_radar_reports_an_account_error_with_exit_one(conn, monkeypatch, capsys):
    monkeypatch.setattr(
        radar,
        "escanear_pendientes",
        lambda **kw: [ResultadoEscaneo("id-1", "Acme", error="linkedin: blocked")],
    )
    assert cli.main(["radar"]) == 1
    assert "linkedin: blocked" in capsys.readouterr().out


def test_radar_rejects_an_unknown_account(conn, capsys):
    assert cli.main(["radar", "--cuenta", "not-a-uuid"]) == 1
    assert cli.main(["radar", "--cuenta", "7d9f1c2e-0000-4000-8000-000000000001"]) == 1
    assert "no existe" in capsys.readouterr().out


def test_radar_stops_at_the_kill_switch(conn, monkeypatch, capsys):
    def stopped(**kw):
        raise guards.KillSwitchActive("kill_switch is on")

    monkeypatch.setattr(radar, "escanear_pendientes", stopped)
    assert cli.main(["radar"]) == 1
    assert "detenido" in capsys.readouterr().out


def test_unipile_estado_without_credentials(conn, capsys):
    assert cli.main(["unipile", "estado"]) == 1
    assert "UNIPILE_API_KEY" in capsys.readouterr().out


@pytest.fixture
def unipile_env(monkeypatch):
    monkeypatch.setenv("UNIPILE_API_KEY", "k")
    monkeypatch.setenv("UNIPILE_DSN", "api4.unipile.com:13460")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "acc-1")


USO = {
    "busquedas_hoy": 3,
    "tope_busquedas": 25,
    "perfiles_hoy": 0,
    "tope_perfiles": 40,
    "en_horario": False,
    "franja": "lun-vie 8:00-19:00",
    "zona": "America/New_York",
    "ahora": "2026-09-26T11:00-04:00",
    "pausado": False,
}


def test_unipile_estado_prints_health_usage_and_hours(conn, unipile_env, monkeypatch, capsys):
    monkeypatch.setattr(unipile, "estado_cuenta", lambda: True)
    monkeypatch.setattr(unipile, "resumen_uso", lambda: USO)
    assert cli.main(["unipile", "estado"]) == 0
    out = capsys.readouterr().out
    assert "cuenta: OK" in out
    assert "búsquedas hoy: 3/25" in out
    assert "perfiles hoy: 0/40" in out
    assert "fuera de horario" in out


def test_unipile_estado_when_the_account_is_down(conn, unipile_env, monkeypatch, capsys):
    monkeypatch.setattr(unipile, "estado_cuenta", lambda: False)
    monkeypatch.setattr(unipile, "resumen_uso", lambda: {**USO, "pausado": True})
    assert cli.main(["unipile", "estado"]) == 1
    out = capsys.readouterr().out
    assert "NO está OK" in out and "pausado: sí" in out


def test_unipile_estado_when_unipile_does_not_answer(conn, unipile_env, monkeypatch, capsys):
    def down():
        raise unipile.UnipileError("Unipile no responde: ConnectError")

    monkeypatch.setattr(unipile, "estado_cuenta", down)
    monkeypatch.setattr(unipile, "resumen_uso", lambda: USO)
    assert cli.main(["unipile", "estado"]) == 1
    assert "ConnectError" in capsys.readouterr().out
