import os

import psycopg
import pytest

DEFAULT_DB = "postgresql://postgres:postgres@127.0.0.1:54332/postgres"

# Orden inverso a las dependencias de clave ajena.
TABLES_TO_CLEAN = ["agent_actions", "llm_calls", "cost_events", "dossiers", "prospects"]


def _existing(cur, tables: list[str]) -> list[str]:
    """Las migraciones se aplican por tareas: hasta la Task 4 varias de estas
    tablas no existen todavía. Se limpia lo que hay."""
    cur.execute("select table_name from information_schema.tables where table_schema = 'public'")
    present = {row[0] for row in cur.fetchall()}
    return [t for t in tables if t in present]


@pytest.fixture(autouse=True)
def test_environment(monkeypatch):
    """Los tests nunca llaman a OpenAI de verdad — el cliente va falseado — pero
    load_settings() exige que la variable exista. Se rellena con un valor obvio
    para que un uso accidental falle de forma legible, no con una clave real."""
    monkeypatch.setenv("DATABASE_URL", os.environ.get("DATABASE_URL") or DEFAULT_DB)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DB)


@pytest.fixture
def conn(database_url):
    """Conexión limpia. Vacía las tablas antes de cada test, no después:
    así, cuando un test falla, sus filas siguen ahí para inspeccionarlas."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cur:
            for table in _existing(cur, TABLES_TO_CLEAN):
                cur.execute(f"truncate table {table} cascade")
        yield connection
