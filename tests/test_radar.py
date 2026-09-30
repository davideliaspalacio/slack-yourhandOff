"""Radar de cuentas objetivo (spec §5). JobSpy y Unipile, siempre sustituidos."""

import json

import pandas as pd
import pytest

from handoff_agent import db, guards
from handoff_agent.accounts import radar, repo
from handoff_agent.tools import jobs, unipile

# --- funciones puras ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "key"),
    [
        ("Mining Engineer (m/f/d)", "mining engineer"),
        ("Senior Analyst [w/m/d]", "senior analyst"),
        ("Ingeniero de Minas - Remote", "ingeniero de minas"),
        ("Operator – Remote", "operator"),
        ("Data Engineer (Hybrid)", "data engineer"),
        ("  Jefe   de  Operaciones ", "jefe de operaciones"),
        ("Técnico Eléctrico", "tecnico electrico"),
        ("Sales Rep (all genders)", "sales rep"),
        ("Ops Lead | Remoto", "ops lead"),
        ("C++ Developer", "c++ developer"),
    ],
)
def test_normalizar_titulo(title, key):
    assert radar.normalizar_titulo(title) == key


def test_two_spellings_of_the_same_role_share_a_key():
    assert radar.normalizar_titulo("Mining Engineer (M/F/D)") == radar.normalizar_titulo(
        "mining  engineer - remote"
    )


@pytest.mark.parametrize(
    ("title", "ignored"),
    [
        ("Summer Intern", True),
        ("Internship - Finance", True),
        ("Práctica Profesional Ingeniería", True),
        ("Becario de Marketing", True),
        ("Internal Auditor", False),
        ("International Sales Manager", False),
        ("Mining Engineer", False),
    ],
)
def test_es_rol_ignorado(title, ignored):
    roles = ["intern", "internship", "práctica", "becario"]
    assert radar.es_rol_ignorado(radar.normalizar_titulo(title), roles) is ignored


@pytest.mark.parametrize(
    ("kwargs", "score"),
    [
        ({}, 3),
        ({"dias_abierta": 29}, 3),
        ({"dias_abierta": 30}, 5),
        ({"reposted": True}, 5),
        ({"abiertas_cuenta_30d": 2}, 3),
        ({"abiertas_cuenta_30d": 3}, 5),
        ({"num_fuentes": 2}, 4),
        (
            {"dias_abierta": 45, "reposted": True, "abiertas_cuenta_30d": 5, "num_fuentes": 3},
            10,
        ),
    ],
)
def test_calcular_score(kwargs, score):
    base = {"dias_abierta": 0, "reposted": False, "abiertas_cuenta_30d": 1, "num_fuentes": 1}
    assert radar.calcular_score(**{**base, **kwargs}) == score


@pytest.mark.parametrize(
    ("nombre", "candidatos", "expected"),
    [
        ("Codelco", [("16300", "CODELCO – Corporación Nacional del Cobre")], "16300"),
        ("Acme, Inc.", [("1", "Acme")], "1"),
        ("Nestlé", [("2", "Nestle")], "2"),
        ("Meta", [("3", "Metabase")], None),
        ("Acme", [("4", "Acme Rival"), ("5", "Acme (Chile)")], "5"),
        ("Acme", [], None),
    ],
)
def test_elegir_empresa(nombre, candidatos, expected):
    chosen = radar.elegir_empresa(nombre, candidatos)
    assert (chosen[0] if chosen else None) == expected


# --- escaneo -------------------------------------------------------------------------


def row(title, company, url="https://jobs.example/1", location="Santiago"):
    return {"title": title, "company": company, "location": location, "job_url": url}


class Boards:
    """JobSpy falso: qué devuelve cada board, y qué se le pidió."""

    def __init__(self, linkedin=(), indeed=(), fail=()):
        self.rows = {"linkedin": list(linkedin), "indeed": list(indeed)}
        self.fail = set(fail)
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        site = kw["site_name"][0]
        if site in self.fail:
            raise RuntimeError(f"{site} blocked us")
        return pd.DataFrame(self.rows[site])


@pytest.fixture
def boards(monkeypatch, restore_config):
    fake = Boards()
    monkeypatch.setattr(jobs, "_scrape", fake)
    # Por defecto Unipile no está configurado: el radar sigue por nombre.
    monkeypatch.setattr(
        radar.unipile,
        "resolver_empresa",
        lambda nombre: (_ for _ in ()).throw(unipile.UnipileNoConfigurado("sin claves")),
    )
    return fake


def account(nombre="Codelco", dominio="codelco.cl", linkedin_id=None):
    return repo.agregar_cuenta(nombre, dominio, linkedin_id)


