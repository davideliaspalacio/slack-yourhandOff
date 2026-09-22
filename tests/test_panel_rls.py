"""Las políticas del panel, probadas como las ve el panel: con el rol
authenticated y un token con correo, no como superusuario, que se las salta."""

import json

import psycopg
import pytest

from handoff_agent import db
from scripts import simular as simular_script

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


# --- panel_simular_persona / panel_borrar_simulados (modo de pruebas) ------


def simular(
    user, nombre, empresa, web=None, mensaje=None, links=None, notas=None, sin_tarjeta=None
):
    # sin_tarjeta=None omite el séptimo argumento (usa el default de la
    # función, false) en vez de mandar NULL explícito -- así un test que no
    # menciona sin_tarjeta prueba de verdad el mismo camino que un caller
    # viejo que nunca supo que ese parámetro existe.
    if sin_tarjeta is None:
        return as_user(
            user,
            "select panel_simular_persona(%s, %s, %s, %s, %s, %s)",
            (nombre, empresa, web, mensaje, links, notas),
        )
    return as_user(
        user,
        "select panel_simular_persona(%s, %s, %s, %s, %s, %s, %s)",
        (nombre, empresa, web, mensaje, links, notas, sin_tarjeta),
    )


def simulated(nombre="Ada Ruiz", empresa="Acme"):
    return db.fetch_one(
        "select * from prospects where slack_user_id = %s",
        (simular_script.fake_user_id(nombre, empresa),),
    )


def test_an_allowed_user_can_create_a_test_person(people):
    rows = simular(
        ALLOWED,
        "Ada Ruiz",
        "Acme",
        web="https://www.Acme.com/about",
        mensaje="hola, buscamos soporte",
        links=["https://acme.com/news", "https://acme.com/news"],
        notas="  nota de prueba  ",
    )
    person = simulated()
    assert person is not None
    assert person["slack_user_id"] == simular_script.fake_user_id("Ada Ruiz", "Acme")
    assert person["full_name"] == "Ada Ruiz"
    assert person["company_name"] == "Acme"
    assert person["state"] == "nuevo"
    assert person["company_domain_override"] == "acme.com"
    assert person["research_links"] == ["https://acme.com/news"]
    assert person["research_notes"] == "nota de prueba"
    # panel_simular_persona returns the prospect id.
    assert rows[0]["panel_simular_persona"] == person["id"]

    message = db.fetch_one(
        "select channel_id, user_id, text, status from slack_messages where user_id = %s",
        (person["slack_user_id"],),
    )
    assert message == {
        "channel_id": "CSIMULADO",
        "user_id": person["slack_user_id"],
        "text": "hola, buscamos soporte",
        # El mismo estado en el que resolve_pending() (ingest/resolver.py) deja
        # el mensaje de una persona ya conocida: delivery/deliver.py::_last_message
        # lo cita, y el resolver nunca lo vuelve a tocar.
        "status": "pendiente_scoring",
    }

    job = db.fetch_one("select reason, status, slack_user_id from research_jobs")
    assert (job["reason"], job["status"], job["slack_user_id"]) == (
        "manual",
        "pendiente",
        person["slack_user_id"],
    )


def test_a_test_person_defaults_to_publishing_a_card(people):
    """Sin mencionar sin_tarjeta (el camino de un caller que no lo sabe ni
    lo manda), la función usa su default: publicar la tarjeta como siempre."""
    simular(ALLOWED, "Ada Ruiz", "Acme")
    assert simulated()["sin_tarjeta"] is False


def test_sin_tarjeta_true_is_stored(people):
    simular(ALLOWED, "Ada Ruiz", "Acme", sin_tarjeta=True)
    assert simulated()["sin_tarjeta"] is True


def test_sin_tarjeta_false_is_stored_explicitly(people):
    simular(ALLOWED, "Ada Ruiz", "Acme", sin_tarjeta=False)
    assert simulated()["sin_tarjeta"] is False


def test_a_second_call_updates_sin_tarjeta(people):
    """El checkbox del panel manda su estado completo en cada envío, así que
    un segundo envío con el checkbox distinto de verdad cambia la fila --
    igual que pasa con empresa, web, enlaces y notas."""
    simular(ALLOWED, "Ada Ruiz", "Acme", sin_tarjeta=True)
    assert simulated()["sin_tarjeta"] is True

    simular(ALLOWED, "Ada Ruiz", "Acme", sin_tarjeta=False)
    assert simulated()["sin_tarjeta"] is False


