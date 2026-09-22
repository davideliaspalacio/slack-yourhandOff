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


# --- panel_corregir_web (Plan de verificación de dominio, parte B) ----------


def prospect_id():
    return db.fetch_one("select id from prospects where slack_user_id = 'U1'")["id"]


def override_of(pid):
    return db.fetch_one("select company_domain_override from prospects where id = %s", (pid,))[
        "company_domain_override"
    ]


def test_an_allowed_user_can_correct_the_website(people):
    pid = prospect_id()
    as_user(ALLOWED, "select panel_corregir_web(%s, %s)", (pid, "https://www.Acme.com/about"))
    assert override_of(pid) == "acme.com"
    job = db.fetch_one("select reason, status, slack_user_id from research_jobs")
    assert (job["reason"], job["status"], job["slack_user_id"]) == ("manual", "pendiente", "U1")


def test_a_stranger_cannot_correct_the_website(people):
    pid = prospect_id()
    with pytest.raises(psycopg.errors.RaiseException):
        as_user(STRANGER, "select panel_corregir_web(%s, %s)", (pid, "acme.com"))
    assert override_of(pid) is None
    assert db.fetch_one("select count(*) as n from research_jobs")["n"] == 0


def test_an_unauthenticated_request_cannot_correct_the_website(people):
    pid = prospect_id()
    with pytest.raises(psycopg.errors.RaiseException):
        as_user(None, "select panel_corregir_web(%s, %s)", (pid, "acme.com"))
    assert override_of(pid) is None


def test_an_invalid_domain_is_rejected(people):
    pid = prospect_id()
    with pytest.raises(psycopg.errors.RaiseException):
        as_user(ALLOWED, "select panel_corregir_web(%s, %s)", (pid, "not a domain"))
    assert override_of(pid) is None
    assert db.fetch_one("select count(*) as n from research_jobs")["n"] == 0


def test_correcting_the_website_of_a_discarded_person_sets_no_job(people):
    pid = prospect_id()
    db.execute("update prospects set state = 'descartado' where id = %s", (pid,))
    as_user(ALLOWED, "select panel_corregir_web(%s, %s)", (pid, "acme.com"))
    assert override_of(pid) == "acme.com"
    assert db.fetch_one("select count(*) as n from research_jobs")["n"] == 0


def test_authenticated_cannot_write_the_override_column_directly(people):
    pid = prospect_id()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(
            ALLOWED,
            "update prospects set company_domain_override = 'acme.com' where id = %s",
            (pid,),
        )


def test_correcting_the_website_with_a_job_already_open_does_not_duplicate(people):
    pid = prospect_id()
    db.execute("insert into research_jobs (slack_user_id, reason) values ('U1', 'mensaje')")
    as_user(ALLOWED, "select panel_corregir_web(%s, %s)", (pid, "acme.com"))
    assert override_of(pid) == "acme.com"
    assert db.fetch_one("select count(*) as n from research_jobs")["n"] == 1


# --- panel_ayudar_research (Task 2: help the research from the panel) ------


def helped(pid):
    return db.fetch_one(
        "select company_name_override, company_domain_override, research_links, "
        "research_notes from prospects where id = %s",
        (pid,),
    )


def ayudar(user, pid, empresa=None, dominio=None, links=None, notas=None):
    return as_user(
        user,
        "select panel_ayudar_research(%s, %s, %s, %s, %s)",
        (pid, empresa, dominio, links, notas),
    )


def test_an_allowed_user_can_help_the_research(people):
    pid = prospect_id()
    ayudar(
        ALLOWED,
        pid,
        empresa="  Acme Inc.  ",
        dominio="https://www.Acme.com/about",
        links=["  https://acme.com/news  ", "https://acme.com/careers"],
        notas="  Growing fast, hiring support.  ",
    )
    row = helped(pid)
    assert row["company_name_override"] == "Acme Inc."
    assert row["company_domain_override"] == "acme.com"
    assert row["research_links"] == ["https://acme.com/news", "https://acme.com/careers"]
    assert row["research_notes"] == "Growing fast, hiring support."
    job = db.fetch_one("select reason, status, slack_user_id from research_jobs")
    assert (job["reason"], job["status"], job["slack_user_id"]) == ("manual", "pendiente", "U1")