def reload(cuenta):
    return repo.obtener_cuenta(cuenta["id"])


def signals(cuenta):
    return {
        s["title_key"]: s
        for s in db.fetch_all("select * from hiring_signals where account_id = %s", (cuenta["id"],))
    }


def set_config(key, value):
    db.execute("update config set value = %s::jsonb where key = %s", (json.dumps(value), key))


def test_a_scan_creates_one_signal_per_role_and_merges_sources(boards):
    cuenta = account(linkedin_id="16300")
    boards.rows["linkedin"] = [
        row("Mining Engineer (m/f/d)", "CODELCO", url="https://li/1"),
        row("Geólogo", "CODELCO", url="https://li/2"),
    ]
    boards.rows["indeed"] = [
        row("Mining Engineer", "Codelco", url="https://in/1"),
        row("Driver", "Otra Minera", url="https://in/2"),
    ]
    result = radar.escanear_cuenta(cuenta)

    found = signals(cuenta)
    assert set(found) == {"mining engineer", "geologo"}
    engineer = found["mining engineer"]
    assert sorted(engineer["sources"]) == ["indeed", "linkedin"]
    assert sorted(engineer["urls"]) == ["https://in/1", "https://li/1"]
    assert engineer["status"] == "new"
    assert engineer["score"] == 4  # nueva (3) + dos fuentes (1)
    assert found["geologo"]["score"] == 3
    assert (result.nuevas, result.vistas, result.error) == (2, 2, None)
    after = reload(cuenta)
    assert after["last_scan_at"] is not None and after["last_scan_error"] is None
    linkedin_call = next(c for c in boards.calls if c["site_name"] == ["linkedin"])
    assert linkedin_call["linkedin_company_ids"] == [16300]


def test_ignored_roles_never_become_signals(boards):
    cuenta = account()
    boards.rows["linkedin"] = [row("Summer Intern", "Codelco"), row("Becario Minas", "Codelco")]
    result = radar.escanear_cuenta(cuenta)
    assert signals(cuenta) == {}
    assert result.ignoradas == 2


def test_a_second_scan_updates_instead_of_duplicating(boards):
    cuenta = account()
    boards.rows["linkedin"] = [row("Geólogo", "Codelco")]
    radar.escanear_cuenta(cuenta)
    db.execute("update hiring_signals set last_seen_at = now() - interval '1 day'")
    result = radar.escanear_cuenta(cuenta)
    found = signals(cuenta)
    assert len(found) == 1
    assert (result.nuevas, result.vistas) == (0, 1)
    age = db.fetch_one("select now() - last_seen_at as age from hiring_signals")["age"]
    assert age.total_seconds() < 60


def test_roles_not_seen_for_three_days_are_closed(boards):
    cuenta = account()
    boards.rows["linkedin"] = [row("Geólogo", "Codelco"), row("Chofer", "Codelco")]
    radar.escanear_cuenta(cuenta)
    db.execute(
        "update hiring_signals set last_seen_at = now() - interval '4 days' "
        "where title_key = 'chofer'"
    )
    boards.rows["linkedin"] = [row("Geólogo", "Codelco")]
    result = radar.escanear_cuenta(cuenta)
    found = signals(cuenta)
    assert found["chofer"]["closed_at"] is not None
    assert found["geologo"]["closed_at"] is None
    assert result.cerradas == 1


def test_a_role_missing_for_less_than_three_days_stays_open(boards):
    cuenta = account()
    boards.rows["linkedin"] = [row("Chofer", "Codelco")]
    radar.escanear_cuenta(cuenta)
    db.execute("update hiring_signals set last_seen_at = now() - interval '2 days'")
    boards.rows["linkedin"] = []
    radar.escanear_cuenta(cuenta)
    assert signals(cuenta)["chofer"]["closed_at"] is None


def test_nothing_is_closed_when_a_board_failed(boards):
    """Si Indeed falló, no saber de una vacante no significa que se cerró."""
    cuenta = account()
    boards.rows["linkedin"] = [row("Chofer", "Codelco")]
    radar.escanear_cuenta(cuenta)
    db.execute("update hiring_signals set last_seen_at = now() - interval '5 days'")
    boards.rows["linkedin"] = []
    boards.fail = {"indeed"}
    result = radar.escanear_cuenta(cuenta)
    assert signals(cuenta)["chofer"]["closed_at"] is None
    assert result.cerradas == 0
    assert "indeed" in reload(cuenta)["last_scan_error"]


