"""Esquema del radar (0012) y lo que el panel puede hacer con él, probado como
lo ve el panel: rol authenticated y un token con correo."""

import json

import psycopg
import pytest

from handoff_agent import db

ALLOWED = "ana@example.com"
STRANGER = "intruso@example.com"


def as_user(email, sql, params=()):
    with db.transaction() as cur:
        cur.execute("set local role authenticated")
        claims = json.dumps({"email": email}) if email else ""
        cur.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else cur.rowcount


@pytest.fixture
def radar(conn):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))
    account = db.fetch_one(
        "insert into target_accounts (name, domain) values ('Codelco', 'codelco.cl') returning id"
    )["id"]
    signal = db.fetch_one(
        "insert into hiring_signals (account_id, title, title_key, sources) "
        "values (%s, 'Mining Engineer', 'mining engineer', array['linkedin']) returning id",
        (account,),
    )["id"]
    return {"account": account, "signal": signal}


def account(account_id):
    return db.fetch_one("select * from target_accounts where id = %s", (account_id,))


def signal(signal_id):
    return db.fetch_one("select * from hiring_signals where id = %s", (signal_id,))


def config_value(key):
    return db.fetch_one("select value from config where key = %s", (key,))["value"]


# --- esquema -----------------------------------------------------------------


def test_the_defaults_from_the_spec_are_in_config(conn):
    assert config_value("unipile_busquedas_por_dia") == 25
    assert config_value("unipile_perfiles_por_dia") == 40
    assert config_value("unipile_hora_inicio") == 8
    assert config_value("unipile_hora_fin") == 19
    assert config_value("unipile_zona") == "America/New_York"
    assert config_value("unipile_pausado") is False
    assert config_value("radar_horas_entre_escaneos") == 20
    assert config_value("radar_dias_para_cerrar") == 3
    assert config_value("radar_auto_min") == 7
    assert "intern" in config_value("radar_roles_ignorados")


def test_rls_is_on_for_every_new_table(conn):
    rows = db.fetch_all(
        "select relname, relrowsecurity from pg_class where relname in "
        "('target_accounts', 'hiring_signals', 'decision_candidates')"
    )
    assert {r["relname"]: r["relrowsecurity"] for r in rows} == {
        "target_accounts": True,
        "hiring_signals": True,
        "decision_candidates": True,
    }


def test_one_signal_per_account_and_title_key(radar):
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "insert into hiring_signals (account_id, title, title_key) "
            "values (%s, 'Mining  engineer', 'mining engineer')",
            (radar["account"],),
        )


def test_an_unknown_signal_status_is_rejected(radar):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "update hiring_signals set status = 'whatever' where id = %s", (radar["signal"],)
        )


def test_the_score_is_capped_at_ten(radar):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("update hiring_signals set score = 11 where id = %s", (radar["signal"],))


def test_only_one_candidate_can_be_chosen_per_signal(radar):
    db.execute(
        "insert into decision_candidates (signal_id, linkedin_id, chosen) values (%s, 'A', true)",
        (radar["signal"],),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "insert into decision_candidates (signal_id, linkedin_id, chosen) "
            "values (%s, 'B', true)",
            (radar["signal"],),
        )


def test_deleting_an_account_takes_its_signals_and_candidates(radar):
    db.execute(
        "insert into decision_candidates (signal_id, linkedin_id) values (%s, 'A')",
        (radar["signal"],),
    )
    db.execute("delete from target_accounts where id = %s", (radar["account"],))
    assert db.fetch_one("select count(*) as n from hiring_signals")["n"] == 0
    assert db.fetch_one("select count(*) as n from decision_candidates")["n"] == 0


# --- lectura -----------------------------------------------------------------


@pytest.mark.parametrize("table", ["target_accounts", "hiring_signals", "decision_candidates"])
def test_an_allowed_user_reads_and_a_stranger_does_not(radar, table):
    db.execute(
        "insert into decision_candidates (signal_id, linkedin_id) values (%s, 'A')",
        (radar["signal"],),
    )
    assert len(as_user(ALLOWED, f"select * from {table}")) == 1
    assert as_user(STRANGER, f"select * from {table}") == []
    assert as_user(None, f"select * from {table}") == []


@pytest.mark.parametrize(
    "sql",
    [
        "insert into target_accounts (name) values ('Otra')",
        "update target_accounts set status = 'paused'",
        "delete from target_accounts",
        "update hiring_signals set status = 'pursued'",
        "delete from hiring_signals",
        "update decision_candidates set chosen = true",
    ],
)
def test_nobody_writes_the_new_tables_directly(radar, sql):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(ALLOWED, sql)