def test_a_message_is_optional(people):
    simular(ALLOWED, "Leo Gil", "Beta")
    assert db.fetch_one("select count(*) as n from slack_messages")["n"] == 0
    assert simulated("Leo Gil", "Beta") is not None


def test_creating_a_test_person_is_idempotent_on_a_second_call(people):
    before = db.fetch_one("select count(*) as n from prospects")["n"]  # U1, del fixture

    simular(ALLOWED, "Ada Ruiz", "Acme", mensaje="primero")
    person = simulated()

    simular(ALLOWED, "Ada Ruiz", "Acme", mensaje="segundo")

    assert db.fetch_one("select count(*) as n from prospects")["n"] == before + 1
    assert simulated()["id"] == person["id"]
    assert db.fetch_one("select count(*) as n from slack_messages")["n"] == 2
    # La cola solo admite una tarea abierta por persona: el segundo envío no duplica.
    assert db.fetch_one("select count(*) as n from research_jobs")["n"] == 1


def test_a_name_or_a_company_is_required(people):
    before = db.fetch_one("select count(*) as n from prospects")["n"]  # U1, del fixture
    with pytest.raises(psycopg.errors.RaiseException, match="name or company is required"):
        simular(ALLOWED, "  ", "")
    assert db.fetch_one("select count(*) as n from prospects")["n"] == before


def test_a_discarded_test_person_is_rejected(people):
    simular(ALLOWED, "Ada Ruiz", "Acme")
    db.execute(
        "update prospects set state = 'descartado' where slack_user_id = %s",
        (simulated()["slack_user_id"],),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="person is discarded"):
        simular(ALLOWED, "Ada Ruiz", "Acme")
    assert simulated()["state"] == "descartado"


def test_the_website_is_normalised_and_validated_like_the_sibling_rpc(people):
    with pytest.raises(psycopg.errors.RaiseException, match="invalid domain"):
        simular(ALLOWED, "Ada Ruiz", "Acme", web="not a domain")


def test_links_are_validated_like_the_sibling_rpc(people):
    with pytest.raises(psycopg.errors.RaiseException, match="invalid link"):
        simular(ALLOWED, "Ada Ruiz", "Acme", links=["ftp://acme.com/x"])


def test_too_many_links_are_rejected(people):
    links = [f"https://acme.com/{i}" for i in range(11)]
    with pytest.raises(psycopg.errors.RaiseException, match="invalid link"):
        simular(ALLOWED, "Ada Ruiz", "Acme", links=links)


def test_notes_are_validated_like_the_sibling_rpc(people):
    with pytest.raises(psycopg.errors.RaiseException, match="invalid notes"):
        simular(ALLOWED, "Ada Ruiz", "Acme", notas="x" * 2001)


def test_a_stranger_cannot_create_a_test_person(people):
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        simular(STRANGER, "Ada Ruiz", "Acme")
    assert simulated() is None


def test_an_unauthenticated_request_cannot_create_a_test_person(people):
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        simular(None, "Ada Ruiz", "Acme")
    assert simulated() is None


def borrar(user):
    return as_user(user, "select panel_borrar_simulados()")


def test_deleting_test_data_only_touches_simulated_rows(people):
    simular(ALLOWED, "Ada Ruiz", "Acme", mensaje="hola")
    db.execute(
        "insert into prospects (slack_user_id, full_name, company_name) "
        "values ('UPRUEBA1', 'Legacy', 'LegacyCo')"
    )
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, status) "
        "values ('CSIMULADO', '2.0', 'UPRUEBA1', 'legacy', 'nuevo')"
    )
    # U1 (del fixture `people`) es una persona real: nunca debe tocarse.
    rows = borrar(ALLOWED)
    assert rows[0]["panel_borrar_simulados"] == 2

    assert db.fetch_one("select 1 as x from prospects where slack_user_id = 'U1'") == {"x": 1}
    assert (
        db.fetch_one("select 1 as x from prospects where slack_user_id like %s", ("USIM%",)) is None
    )
    assert db.fetch_one("select 1 as x from prospects where slack_user_id = 'UPRUEBA1'") is None
    assert db.fetch_one("select count(*) as n from slack_messages")["n"] == 0


def test_a_stranger_cannot_delete_test_data(people):
    simular(ALLOWED, "Ada Ruiz", "Acme")
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        borrar(STRANGER)
    assert simulated() is not None


def test_an_unauthenticated_request_cannot_delete_test_data(people):
    simular(ALLOWED, "Ada Ruiz", "Acme")
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        borrar(None)
    assert simulated() is not None
