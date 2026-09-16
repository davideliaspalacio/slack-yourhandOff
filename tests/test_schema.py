import psycopg
import pytest

LEDGER_TABLES = ["dossiers", "llm_calls", "cost_events", "agent_actions", "config"]
DELIVERY_TABLES = ["deliveries"]


def test_prospects_table_exists_with_expected_columns(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select column_name from information_schema.columns where table_name = 'prospects'"
        )
        columns = {row[0] for row in cur.fetchall()}
    assert {"id", "slack_user_id", "state", "company_domain"} <= columns


def test_prospect_state_defaults_to_nuevo(conn):
    with conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id) values ('U123') returning state")
        assert cur.fetchone()[0] == "nuevo"


def test_slack_user_id_is_unique(conn):
    with conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id) values ('U999')")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id) values ('U999')")


def test_rls_is_enabled_on_prospects(conn):
    with conn.cursor() as cur:
        cur.execute("select relrowsecurity from pg_class where relname = 'prospects'")
        assert cur.fetchone()[0] is True


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_ledger_table_exists_with_rls(conn, table):
    with conn.cursor() as cur:
        cur.execute("select relrowsecurity from pg_class where relname = %s", (table,))
        row = cur.fetchone()
    assert row is not None, f"la tabla {table} no existe"
    assert row[0] is True, f"RLS desactivado en {table}"


@pytest.mark.parametrize("table", DELIVERY_TABLES)
def test_delivery_table_exists_with_rls(conn, table):
    with conn.cursor() as cur:
        cur.execute("select relrowsecurity from pg_class where relname = %s", (table,))
        row = cur.fetchone()
    assert row is not None, f"la tabla {table} no existe"
    assert row[0] is True, f"RLS desactivado en {table}"


def test_config_ships_with_kill_switch_off(conn):
    with conn.cursor() as cur:
        cur.execute("select value from config where key = 'kill_switch'")
        assert cur.fetchone()[0] is False


def test_dossier_versions_are_unique_per_prospect(conn):
    with conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id) values ('U1') returning id")
        prospect_id = cur.fetchone()[0]
        cur.execute(
            "insert into dossiers (prospect_id, version, content) values (%s, 1, '{}')",
            (prospect_id,),
        )
    with pytest.raises(psycopg.errors.UniqueViolation), conn.cursor() as cur:
        cur.execute(
            "insert into dossiers (prospect_id, version, content) values (%s, 1, '{}')",
            (prospect_id,),
        )


def test_suite_runs_against_the_test_database(conn):
    with conn.cursor() as cur:
        cur.execute("select current_database()")
        assert cur.fetchone()[0] == "handoff_test"


INGEST_TABLES = ["slack_messages", "member_snapshots", "research_jobs"]


@pytest.mark.parametrize("table", INGEST_TABLES)
def test_ingest_table_exists_with_rls(conn, table):
    with conn.cursor() as cur:
        cur.execute("select relrowsecurity from pg_class where relname = %s", (table,))
        row = cur.fetchone()
    assert row is not None, f"la tabla {table} no existe"
    assert row[0] is True, f"RLS desactivado en {table}"


def test_only_one_open_research_job_per_person(conn):
    with conn.cursor() as cur:
        cur.execute("insert into research_jobs (slack_user_id, reason) values ('U1', 'mensaje')")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.cursor() as cur:
        cur.execute(
            "insert into research_jobs (slack_user_id, reason) values ('U1', 'miembro_nuevo')"
        )


def test_a_finished_job_does_not_block_a_new_one(conn):
    with conn.cursor() as cur:
        cur.execute(
            "insert into research_jobs (slack_user_id, reason, status) values ('U1', 'mensaje', 'hecho')"
        )
        cur.execute("insert into research_jobs (slack_user_id, reason) values ('U1', 'mensaje')")
        cur.execute("select count(*) from research_jobs where slack_user_id = 'U1'")
        assert cur.fetchone()[0] == 2


def test_a_slack_message_is_stored_once(conn):
    with conn.cursor() as cur:
        cur.execute(
            "insert into slack_messages (channel_id, ts) values ('C1', '1726000000.000100')"
        )
    with pytest.raises(psycopg.errors.UniqueViolation), conn.cursor() as cur:
        cur.execute(
            "insert into slack_messages (channel_id, ts) values ('C1', '1726000000.000100')"
        )


def test_config_ships_with_an_hourly_research_limit(conn):
    with conn.cursor() as cur:
        cur.execute("select value from config where key = 'research_por_hora'")
        assert cur.fetchone()[0] == 20