def test_duplicate_links_are_deduped_keeping_the_first_order(people):
    pid = prospect_id()
    ayudar(ALLOWED, pid, links=["https://acme.com/a", "https://acme.com/b", "https://acme.com/a"])
    assert helped(pid)["research_links"] == ["https://acme.com/a", "https://acme.com/b"]


def test_blank_arguments_clear_the_overrides(people):
    pid = prospect_id()
    ayudar(
        ALLOWED, pid, empresa="Acme", dominio="acme.com", links=["https://acme.com/x"], notas="n"
    )
    assert helped(pid)["company_name_override"] == "Acme"

    ayudar(ALLOWED, pid, empresa="  ", dominio=None, links=[], notas="")
    row = helped(pid)
    assert row["company_name_override"] is None
    assert row["company_domain_override"] is None
    assert row["research_links"] is None
    assert row["research_notes"] is None


def test_more_than_ten_links_is_rejected(people):
    pid = prospect_id()
    links = [f"https://acme.com/{i}" for i in range(11)]
    with pytest.raises(psycopg.errors.RaiseException, match="invalid link"):
        ayudar(ALLOWED, pid, links=links)
    assert helped(pid)["research_links"] is None


def test_a_non_http_link_is_rejected(people):
    pid = prospect_id()
    with pytest.raises(psycopg.errors.RaiseException, match="invalid link"):
        ayudar(ALLOWED, pid, links=["ftp://acme.com/x"])
    assert helped(pid)["research_links"] is None


def test_a_link_over_500_chars_is_rejected(people):
    pid = prospect_id()
    long_link = "https://acme.com/" + "x" * 500
    with pytest.raises(psycopg.errors.RaiseException, match="invalid link"):
        ayudar(ALLOWED, pid, links=[long_link])
    assert helped(pid)["research_links"] is None


def test_notes_over_2000_chars_are_rejected(people):
    pid = prospect_id()
    with pytest.raises(psycopg.errors.RaiseException, match="invalid notes"):
        ayudar(ALLOWED, pid, notas="x" * 2001)
    assert helped(pid)["research_notes"] is None


def test_an_invalid_domain_is_rejected_by_ayudar_research(people):
    pid = prospect_id()
    with pytest.raises(psycopg.errors.RaiseException, match="invalid domain"):
        ayudar(ALLOWED, pid, dominio="not a domain")
    assert helped(pid)["company_domain_override"] is None


def test_a_stranger_cannot_help_the_research(people):
    pid = prospect_id()
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        ayudar(STRANGER, pid, empresa="Acme")
    assert helped(pid)["company_name_override"] is None
    assert db.fetch_one("select count(*) as n from research_jobs")["n"] == 0


def test_an_unauthenticated_request_cannot_help_the_research(people):
    pid = prospect_id()
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        ayudar(None, pid, empresa="Acme")
    assert helped(pid)["company_name_override"] is None


def test_helping_a_discarded_person_saves_but_sets_no_job(people):
    pid = prospect_id()
    db.execute("update prospects set state = 'descartado' where id = %s", (pid,))
    ayudar(ALLOWED, pid, empresa="Acme", notas="algo")
    row = helped(pid)
    assert row["company_name_override"] == "Acme"
    assert db.fetch_one("select count(*) as n from research_jobs")["n"] == 0


def test_helping_the_research_with_a_job_already_open_does_not_duplicate(people):
    pid = prospect_id()
    db.execute("insert into research_jobs (slack_user_id, reason) values ('U1', 'mensaje')")
    ayudar(ALLOWED, pid, empresa="Acme")
    assert helped(pid)["company_name_override"] == "Acme"
    assert db.fetch_one("select count(*) as n from research_jobs")["n"] == 1


def test_a_missing_person_is_reported_by_name(people):
    with pytest.raises(psycopg.errors.RaiseException, match="not found"):
        ayudar(ALLOWED, "00000000-0000-0000-0000-000000000000", empresa="Acme")


@pytest.mark.parametrize(
    "column,value",
    [
        ("company_name_override", "Acme"),
        ("company_domain_override", "acme.com"),
        ("research_notes", "n"),
    ],
)
def test_authenticated_cannot_write_the_new_columns_directly(people, column, value):
    pid = prospect_id()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(
            ALLOWED,
            f"update prospects set {column} = %s where id = %s",
            (value, pid),
        )


def test_authenticated_cannot_write_research_links_directly(people):
    pid = prospect_id()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(
            ALLOWED,
            "update prospects set research_links = %s where id = %s",
            (["https://acme.com"], pid),
        )
