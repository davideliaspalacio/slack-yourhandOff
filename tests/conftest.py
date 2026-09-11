import os
import pathlib

import psycopg
import pytest

ADMIN_DB = os.environ.get(
    "TEST_ADMIN_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres"
)
TEST_DB_NAME = "handoff_test"
TEST_DB = ADMIN_DB.rsplit("/", 1)[0] + f"/{TEST_DB_NAME}"
MIGRATIONS = sorted(
    (pathlib.Path(__file__).parent.parent / "supabase" / "migrations").glob("*.sql")
)

# Orden inverso a las dependencias de clave ajena.
TABLES_TO_CLEAN = ["agent_actions", "llm_calls", "cost_events", "dossiers", "prospects"]

ENV_THAT_MUST_NOT_LEAK = [
    "BRAVE_SEARCH_API_KEY",
    "SLACK_USER_TOKEN",
    "SLACK_CHANNEL_IDS",
    "HANDOFF_ALERT_WEBHOOK_URL",
]


@pytest.fixture(scope="session")
def database_url() -> str:
    """Base propia para los tests, recreada en cada sesión desde las migraciones.
    Así los tests prueban además que las migraciones construyen el esquema
    desde cero, y nunca tocan los datos de desarrollo."""
    with psycopg.connect(ADMIN_DB, autocommit=True) as admin:
        admin.execute(f"drop database if exists {TEST_DB_NAME} with (force)")
        admin.execute(f"create database {TEST_DB_NAME}")
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        for migration in MIGRATIONS:
            conn.execute(migration.read_text())
    return TEST_DB


@pytest.fixture(autouse=True)
def test_environment(monkeypatch, database_url):
    """Los tests nunca llaman a OpenAI de verdad; load_settings() solo necesita
    que la variable exista. Valor obvio para que un uso accidental falle."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    # Si algún día hay claves reales en .env, load_dotenv las mete en el entorno
    # y los tests acabarían llamando a Brave, a Slack o al webhook de verdad.
    for name in ENV_THAT_MUST_NOT_LEAK:
        monkeypatch.delenv(name, raising=False)


def _existing(cur, tables: list[str]) -> list[str]:
    cur.execute("select table_name from information_schema.tables where table_schema = 'public'")
    present = {row[0] for row in cur.fetchall()}
    return [t for t in tables if t in present]


@pytest.fixture
def conn(database_url):
    """Conexión limpia. Vacía las tablas antes de cada test, no después: así,
    cuando un test falla, sus filas siguen ahí para inspeccionarlas."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cur:
            cur.execute("select current_database()")
            database = cur.fetchone()[0]
            if database != TEST_DB_NAME:
                raise RuntimeError(
                    f"refusing to truncate {database!r}: tests only run on {TEST_DB_NAME}"
                )
            present = _existing(cur, TABLES_TO_CLEAN)
            if present:
                # Un solo TRUNCATE: en sentencias separadas choca con los locks
                # del pool y la suite se cae por deadlock.
                cur.execute(f"truncate table {', '.join(present)} restart identity cascade")
        yield connection
