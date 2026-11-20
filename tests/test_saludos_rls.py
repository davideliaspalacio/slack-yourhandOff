"""Lo que la migración 0037 deja ver y tocar desde el panel, probado como lo ve el
panel: con el rol `authenticated` y un token con correo (ver `test_panel_rls.py`)."""

import psycopg
import pytest

from handoff_agent import db
from tests.test_panel_rls import ALLOWED, STRANGER, as_user


@pytest.fixture
def people(conn):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))
    db.execute("insert into miembros_nuevos (slack_user_id, channel_id) values ('UDANA', 'C1')")


def valor(clave):
    return db.fetch_one("select value from config where key = %s", (clave,))["value"]


def test_the_defaults_of_the_migration(conn):
    assert valor("saludo_sugerido_activo") is True
    assert valor("miembros_nuevos_por_dia") == 20
    assert valor("miembro_nuevo_espera_min") == 30


def test_the_panel_reads_the_new_members_and_a_stranger_does_not(people):
    assert len(as_user(ALLOWED, "select * from miembros_nuevos")) == 1
    assert as_user(STRANGER, "select * from miembros_nuevos") == []
    assert as_user(None, "select * from miembros_nuevos") == []


def test_the_panel_cannot_write_the_new_members(people):
    """La tabla la escribe el worker: el panel solo la lee."""
    for sql in (
        "insert into miembros_nuevos (slack_user_id, channel_id) values ('UX', 'C1')",
        "update miembros_nuevos set estado = 'publicada'",
        "delete from miembros_nuevos",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_user(ALLOWED, sql)


def test_the_panel_reads_the_suggested_greeting(people):
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text) "
        "values ('C0INTROS', '1.1', 'UDANA', 'hi')"
    )
    db.execute(
        "insert into eventos_canal (channel_id, ts, tipo, slack_user_id, saludo) "
        "values ('C0INTROS', '1.1', 'presentacion', 'UDANA', 'Welcome Dana!')"
    )
    assert as_user(ALLOWED, "select saludo from eventos_canal") == [{"saludo": "Welcome Dana!"}]
    assert as_user(STRANGER, "select saludo from eventos_canal") == []


def test_the_greeting_switch_can_be_turned_off_and_on_from_settings(people, restore_config):
    as_user(
        ALLOWED, "update config set value = 'false'::jsonb where key = 'saludo_sugerido_activo'"
    )
    assert valor("saludo_sugerido_activo") is False
    as_user(ALLOWED, "update config set value = 'true'::jsonb where key = 'saludo_sugerido_activo'")
    assert valor("saludo_sugerido_activo") is True


@pytest.mark.parametrize("malo", ["1", '"yes"', "null"])
def test_the_greeting_switch_only_takes_a_boolean(people, restore_config, malo):
    with pytest.raises(psycopg.errors.Error):
        as_user(
            ALLOWED,
            "update config set value = %s::jsonb where key = 'saludo_sugerido_activo'",
            (malo,),
        )
    assert valor("saludo_sugerido_activo") is True


@pytest.mark.parametrize("clave", ["miembros_nuevos_por_dia", "miembro_nuevo_espera_min"])
def test_the_new_member_caps_can_be_changed_from_settings(people, restore_config, clave):
    as_user(ALLOWED, "update config set value = '5'::jsonb where key = %s", (clave,))
    assert valor(clave) == 5
    as_user(ALLOWED, "update config set value = '0'::jsonb where key = %s", (clave,))
    assert valor(clave) == 0


@pytest.mark.parametrize("clave", ["miembros_nuevos_por_dia", "miembro_nuevo_espera_min"])
@pytest.mark.parametrize("malo", ["-1", "true", '"20"'])
def test_the_new_member_caps_only_take_a_number_from_zero_up(people, restore_config, clave, malo):
    antes = valor(clave)
    with pytest.raises(psycopg.errors.Error):
        as_user(ALLOWED, "update config set value = %s::jsonb where key = %s", (malo, clave))
    assert valor(clave) == antes


def test_a_stranger_cannot_touch_any_of_it(people, restore_config):
    for clave, nuevo in (
        ("saludo_sugerido_activo", "false"),
        ("miembros_nuevos_por_dia", "999"),
        ("miembro_nuevo_espera_min", "999"),
    ):
        antes = valor(clave)
        as_user(STRANGER, "update config set value = %s::jsonb where key = %s", (nuevo, clave))
        assert valor(clave) == antes


def test_the_new_policy_does_not_widen_the_allowlist(people, restore_config):
    for clave in ("kill_switch", "monthly_budget_usd", "zona_horaria"):
        antes = valor(clave)
        as_user(ALLOWED, "update config set value = '5'::jsonb where key = %s", (clave,))
        assert valor(clave) == antes