def test_a_closed_role_that_comes_back_is_a_repost(boards):
    cuenta = account()
    boards.rows["linkedin"] = [row("Chofer", "Codelco")]
    radar.escanear_cuenta(cuenta)
    db.execute("update hiring_signals set closed_at = now() - interval '1 day'")
    result = radar.escanear_cuenta(cuenta)
    chofer = signals(cuenta)["chofer"]
    assert chofer["reposted"] is True and chofer["closed_at"] is None
    assert chofer["score"] == 5  # nueva (3) + re-publicada (2)
    assert result.reabiertas == 1


def test_the_score_grows_with_age_and_with_many_openings(boards):
    cuenta = account()
    boards.rows["linkedin"] = [
        row("Geólogo", "Codelco"),
        row("Chofer", "Codelco"),
        row("Mecánico", "Codelco"),
    ]
    radar.escanear_cuenta(cuenta)
    db.execute(
        "update hiring_signals set first_seen_at = now() - interval '31 days' "
        "where title_key = 'geologo'"
    )
    radar.escanear_cuenta(cuenta)
    found = signals(cuenta)
    assert found["chofer"]["score"] == 5  # nueva (3) + tres abiertas en 30 días (2)
    assert found["geologo"]["score"] == 7  # + abierta 30 días o más (2)


def test_a_strong_signal_is_pursued_on_its_own(boards):
    cuenta = account()
    boards.rows["linkedin"] = [row("Geólogo", "Codelco"), row("Chofer", "Codelco")]
    boards.rows["indeed"] = [row("Geólogo", "Codelco")]
    set_config("radar_auto_min", 4)
    result = radar.escanear_cuenta(cuenta)
    found = signals(cuenta)
    assert found["geologo"]["status"] == "pursued"
    assert found["chofer"]["status"] == "new"
    assert result.auto_pursued == 1


def test_auto_pursue_never_revives_a_dismissed_signal(boards):
    cuenta = account()
    boards.rows["linkedin"] = [row("Geólogo", "Codelco")]
    radar.escanear_cuenta(cuenta)
    db.execute("update hiring_signals set status = 'dismissed'")
    set_config("radar_auto_min", 1)
    radar.escanear_cuenta(cuenta)
    assert signals(cuenta)["geologo"]["status"] == "dismissed"


def test_when_every_board_fails_the_error_is_kept_and_the_scan_is_stamped(boards):
    cuenta = account()
    boards.fail = {"linkedin", "indeed"}
    result = radar.escanear_cuenta(cuenta)
    after = reload(cuenta)
    assert "linkedin" in after["last_scan_error"] and "indeed" in after["last_scan_error"]
    assert after["last_scan_at"] is not None
    assert result.error == after["last_scan_error"]


def test_every_scan_is_in_the_ledger(boards):
    cuenta = account()
    boards.rows["linkedin"] = [row("Geólogo", "Codelco")]
    radar.escanear_cuenta(cuenta)
    logged = db.fetch_one(
        "select payload, result from agent_actions where action = 'radar_escaneo'"
    )
    assert logged["payload"]["account_id"] == str(cuenta["id"])
    assert logged["result"]["nuevas"] == 1


# --- resolución del id de LinkedIn ------------------------------------------------------


def test_a_missing_linkedin_id_is_resolved_once_and_used(boards, monkeypatch):
    asked = []

    def resolver(nombre):
        asked.append(nombre)
        return [("99", "Codelco Rival"), ("16300", "CODELCO – Corporación Nacional del Cobre")]

    monkeypatch.setattr(radar.unipile, "resolver_empresa", resolver)
    cuenta = account()
    radar.escanear_cuenta(cuenta)
    after = reload(cuenta)
    assert after["linkedin_company_id"] == "16300"
    assert after["linkedin_name"] == "CODELCO – Corporación Nacional del Cobre"
    linkedin_call = next(c for c in boards.calls if c["site_name"] == ["linkedin"])
    assert linkedin_call["linkedin_company_ids"] == [16300]
    radar.escanear_cuenta(after)
    assert asked == ["Codelco"]


def test_without_a_match_the_account_is_scanned_by_name_and_not_asked_again(boards, monkeypatch):
    asked = []
    monkeypatch.setattr(
        radar.unipile, "resolver_empresa", lambda n: asked.append(n) or [("1", "Otra")]
    )
    cuenta = account()
    radar.escanear_cuenta(cuenta)
    radar.escanear_cuenta(reload(cuenta))
    assert reload(cuenta)["linkedin_company_id"] is None
    assert asked == ["Codelco"]
    assert all(c.get("search_term") == "Codelco" for c in boards.calls)