# --- config ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("radar_auto_min", 8),
        ("radar_horas_entre_escaneos", 12),
        ("unipile_busquedas_por_dia", 10),
        ("unipile_perfiles_por_dia", 20),
        ("unipile_pausado", False),
    ],
)
def test_the_panel_can_move_the_radar_knobs(restore_config, key, value):
    db.execute("update config set value = 'true'::jsonb where key = 'unipile_pausado'")
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))
    as_user(ALLOWED, "update config set value = %s::jsonb where key = %s", (json.dumps(value), key))
    assert config_value(key) == value


@pytest.mark.parametrize(
    "key", ["unipile_hora_inicio", "unipile_zona", "radar_roles_ignorados", "kill_switch"]
)
def test_the_panel_cannot_move_the_other_radar_keys(restore_config, key):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))
    before = config_value(key)
    as_user(ALLOWED, "update config set value = '1'::jsonb where key = %s", (key,))
    assert config_value(key) == before


def test_the_old_thresholds_still_move(restore_config):
    """La política nueva se suma a la de 0006, no la sustituye."""
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))
    as_user(ALLOWED, "update config set value = '3'::jsonb where key = 'banda_media_min'")
    assert config_value("banda_media_min") == 3


def test_a_stranger_cannot_resume_unipile(restore_config):
    db.execute("update config set value = 'true'::jsonb where key = 'unipile_pausado'")
    as_user(STRANGER, "update config set value = 'false'::jsonb where key = 'unipile_pausado'")
    assert config_value("unipile_pausado") is True


# --- panel_agregar_cuenta ----------------------------------------------------


def agregar(user, nombre, dominio=None, linkedin_id=None):
    rows = as_user(
        user,
        "select public.panel_agregar_cuenta(%s, %s, %s) as id",
        (nombre, dominio, linkedin_id),
    )
    return rows[0]["id"]


def test_an_allowed_user_adds_an_account_with_a_normalised_domain(conn):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))
    new_id = agregar(ALLOWED, "  Acme  ", "HTTPS://www.Acme.com/", "1234")
    row = account(new_id)
    assert (row["name"], row["domain"], row["linkedin_company_id"]) == ("Acme", "acme.com", "1234")
    assert (row["source"], row["status"]) == ("manual", "watching")


def test_adding_the_same_domain_twice_updates_the_account(conn):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))
    first = agregar(ALLOWED, "Acme", "acme.com")
    second = agregar(ALLOWED, "Acme Corp", "http://acme.com", "999")
    assert first == second
    row = account(first)
    assert (row["name"], row["linkedin_company_id"]) == ("Acme Corp", "999")
    assert db.fetch_one("select count(*) as n from target_accounts")["n"] == 1


def test_readding_without_a_linkedin_id_keeps_the_resolved_one(conn):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))
    first = agregar(ALLOWED, "Acme", "acme.com", "999")
    agregar(ALLOWED, "Acme", "acme.com")
    assert account(first)["linkedin_company_id"] == "999"


def test_an_account_without_a_domain_is_deduped_by_name(conn):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))
    first = agregar(ALLOWED, "Codelco")
    assert agregar(ALLOWED, "codelco") == first


@pytest.mark.parametrize(
    ("nombre", "dominio", "linkedin_id", "message"),
    [
        ("  ", "acme.com", None, "name is required"),
        (None, "acme.com", None, "name is required"),
        ("Acme", "not a domain", None, "invalid domain"),
        ("Acme", "acme.com", "abc", "invalid LinkedIn company id"),
    ],
)
def test_bad_input_is_rejected_in_english(conn, nombre, dominio, linkedin_id, message):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))
    with pytest.raises(psycopg.errors.RaiseException, match=message):
        agregar(ALLOWED, nombre, dominio, linkedin_id)


@pytest.mark.parametrize("user", [STRANGER, None])
def test_only_panel_users_add_accounts(conn, user):
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        agregar(user, "Acme", "acme.com")
    assert db.fetch_one("select count(*) as n from target_accounts")["n"] == 0


# --- panel_estado_cuenta / panel_escanear_cuenta -------------------------------


def test_an_allowed_user_pauses_and_resumes_an_account(radar):
    as_user(ALLOWED, "select public.panel_estado_cuenta(%s, 'paused')", (radar["account"],))
    assert account(radar["account"])["status"] == "paused"
    as_user(ALLOWED, "select public.panel_estado_cuenta(%s, 'watching')", (radar["account"],))
    assert account(radar["account"])["status"] == "watching"


def test_an_unknown_account_status_is_rejected(radar):
    with pytest.raises(psycopg.errors.RaiseException, match="invalid status"):
        as_user(ALLOWED, "select public.panel_estado_cuenta(%s, 'deleted')", (radar["account"],))


