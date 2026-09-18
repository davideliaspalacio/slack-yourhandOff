"""Las políticas del panel, probadas como las ve el panel: con el rol
authenticated y un token con correo, no como superusuario, que se las salta."""

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
def people(conn):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))
    db.execute("insert into prospects (slack_user_id, full_name) values ('U1', 'Ada')")
    db.execute(
        "insert into llm_calls (stage, model, input_tokens, cost_usd) values ('s', 'm', 1, 0.5)"
    )


def state_of(slack_user_id):
    return db.fetch_one("select state from prospects where slack_user_id = %s", (slack_user_id,))[
        "state"
    ]


def test_an_allowed_user_reads_people(people):
    assert len(as_user(ALLOWED, "select * from prospects")) == 1


def test_a_signed_in_stranger_reads_nothing(people):
    """Supabase deja registrarse a cualquiera: iniciar sesión no puede bastar."""
    assert as_user(STRANGER, "select * from prospects") == []
    assert as_user(STRANGER, "select * from llm_calls") == []


def test_a_request_without_a_token_reads_nothing(people):
    assert as_user(None, "select * from prospects") == []


def test_the_email_check_ignores_case(people):
    assert len(as_user(ALLOWED.upper(), "select * from prospects")) == 1


def test_an_allowed_user_can_discard_and_revive(people):
    as_user(ALLOWED, "update prospects set state = 'descartado' where slack_user_id = 'U1'")
    assert state_of("U1") == "descartado"
    as_user(ALLOWED, "update prospects set state = 'investigado' where slack_user_id = 'U1'")
    assert state_of("U1") == "investigado"


def test_a_stranger_cannot_change_a_state(people):
    as_user(STRANGER, "update prospects set state = 'descartado' where slack_user_id = 'U1'")
    assert state_of("U1") == "nuevo"


def test_incomplete_is_set_by_the_research_never_by_a_person(people):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(ALLOWED, "update prospects set state = 'incompleto' where slack_user_id = 'U1'")


def test_only_the_state_column_can_be_edited(people):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(ALLOWED, "update prospects set full_name = 'Otro' where slack_user_id = 'U1'")


def test_nobody_can_delete_a_person(people):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(ALLOWED, "delete from prospects where slack_user_id = 'U1'")
    assert db.fetch_one("select count(*) as n from prospects")["n"] == 1


def test_nobody_can_write_a_dossier(people):
    pid = db.fetch_one("select id from prospects")["id"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(
            ALLOWED,
            "insert into dossiers (prospect_id, version, content, sources) "
            "values (%s, 1, '{}'::jsonb, '[]'::jsonb)",
            (pid,),
        )


def test_an_allowed_user_can_queue_a_manual_research(people):
    as_user(ALLOWED, "insert into research_jobs (slack_user_id, reason) values ('U1', 'manual')")
    row = db.fetch_one("select reason, status from research_jobs")
    assert (row["reason"], row["status"]) == ("manual", "pendiente")


def test_only_manual_jobs_can_be_queued(people):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(
            ALLOWED, "insert into research_jobs (slack_user_id, reason) values ('U1', 'mensaje')"
        )


def test_a_stranger_cannot_queue_research(people):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(
            STRANGER, "insert into research_jobs (slack_user_id, reason) values ('U1', 'manual')"
        )


def test_an_allowed_user_can_move_a_threshold(people):
    try:
        as_user(ALLOWED, "update config set value = '3'::jsonb where key = 'banda_media_min'")
        assert db.fetch_one("select value from config where key = 'banda_media_min'")["value"] == 3
    finally:
        db.execute("update config set value = '2'::jsonb where key = 'banda_media_min'")


def test_nobody_touches_the_kill_switch_from_the_panel(people):
    as_user(ALLOWED, "update config set value = 'true'::jsonb where key = 'kill_switch'")
    assert db.fetch_one("select value from config where key = 'kill_switch'")["value"] is False


def test_the_cost_view_respects_the_allowlist(people):
    """Sin security_invoker, la vista correría como su dueño y se saltaría RLS."""
    assert as_user(STRANGER, "select * from panel_costes_diarios") == []
    rows = as_user(ALLOWED, "select usd from panel_costes_diarios")
    assert float(rows[0]["usd"]) == 0.5