@pytest.mark.parametrize(
    "exc",
    [
        unipile.UnipileFueraDeHorario("sábado"),
        unipile.UnipileTopeDiario("25 de 25"),
        unipile.UnipilePausado("pausado"),
        unipile.UnipileNoConfigurado("sin claves"),
    ],
)
def test_a_unipile_guardrail_is_not_a_scan_error(boards, monkeypatch, exc):
    def resolver(nombre):
        raise exc

    monkeypatch.setattr(radar.unipile, "resolver_empresa", resolver)
    cuenta = account()
    boards.rows["linkedin"] = [row("Geólogo", "Codelco")]
    result = radar.escanear_cuenta(cuenta)
    assert result.error is None
    assert reload(cuenta)["last_scan_error"] is None
    assert set(signals(cuenta)) == {"geologo"}


def test_a_guardrail_does_not_use_up_the_single_attempt(boards, monkeypatch):
    calls = []

    def resolver(nombre):
        calls.append(nombre)
        if len(calls) == 1:
            raise unipile.UnipileFueraDeHorario("de noche")
        return [("16300", "Codelco")]

    monkeypatch.setattr(radar.unipile, "resolver_empresa", resolver)
    cuenta = account()
    radar.escanear_cuenta(cuenta)
    radar.escanear_cuenta(reload(cuenta))
    assert reload(cuenta)["linkedin_company_id"] == "16300"


# --- escanear_pendientes ------------------------------------------------------------


def test_only_watched_accounts_that_are_due_get_scanned(boards):
    due = account("Due", "due.com")
    recent = account("Recent", "recent.com")
    paused = account("Paused", "paused.com")
    db.execute("update target_accounts set last_scan_at = now() where id = %s", (recent["id"],))
    db.execute("update target_accounts set status = 'paused' where id = %s", (paused["id"],))
    results = radar.escanear_pendientes()
    assert [r.account_id for r in results] == [str(due["id"])]


def test_an_account_scanned_long_ago_is_due_again(boards):
    old = account()
    db.execute("update target_accounts set last_scan_at = now() - interval '21 hours'")
    assert [r.account_id for r in radar.escanear_pendientes()] == [str(old["id"])]


def test_forzar_scans_every_watched_account(boards):
    account("A", "a.com")
    account("B", "b.com")
    db.execute("update target_accounts set last_scan_at = now()")
    assert len(radar.escanear_pendientes(forzar=True)) == 2


def test_the_batch_is_capped(boards):
    for i in range(4):
        account(f"C{i}", f"c{i}.com")
    assert len(radar.escanear_pendientes(limite=3)) == 3
    assert len(radar.escanear_pendientes(limite=3)) == 1


def test_one_account_failing_does_not_stop_the_others(boards, monkeypatch):
    account("A", "a.com")
    account("B", "b.com")
    real = radar._escanear

    def flaky(cuenta):
        if cuenta["name"] == "A":
            raise RuntimeError("algo raro")
        return real(cuenta)

    monkeypatch.setattr(radar, "_escanear", flaky)
    results = {r.nombre: r for r in radar.escanear_pendientes()}
    assert results["A"].error == "RuntimeError: algo raro"
    assert results["B"].error is None
    errors = {
        r["name"]: r["last_scan_error"]
        for r in db.fetch_all("select name, last_scan_error from target_accounts")
    }
    assert errors == {"A": "RuntimeError: algo raro", "B": None}


def test_the_kill_switch_stops_the_radar(boards):
    account()
    set_config("kill_switch", True)
    with pytest.raises(guards.KillSwitchActive):
        radar.escanear_pendientes()
    assert boards.calls == []


def test_expired_snoozes_come_back_as_new(boards):
    cuenta = account()
    db.execute("update target_accounts set last_scan_at = now()")
    db.execute(
        "insert into hiring_signals (account_id, title, title_key, status, snoozed_until) values "
        "(%s, 'A', 'a', 'snoozed', now() - interval '1 minute'), "
        "(%s, 'B', 'b', 'snoozed', now() + interval '1 day')",
        (cuenta["id"], cuenta["id"]),
    )
    radar.escanear_pendientes()
    found = signals(cuenta)
    assert (found["a"]["status"], found["a"]["snoozed_until"]) == ("new", None)
    assert found["b"]["status"] == "snoozed"


def test_escanear_por_id_scans_one_account_even_if_recent(boards):
    cuenta = account()
    db.execute("update target_accounts set last_scan_at = now() - interval '1 hour'")
    boards.rows["linkedin"] = [row("Geólogo", "Codelco")]
    result = radar.escanear_por_id(cuenta["id"])
    assert result.nuevas == 1
    with pytest.raises(LookupError):
        radar.escanear_por_id("00000000-0000-0000-0000-000000000000")