def test_scan_now_clears_the_last_scan(radar):
    db.execute("update target_accounts set last_scan_at = now() where id = %s", (radar["account"],))
    as_user(ALLOWED, "select public.panel_escanear_cuenta(%s)", (radar["account"],))
    assert account(radar["account"])["last_scan_at"] is None


def test_a_missing_account_is_reported(radar):
    with pytest.raises(psycopg.errors.RaiseException, match="account .* not found"):
        as_user(
            ALLOWED,
            "select public.panel_escanear_cuenta('00000000-0000-0000-0000-000000000000')",
        )


@pytest.mark.parametrize(
    "sql",
    [
        "select public.panel_estado_cuenta(%s, 'paused')",
        "select public.panel_escanear_cuenta(%s)",
    ],
)
def test_a_stranger_cannot_touch_an_account(radar, sql):
    db.execute("update target_accounts set last_scan_at = now() where id = %s", (radar["account"],))
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        as_user(STRANGER, sql, (radar["account"],))
    row = account(radar["account"])
    assert row["status"] == "watching" and row["last_scan_at"] is not None


# --- panel_accion_senal --------------------------------------------------------


def accion(user, signal_id, action):
    as_user(user, "select public.panel_accion_senal(%s, %s)", (signal_id, action))


def test_pursue_dismiss_and_snooze(radar):
    accion(ALLOWED, radar["signal"], "snooze")
    row = signal(radar["signal"])
    assert row["status"] == "snoozed"
    until = db.fetch_one(
        "select snoozed_until - now() as left from hiring_signals where id = %s",
        (radar["signal"],),
    )["left"]
    assert 6.9 < until.total_seconds() / 86400 <= 7

    accion(ALLOWED, radar["signal"], "pursue")
    row = signal(radar["signal"])
    assert (row["status"], row["snoozed_until"]) == ("pursued", None)

    accion(ALLOWED, radar["signal"], "dismiss")
    assert signal(radar["signal"])["status"] == "dismissed"

    accion(ALLOWED, radar["signal"], "pursue")
    assert signal(radar["signal"])["status"] == "pursued"


@pytest.mark.parametrize("status", ["researching", "ready", "task_created", "pursued"])
def test_pursue_only_from_new_snoozed_or_dismissed(radar, status):
    db.execute("update hiring_signals set status = %s where id = %s", (status, radar["signal"]))
    with pytest.raises(psycopg.errors.RaiseException, match="cannot be pursued"):
        accion(ALLOWED, radar["signal"], "pursue")
    assert signal(radar["signal"])["status"] == status


def test_an_unknown_action_is_rejected(radar):
    with pytest.raises(psycopg.errors.RaiseException, match="invalid action"):
        accion(ALLOWED, radar["signal"], "delete")


def test_a_stranger_cannot_act_on_a_signal(radar):
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        accion(STRANGER, radar["signal"], "pursue")
    assert signal(radar["signal"])["status"] == "new"


# --- panel_elegir_candidato ----------------------------------------------------


def test_choosing_a_candidate_unchooses_the_others(radar):
    ids = [
        db.fetch_one(
            "insert into decision_candidates (signal_id, linkedin_id, chosen) "
            "values (%s, %s, %s) returning id",
            (radar["signal"], linkedin_id, chosen),
        )["id"]
        for linkedin_id, chosen in [("A", True), ("B", False)]
    ]
    as_user(ALLOWED, "select public.panel_elegir_candidato(%s)", (ids[1],))
    rows = db.fetch_all("select linkedin_id, chosen from decision_candidates order by linkedin_id")
    assert [(r["linkedin_id"], r["chosen"]) for r in rows] == [("A", False), ("B", True)]


def test_a_stranger_cannot_choose_a_candidate(radar):
    candidate = db.fetch_one(
        "insert into decision_candidates (signal_id, linkedin_id) values (%s, 'A') returning id",
        (radar["signal"],),
    )["id"]
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        as_user(STRANGER, "select public.panel_elegir_candidato(%s)", (candidate,))
    assert db.fetch_one("select chosen from decision_candidates")["chosen"] is False


def test_anon_cannot_execute_the_panel_functions(radar):
    rows = db.fetch_all(
        "select p.proname, has_function_privilege('anon', p.oid, 'execute') as anon_ok, "
        "has_function_privilege('authenticated', p.oid, 'execute') as auth_ok "
        "from pg_proc p where p.proname in ('panel_agregar_cuenta', 'panel_estado_cuenta', "
        "'panel_escanear_cuenta', 'panel_accion_senal', 'panel_elegir_candidato')"
    )
    assert len(rows) == 5
    assert all(not r["anon_ok"] and r["auth_ok"] for r in rows)
