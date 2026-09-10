# Plan 1 — Fundación: Supabase, ledger de costes y toolbox MCP

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dejar operativa la base de datos, la contabilidad de costes y el toolbox de research expuesto por MCP, de forma que se pueda investigar a una persona a mano y ver cada céntimo registrado — sin que Slack esté involucrado todavía.

**Architecture:** Un paquete Python (`handoff_agent`) sobre Supabase local en Docker. Todo el esquema vive en `supabase/migrations/`. Cada llamada a un servicio de pago pasa por un ledger que la registra en Postgres antes de devolver. Las herramientas de research se exponen como servidor MCP para que las consuman el pipeline, Claude Code y, más adelante, el CEO Command Center.

**Tech Stack:** Python 3.13, uv, Supabase CLI, psycopg 3, pytest, MCP Python SDK (FastMCP), httpx, Crawl4AI, trafilatura, python-jobspy, openai, langfuse.

## Global Constraints

- **Todo cambio de esquema es un archivo en `supabase/migrations/`.** Nada creado a mano en Studio: lo que se hace clicando no viaja a Supabase Cloud.
- **Ninguna llamada a un servicio de pago sin pasar por el ledger.** Si una función llama a OpenAI, Twilio o cualquier API con coste y no registra en `llm_calls` o `cost_events`, está mal escrita.
- **RLS activado en todas las tablas.** Los workers usan conexión directa (`service_role`, se salta RLS); el panel irá por PostgREST en el Plan 4.
- **Ningún secreto en el repo.** Todo por variables de entorno. `.env` está en `.gitignore`; `.env.example` documenta las claves sin valores.
- **Modelo LLM:** `gpt-4.1` para todo lo human-facing. El nombre del modelo es configurable por entorno, nunca hardcodeado en una llamada.
- **Idioma del código:** identificadores y comentarios en inglés; los nombres de las herramientas MCP en español, porque son la interfaz que consume el equipo (`buscar_web`, `leer_sitio`, `buscar_ofertas`, `historial_prospecto`).
- **Toda función que hace red tiene timeout explícito.** Sin excepciones: un fetch colgado bloquea un worker.

---

## Estructura de archivos

```
pyproject.toml                      dependencias y config de pytest/ruff
.env.example                        claves documentadas, sin valores
docker-compose.searxng.yml          SearXNG local para buscar_web

supabase/
  config.toml                       generado por la CLI
  migrations/
    0001_prospects.sql              personas + estado
    0002_dossiers.sql               dossiers versionados
    0003_ledger.sql                 llm_calls, cost_events, agent_actions, config

src/handoff_agent/
  __init__.py
  config.py                         Settings desde entorno, con validación
  db.py                             pool de conexiones y helpers de consulta
  ledger.py                         registro de coste LLM y no-LLM
  guards.py                         kill switch, tope mensual, tope por ejecución
  llm.py                            cliente GPT-4.1 envuelto en ledger + Langfuse
  mcp_server.py                     expone las herramientas por MCP
  tools/
    __init__.py
    search.py                       buscar_web        (SearXNG)
    web.py                          leer_sitio        (Crawl4AI + trafilatura)
    jobs.py                         buscar_ofertas    (JobSpy)
    prospects.py                    historial_prospecto (Supabase)

tests/
  conftest.py                       fixtures de base de datos limpia
  test_config.py
  test_schema.py
  test_db.py
  test_ledger.py
  test_guards.py
  test_llm.py
  test_search.py
  test_web.py
  test_jobs.py
  test_prospects.py
  test_mcp_server.py
```

**Por qué esta división:** `tools/` agrupa lo que se expone por MCP, un archivo por servicio externo, de modo que cambiar de proveedor toca un solo archivo. `ledger.py` y `guards.py` están separados porque uno registra y el otro decide: se prueban por separado y el ledger no debe poder bloquear nada.

---

### Task 1: Bootstrap del proyecto y configuración

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `src/handoff_agent/__init__.py`
- Create: `src/handoff_agent/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nada
- Produces: `Settings` (dataclass congelada) y `load_settings() -> Settings`. Campos: `database_url: str`, `openai_api_key: str`, `openai_model: str`, `searxng_url: str`, `langfuse_public_key: str | None`, `langfuse_secret_key: str | None`, `langfuse_host: str`, `price_input_per_m: float`, `price_cached_input_per_m: float`, `price_output_per_m: float`, `http_timeout_seconds: float`, `monthly_budget_usd: float`, `run_budget_usd: float`.

- [ ] **Step 1: Crear `pyproject.toml`**

```toml
[project]
name = "handoff-agent"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "psycopg[binary,pool]>=3.2",
    "httpx>=0.28",
    "openai>=1.60",
    "mcp>=1.2",
    "python-jobspy>=1.1.80",
    "trafilatura>=2.0",
    "langfuse>=2.57",
]

[dependency-groups]
dev = ["pytest>=8.3", "pytest-asyncio>=0.25", "ruff>=0.9", "respx>=0.22"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/handoff_agent"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-v"

[tool.ruff]
line-length = 100
```

Nota: Crawl4AI se añade en la Task 9, porque arrastra Playwright y conviene aislar ese fallo si ocurre.

- [ ] **Step 2: Escribir el test que falla**

```python
# tests/test_config.py
import pytest
from handoff_agent.config import load_settings


def test_load_settings_reads_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = load_settings()
    assert settings.database_url.endswith("/postgres")
    assert settings.openai_model == "gpt-4.1"
    assert settings.http_timeout_seconds > 0


def test_load_settings_fails_loudly_when_required_var_missing(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        load_settings()
```

- [ ] **Step 3: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.config'`

- [ ] **Step 4: Implementar `config.py`**

```python
# src/handoff_agent/config.py
"""Environment-backed settings. Fail loudly at startup, never mid-pipeline."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    openai_api_key: str
    openai_model: str
    searxng_url: str
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_host: str
    price_input_per_m: float
    price_cached_input_per_m: float
    price_output_per_m: float
    http_timeout_seconds: float
    monthly_budget_usd: float
    run_budget_usd: float


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def load_settings() -> Settings:
    return Settings(
        database_url=_required("DATABASE_URL"),
        openai_api_key=_required("OPENAI_API_KEY"),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4.1"),
        searxng_url=os.environ.get("SEARXNG_URL", "http://127.0.0.1:8080"),
        langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
        langfuse_host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        price_input_per_m=float(os.environ.get("PRICE_INPUT_PER_M", "2.00")),
        price_cached_input_per_m=float(os.environ.get("PRICE_CACHED_INPUT_PER_M", "0.50")),
        price_output_per_m=float(os.environ.get("PRICE_OUTPUT_PER_M", "8.00")),
        http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "30")),
        monthly_budget_usd=float(os.environ.get("MONTHLY_BUDGET_USD", "150")),
        run_budget_usd=float(os.environ.get("RUN_BUDGET_USD", "1.00")),
    )
```

Los precios son valores por defecto que hay que contrastar con la página de precios de OpenAI antes de producción; por eso son configurables por entorno y no constantes en el código.

- [ ] **Step 5: Crear `.env.example` y `src/handoff_agent/__init__.py`**

```bash
touch src/handoff_agent/__init__.py
cat > .env.example <<'EOF'
# Supabase local — sale de `supabase status`
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54332/postgres

# OpenAI
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1

# SearXNG local (docker-compose.searxng.yml)
SEARXNG_URL=http://127.0.0.1:8080

# Langfuse Cloud — opcional; sin claves, el trazado queda desactivado
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com

# Precios USD por millón de tokens — CONTRASTAR con la web de OpenAI
PRICE_INPUT_PER_M=2.00
PRICE_CACHED_INPUT_PER_M=0.50
PRICE_OUTPUT_PER_M=8.00

# Guardarraíles
HTTP_TIMEOUT_SECONDS=30
MONTHLY_BUDGET_USD=150
RUN_BUDGET_USD=1.00
EOF
```

- [ ] **Step 6: Ejecutar los tests y comprobar que pasan**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS, 2 tests

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .env.example src/handoff_agent/ tests/test_config.py uv.lock
git commit -m "feat: bootstrap del paquete handoff_agent con configuración por entorno"
```

---

### Task 2: Esquema de personas y migración inicial

**Files:**
- Create: `supabase/migrations/0001_prospects.sql`
- Test: `tests/conftest.py`, `tests/test_schema.py`

**Interfaces:**
- Consumes: `load_settings()` de la Task 1
- Produces: tabla `prospects` con columnas `id uuid`, `slack_user_id text unique`, `full_name text`, `company_name text`, `company_domain text`, `state prospect_state`, `first_seen_at timestamptz`, `created_at`, `updated_at`. Tipo enum `prospect_state` con valores `nuevo | investigado | contactado | cliente | descartado | incompleto`.

- [ ] **Step 1: Escribir la migración**

```sql
-- supabase/migrations/0001_prospects.sql
create type prospect_state as enum (
    'nuevo', 'investigado', 'contactado', 'cliente', 'descartado', 'incompleto'
);

create table prospects (
    id             uuid primary key default gen_random_uuid(),
    slack_user_id  text unique not null,
    full_name      text,
    company_name   text,
    company_domain text,
    state          prospect_state not null default 'nuevo',
    first_seen_at  timestamptz not null default now(),
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

create index prospects_state_idx on prospects (state);

-- El descarte es pegajoso: se consulta en cada mensaje entrante, tiene que ser barato.
create index prospects_discarded_idx on prospects (slack_user_id) where state = 'descartado';

alter table prospects enable row level security;
```

RLS queda activado sin políticas: eso deniega todo por defecto para roles normales, y los workers entran con `service_role`, que se la salta. Las políticas del panel llegan en el Plan 4.

- [ ] **Step 2: Escribir las fixtures de test**

```python
# tests/conftest.py
import os

import psycopg
import pytest

DEFAULT_DB = "postgresql://postgres:postgres@127.0.0.1:54332/postgres"

# Orden inverso a las dependencias de clave ajena.
TABLES_TO_CLEAN = ["agent_actions", "llm_calls", "cost_events", "dossiers", "prospects"]


def _existing(cur, tables: list[str]) -> list[str]:
    """Las migraciones se aplican por tareas: hasta la Task 4 varias de estas
    tablas no existen todavía, y truncarlas a ciegas rompería los tests de la
    Task 2. Se limpia lo que hay."""
    cur.execute(
        "select table_name from information_schema.tables where table_schema = 'public'"
    )
    present = {row[0] for row in cur.fetchall()}
    return [t for t in tables if t in present]


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
```

`_existing` filtra a las tablas que ya existen, porque las migraciones se aplican tarea a tarea y hasta la Task 4 varias no están creadas.

- [ ] **Step 3: Escribir el test que falla**

```python
# tests/test_schema.py
def test_prospects_table_exists_with_expected_columns(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select column_name from information_schema.columns "
            "where table_name = 'prospects'"
        )
        columns = {row[0] for row in cur.fetchall()}
    assert {"id", "slack_user_id", "state", "company_domain"} <= columns


def test_prospect_state_defaults_to_nuevo(conn):
    with conn.cursor() as cur:
        cur.execute(
            "insert into prospects (slack_user_id) values ('U123') returning state"
        )
        assert cur.fetchone()[0] == "nuevo"


def test_slack_user_id_is_unique(conn):
    import psycopg
    import pytest

    with conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id) values ('U999')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute("insert into prospects (slack_user_id) values ('U999')")


def test_rls_is_enabled_on_prospects(conn):
    with conn.cursor() as cur:
        cur.execute("select relrowsecurity from pg_class where relname = 'prospects'")
        assert cur.fetchone()[0] is True
```

- [ ] **Step 4: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_schema.py -v`
Expected: FAIL — `relation "prospects" does not exist`

- [ ] **Step 5: Aplicar la migración**

```bash
supabase db reset
```

`db reset` recrea la base desde cero aplicando todas las migraciones en orden. Es el comando que garantiza que las migraciones son reproducibles — si el esquema solo funciona aplicando parches a mano, `db reset` lo delata.

- [ ] **Step 6: Ejecutar los tests y comprobar que pasan**

Run: `uv run pytest tests/test_schema.py -v`
Expected: PASS, 4 tests

- [ ] **Step 7: Commit**

```bash
git add supabase/migrations/0001_prospects.sql tests/conftest.py tests/test_schema.py
git commit -m "feat: tabla prospects con estado por persona y RLS activado"
```

---

### Task 3: Capa de acceso a datos

**Files:**
- Create: `src/handoff_agent/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `load_settings()` de la Task 1
- Produces:
  - `get_pool() -> ConnectionPool` — pool perezoso, de proceso único
  - `fetch_one(sql: str, params: tuple = ()) -> dict | None`
  - `fetch_all(sql: str, params: tuple = ()) -> list[dict]`
  - `execute(sql: str, params: tuple = ()) -> int` — devuelve filas afectadas

- [ ] **Step 1: Escribir el test que falla**

```python
# tests/test_db.py
from handoff_agent import db


def test_fetch_one_returns_dict(conn):
    with conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id, full_name) values ('U1', 'Ada')")
    row = db.fetch_one("select slack_user_id, full_name from prospects where slack_user_id = %s", ("U1",))
    assert row == {"slack_user_id": "U1", "full_name": "Ada"}


def test_fetch_one_returns_none_when_no_match():
    assert db.fetch_one("select 1 as n from prospects where slack_user_id = %s", ("nope",)) is None


def test_fetch_all_returns_every_row(conn):
    with conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id) values ('U1'), ('U2')")
    rows = db.fetch_all("select slack_user_id from prospects order by slack_user_id")
    assert [r["slack_user_id"] for r in rows] == ["U1", "U2"]


def test_execute_returns_affected_row_count(conn):
    with conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id) values ('U1'), ('U2')")
    assert db.execute("update prospects set state = 'investigado'") == 2
```

- [ ] **Step 2: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.db'`

- [ ] **Step 3: Implementar `db.py`**

```python
# src/handoff_agent/db.py
"""Postgres access for the workers.

Workers connect straight to Postgres rather than through PostgREST because the
job queue relies on SELECT ... FOR UPDATE SKIP LOCKED, which PostgREST does not
expose. This connection carries service_role and therefore bypasses RLS.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import load_settings


@lru_cache(maxsize=1)
def get_pool() -> ConnectionPool:
    settings = load_settings()
    return ConnectionPool(settings.database_url, min_size=1, max_size=8, open=True)


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict | None:
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount
```

- [ ] **Step 4: Ejecutar los tests y comprobar que pasan**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/db.py tests/test_db.py
git commit -m "feat: capa de acceso a Postgres con pool y filas como diccionario"
```

---

### Task 4: Esquema de dossiers y ledger

**Files:**
- Create: `supabase/migrations/0002_dossiers.sql`
- Create: `supabase/migrations/0003_ledger.sql`
- Modify: `tests/test_schema.py`

**Interfaces:**
- Consumes: `prospects` de la Task 2
- Produces: tablas `dossiers`, `llm_calls`, `cost_events`, `agent_actions`, `config`

- [ ] **Step 1: Escribir la migración de dossiers**

```sql
-- supabase/migrations/0002_dossiers.sql
create table dossiers (
    id           uuid primary key default gen_random_uuid(),
    prospect_id  uuid not null references prospects (id) on delete cascade,
    version      integer not null,
    content      jsonb not null,
    sources      jsonb not null default '[]'::jsonb,
    created_at   timestamptz not null default now(),
    unique (prospect_id, version)
);

-- Un dossier caduca a los 6 meses (sección 9 del spec); esta vista da el vigente.
create index dossiers_prospect_version_idx on dossiers (prospect_id, version desc);

alter table dossiers enable row level security;
```

- [ ] **Step 2: Escribir la migración del ledger**

```sql
-- supabase/migrations/0003_ledger.sql

-- Toda llamada a un LLM, sin excepción.
create table llm_calls (
    id             uuid primary key default gen_random_uuid(),
    prospect_id    uuid references prospects (id) on delete set null,
    stage          text not null,
    model          text not null,
    input_tokens   integer not null default 0,
    cached_tokens  integer not null default 0,
    output_tokens  integer not null default 0,
    cost_usd       numeric(12, 6) not null default 0,
    latency_ms     integer,
    trace_id       text,
    created_at     timestamptz not null default now()
);

create index llm_calls_created_idx on llm_calls (created_at desc);
create index llm_calls_prospect_idx on llm_calls (prospect_id);

-- Todo coste que no es LLM: Twilio, proxies, compute, APIs de pago futuras.
create table cost_events (
    id           uuid primary key default gen_random_uuid(),
    prospect_id  uuid references prospects (id) on delete set null,
    source       text not null,
    description  text,
    cost_usd     numeric(12, 6) not null,
    created_at   timestamptz not null default now()
);

create index cost_events_created_idx on cost_events (created_at desc);

-- Log append-only de lo que hace el agente. Es el contexto que consulta
-- antes de decidir, y el rastro de auditoría cuando algo sale mal.
create table agent_actions (
    id           uuid primary key default gen_random_uuid(),
    prospect_id  uuid references prospects (id) on delete set null,
    action       text not null,
    payload      jsonb not null default '{}'::jsonb,
    result       jsonb,
    created_at   timestamptz not null default now()
);

create index agent_actions_prospect_idx on agent_actions (prospect_id, created_at desc);

-- Umbrales, topes y kill switch. Editable sin redeploy.
create table config (
    key         text primary key,
    value       jsonb not null,
    updated_at  timestamptz not null default now()
);

insert into config (key, value) values
    ('kill_switch', 'false'::jsonb),
    ('monthly_budget_usd', '150'::jsonb),
    ('run_budget_usd', '1.0'::jsonb);

alter table dossiers      enable row level security;
alter table llm_calls     enable row level security;
alter table cost_events   enable row level security;
alter table agent_actions enable row level security;
alter table config        enable row level security;
```

- [ ] **Step 3: Ampliar el test de esquema**

```python
# tests/test_schema.py — añadir al final
import pytest

LEDGER_TABLES = ["dossiers", "llm_calls", "cost_events", "agent_actions", "config"]


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_ledger_table_exists_with_rls(conn, table):
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
    import psycopg

    with conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id) values ('U1') returning id")
        prospect_id = cur.fetchone()[0]
        cur.execute(
            "insert into dossiers (prospect_id, version, content) values (%s, 1, '{}')",
            (prospect_id,),
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                "insert into dossiers (prospect_id, version, content) values (%s, 1, '{}')",
                (prospect_id,),
            )
```

- [ ] **Step 4: Ejecutar los tests y comprobar que fallan**

Run: `uv run pytest tests/test_schema.py -v`
Expected: FAIL — `relation "dossiers" does not exist`

- [ ] **Step 5: Aplicar las migraciones**

Run: `supabase db reset`
Expected: aplica 0001, 0002 y 0003 sin error

- [ ] **Step 6: Ejecutar los tests y comprobar que pasan**

Run: `uv run pytest -v`
Expected: PASS, todos

- [ ] **Step 7: Commit**

```bash
git add supabase/migrations/ tests/test_schema.py
git commit -m "feat: esquema de dossiers, ledger de costes, log de acciones y config"
```

---

### Task 5: Ledger de costes

**Files:**
- Create: `src/handoff_agent/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Consumes: `db.fetch_one`, `db.execute` de la Task 3; `load_settings()` de la Task 1
- Produces:
  - `compute_llm_cost(input_tokens: int, cached_tokens: int, output_tokens: int) -> Decimal`
  - `record_llm_call(stage: str, model: str, input_tokens: int, cached_tokens: int, output_tokens: int, latency_ms: int | None = None, prospect_id: str | None = None, trace_id: str | None = None) -> Decimal` — devuelve el coste registrado
  - `record_cost_event(source: str, cost_usd: Decimal | float, description: str | None = None, prospect_id: str | None = None) -> None`
  - `record_action(action: str, payload: dict, result: dict | None = None, prospect_id: str | None = None) -> None`
  - `spend_since(since: datetime) -> Decimal` — suma de `llm_calls` y `cost_events`

- [ ] **Step 1: Escribir el test que falla**

```python
# tests/test_ledger.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from handoff_agent import ledger


def test_compute_llm_cost_charges_cached_tokens_at_the_lower_rate():
    # 1M frescos a $2, 1M cacheados a $0.50, 1M de salida a $8
    cost = ledger.compute_llm_cost(1_000_000, 1_000_000, 1_000_000)
    assert cost == Decimal("10.50")


def test_compute_llm_cost_is_zero_for_no_tokens():
    assert ledger.compute_llm_cost(0, 0, 0) == Decimal("0")


def test_record_llm_call_persists_a_row_and_returns_cost(conn):
    cost = ledger.record_llm_call(
        stage="triage", model="gpt-4.1",
        input_tokens=1000, cached_tokens=0, output_tokens=500,
    )
    with conn.cursor() as cur:
        cur.execute("select stage, model, cost_usd from llm_calls")
        row = cur.fetchone()
    assert row[0] == "triage"
    assert row[1] == "gpt-4.1"
    assert Decimal(row[2]) == cost
    assert cost > 0


def test_record_cost_event_persists_non_llm_spend(conn):
    ledger.record_cost_event(source="twilio", cost_usd=0.0079, description="SMS a Anthony")
    with conn.cursor() as cur:
        cur.execute("select source, cost_usd from cost_events")
        row = cur.fetchone()
    assert row[0] == "twilio"
    assert Decimal(row[1]) == Decimal("0.007900")


def test_record_action_appends_to_the_log(conn):
    ledger.record_action(action="buscar_web", payload={"query": "acme corp"}, result={"hits": 3})
    with conn.cursor() as cur:
        cur.execute("select action, payload, result from agent_actions")
        row = cur.fetchone()
    assert row[0] == "buscar_web"
    assert row[1] == {"query": "acme corp"}
    assert row[2] == {"hits": 3}


def test_spend_since_sums_both_ledgers(conn):
    ledger.record_llm_call(
        stage="research", model="gpt-4.1",
        input_tokens=1_000_000, cached_tokens=0, output_tokens=0,
    )  # $2.00
    ledger.record_cost_event(source="twilio", cost_usd=1.00)
    total = ledger.spend_since(datetime.now(UTC) - timedelta(hours=1))
    assert total == Decimal("3.00")
```

- [ ] **Step 2: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_ledger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.ledger'`

- [ ] **Step 3: Implementar `ledger.py`**

```python
# src/handoff_agent/ledger.py
"""Cost ledger. Every paid call lands here before anything else happens.

Supabase is the source of truth for spend because the metric that matters —
cost per closed deal — is a JOIN against prospects, feedback and deliveries.
Langfuse holds the execution trees; it cannot answer that question.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from . import db
from .config import load_settings

MILLION = Decimal(1_000_000)


def compute_llm_cost(input_tokens: int, cached_tokens: int, output_tokens: int) -> Decimal:
    settings = load_settings()
    return (
        Decimal(input_tokens) / MILLION * Decimal(str(settings.price_input_per_m))
        + Decimal(cached_tokens) / MILLION * Decimal(str(settings.price_cached_input_per_m))
        + Decimal(output_tokens) / MILLION * Decimal(str(settings.price_output_per_m))
    )


def record_llm_call(
    stage: str,
    model: str,
    input_tokens: int,
    cached_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int | None = None,
    prospect_id: str | None = None,
    trace_id: str | None = None,
) -> Decimal:
    cost = compute_llm_cost(input_tokens, cached_tokens, output_tokens)
    db.execute(
        """
        insert into llm_calls
            (prospect_id, stage, model, input_tokens, cached_tokens,
             output_tokens, cost_usd, latency_ms, trace_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (prospect_id, stage, model, input_tokens, cached_tokens,
         output_tokens, cost, latency_ms, trace_id),
    )
    return cost


def record_cost_event(
    source: str,
    cost_usd: Decimal | float,
    description: str | None = None,
    prospect_id: str | None = None,
) -> None:
    db.execute(
        "insert into cost_events (prospect_id, source, description, cost_usd) "
        "values (%s, %s, %s, %s)",
        (prospect_id, source, description, Decimal(str(cost_usd))),
    )


def record_action(
    action: str,
    payload: dict,
    result: dict | None = None,
    prospect_id: str | None = None,
) -> None:
    db.execute(
        "insert into agent_actions (prospect_id, action, payload, result) "
        "values (%s, %s, %s, %s)",
        (prospect_id, action, json.dumps(payload),
         json.dumps(result) if result is not None else None),
    )


def spend_since(since: datetime) -> Decimal:
    row = db.fetch_one(
        """
        select coalesce(
            (select sum(cost_usd) from llm_calls   where created_at >= %s), 0
        ) + coalesce(
            (select sum(cost_usd) from cost_events where created_at >= %s), 0
        ) as total
        """,
        (since, since),
    )
    return Decimal(row["total"])
```

- [ ] **Step 4: Ejecutar los tests y comprobar que pasan**

Run: `uv run pytest tests/test_ledger.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/ledger.py tests/test_ledger.py
git commit -m "feat: ledger de costes de LLM, gastos externos y log de acciones"
```

---

### Task 6: Guardarraíles de presupuesto

**Files:**
- Create: `src/handoff_agent/guards.py`
- Test: `tests/test_guards.py`

**Interfaces:**
- Consumes: `ledger.spend_since`, `db.fetch_one` de tareas anteriores
- Produces:
  - `KillSwitchActive(RuntimeError)`, `MonthlyBudgetExceeded(RuntimeError)`, `RunBudgetExceeded(RuntimeError)`
  - `check_kill_switch() -> None` — lanza si está activo
  - `monthly_spend() -> Decimal`
  - `check_monthly_budget() -> None` — lanza si se superó el tope
  - `RunBudget` — context manager con `.add(cost)` y `.spent`

- [ ] **Step 1: Escribir el test que falla**

```python
# tests/test_guards.py
from decimal import Decimal

import pytest

from handoff_agent import guards, ledger


def test_kill_switch_passes_when_off(conn):
    guards.check_kill_switch()  # no debe lanzar


def test_kill_switch_raises_when_on(conn):
    with conn.cursor() as cur:
        cur.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    with pytest.raises(guards.KillSwitchActive):
        guards.check_kill_switch()
    with conn.cursor() as cur:
        cur.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")


def test_monthly_budget_raises_once_exceeded(conn):
    with conn.cursor() as cur:
        cur.execute("update config set value = '1.0'::jsonb where key = 'monthly_budget_usd'")
    ledger.record_cost_event(source="test", cost_usd=2.00)
    with pytest.raises(guards.MonthlyBudgetExceeded):
        guards.check_monthly_budget()


def test_run_budget_raises_when_the_cap_is_crossed(conn):
    budget = guards.RunBudget(limit_usd=Decimal("0.10"))
    budget.add(Decimal("0.06"))
    assert budget.spent == Decimal("0.06")
    with pytest.raises(guards.RunBudgetExceeded):
        budget.add(Decimal("0.06"))


def test_run_budget_reports_spend_on_exit(conn):
    with guards.RunBudget(limit_usd=Decimal("1.00")) as budget:
        budget.add(Decimal("0.30"))
    assert budget.spent == Decimal("0.30")
```

- [ ] **Step 2: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_guards.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.guards'`

- [ ] **Step 3: Implementar `guards.py`**

```python
# src/handoff_agent/guards.py
"""Budget guardrails.

An agentic loop with no ceiling can eat a month of budget in one bad night.
These are the three brakes from the spec: kill switch, monthly cap, per-run cap.
Kept apart from ledger.py on purpose — the ledger records, the guards decide,
and recording must never be able to block anything.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from . import db, ledger


class KillSwitchActive(RuntimeError):
    """The kill_switch row in config is true. Everything stops."""


class MonthlyBudgetExceeded(RuntimeError):
    """Spend this calendar month crossed the configured ceiling."""


class RunBudgetExceeded(RuntimeError):
    """One research run cost more than its cap. It is cut short, not retried."""


def _config_value(key: str, default):
    row = db.fetch_one("select value from config where key = %s", (key,))
    return default if row is None else row["value"]


def check_kill_switch() -> None:
    if _config_value("kill_switch", False) is True:
        raise KillSwitchActive("kill_switch is on; set it to false in the config table")


def monthly_spend() -> Decimal:
    now = datetime.now(UTC)
    return ledger.spend_since(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def check_monthly_budget() -> None:
    limit = Decimal(str(_config_value("monthly_budget_usd", 150)))
    spent = monthly_spend()
    if spent >= limit:
        raise MonthlyBudgetExceeded(f"spent ${spent} of ${limit} this month")


class RunBudget:
    """Caps a single research run. Use as a context manager around the loop."""

    def __init__(self, limit_usd: Decimal) -> None:
        self.limit_usd = limit_usd
        self.spent = Decimal("0")

    def add(self, cost: Decimal) -> None:
        self.spent += cost
        if self.spent > self.limit_usd:
            raise RunBudgetExceeded(
                f"run spent ${self.spent}, cap is ${self.limit_usd}"
            )

    def __enter__(self) -> RunBudget:
        check_kill_switch()
        check_monthly_budget()
        return self

    def __exit__(self, *exc_info) -> None:
        return None
```

- [ ] **Step 4: Ejecutar los tests y comprobar que pasan**

Run: `uv run pytest tests/test_guards.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/guards.py tests/test_guards.py
git commit -m "feat: kill switch, tope mensual y tope por ejecución"
```

---

### Task 7: Cliente LLM envuelto

**Files:**
- Create: `src/handoff_agent/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `ledger.record_llm_call`, `guards.check_kill_switch`, `load_settings()`
- Produces:
  - `LLMResponse` — dataclass con `text: str`, `cost_usd: Decimal`, `input_tokens: int`, `cached_tokens: int`, `output_tokens: int`, `latency_ms: int`
  - `complete(prompt: str, *, stage: str, system: str | None = None, prospect_id: str | None = None, json_mode: bool = False) -> LLMResponse`

- [ ] **Step 1: Escribir el test que falla**

```python
# tests/test_llm.py
from decimal import Decimal
from types import SimpleNamespace

import pytest

from handoff_agent import guards, llm


class FakeOpenAI:
    """Doble de la parte del SDK de OpenAI que usamos, y solo de esa parte."""

    def __init__(self, text="hola", input_tokens=1000, cached=0, output_tokens=500):
        self._text = text
        self._usage = SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        )
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._text))],
            usage=self._usage,
        )


def test_complete_returns_text_and_cost(conn, monkeypatch):
    fake = FakeOpenAI(text="respuesta")
    monkeypatch.setattr(llm, "_client", lambda: fake)

    result = llm.complete("¿quién es Ada Lovelace?", stage="research")

    assert result.text == "respuesta"
    assert result.cost_usd == Decimal("1000") / Decimal("1000000") * Decimal("2.00") + \
        Decimal("500") / Decimal("1000000") * Decimal("8.00")
    assert result.latency_ms >= 0


def test_complete_writes_one_row_to_the_ledger(conn, monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI())
    llm.complete("hola", stage="triage")
    with conn.cursor() as cur:
        cur.execute("select stage, input_tokens, output_tokens from llm_calls")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0] == ("triage", 1000, 500)


def test_complete_charges_cached_tokens_separately(conn, monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI(input_tokens=1000, cached=800))
    llm.complete("hola", stage="triage")
    with conn.cursor() as cur:
        cur.execute("select input_tokens, cached_tokens from llm_calls")
        # El SDK cuenta los cacheados dentro de prompt_tokens; los separamos
        # para no cobrar dos veces por el mismo token.
        assert cur.fetchone() == (200, 800)


def test_complete_refuses_to_run_when_the_kill_switch_is_on(conn, monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI())
    with conn.cursor() as cur:
        cur.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    with pytest.raises(guards.KillSwitchActive):
        llm.complete("hola", stage="triage")
    with conn.cursor() as cur:
        cur.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")


def test_json_mode_asks_openai_for_a_json_object(conn, monkeypatch):
    fake = FakeOpenAI(text='{"ok": true}')
    monkeypatch.setattr(llm, "_client", lambda: fake)
    llm.complete("dame json", stage="triage", json_mode=True)
    assert fake.calls[0]["response_format"] == {"type": "json_object"}
```

- [ ] **Step 2: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_llm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.llm'`

- [ ] **Step 3: Implementar `llm.py`**

```python
# src/handoff_agent/llm.py
"""The only way this codebase talks to an LLM.

Every call goes through here so that three things always happen: the kill
switch is honoured, the cost lands in the ledger, and the call shows up in
Langfuse. Calling the OpenAI SDK directly anywhere else is a bug.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from openai import OpenAI

from . import guards, ledger
from .config import load_settings


@dataclass(frozen=True)
class LLMResponse:
    text: str
    cost_usd: Decimal
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    latency_ms: int


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    settings = load_settings()
    return OpenAI(api_key=settings.openai_api_key, timeout=settings.http_timeout_seconds)


def complete(
    prompt: str,
    *,
    stage: str,
    system: str | None = None,
    prospect_id: str | None = None,
    json_mode: bool = False,
) -> LLMResponse:
    guards.check_kill_switch()
    settings = load_settings()

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {"model": settings.openai_model, "messages": messages}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    started = time.perf_counter()
    response = _client().chat.completions.create(**kwargs)
    latency_ms = int((time.perf_counter() - started) * 1000)

    usage = response.usage
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    # OpenAI reports cached tokens inside prompt_tokens. Split them so the
    # cheaper rate applies to exactly the tokens that were cached.
    fresh_input = max(usage.prompt_tokens - cached, 0)

    cost = ledger.record_llm_call(
        stage=stage,
        model=settings.openai_model,
        input_tokens=fresh_input,
        cached_tokens=cached,
        output_tokens=usage.completion_tokens,
        latency_ms=latency_ms,
        prospect_id=prospect_id,
    )

    return LLMResponse(
        text=response.choices[0].message.content or "",
        cost_usd=cost,
        input_tokens=fresh_input,
        cached_tokens=cached,
        output_tokens=usage.completion_tokens,
        latency_ms=latency_ms,
    )
```

- [ ] **Step 4: Ejecutar los tests y comprobar que pasan**

Run: `uv run pytest tests/test_llm.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/llm.py tests/test_llm.py
git commit -m "feat: cliente GPT-4.1 con kill switch y registro de coste obligatorio"
```

---

### Task 8: SearXNG y `buscar_web`

**Files:**
- Create: `docker-compose.searxng.yml`
- Create: `searxng/settings.yml`
- Create: `src/handoff_agent/tools/__init__.py`
- Create: `src/handoff_agent/tools/search.py`
- Test: `tests/test_search.py`

**Interfaces:**
- Consumes: `load_settings()`, `ledger.record_action`
- Produces:
  - `SearchResult` — dataclass con `title: str`, `url: str`, `snippet: str`
  - `buscar_web(query: str, limit: int = 8) -> list[SearchResult]`

- [ ] **Step 1: Crear la configuración de SearXNG**

SearXNG trae el formato JSON **desactivado por defecto**. Sin este archivo, la API devuelve HTML y `buscar_web` no funciona — es el fallo más común al montarlo.

```yaml
# searxng/settings.yml
use_default_settings: true
server:
  secret_key: "cambiar-en-produccion"
  limiter: false
search:
  formats:
    - html
    - json
```

```yaml
# docker-compose.searxng.yml
services:
  searxng:
    image: searxng/searxng:latest
    ports:
      - "8080:8080"
    volumes:
      - ./searxng:/etc/searxng:rw
    environment:
      - SEARXNG_BASE_URL=http://localhost:8080/
    restart: unless-stopped
```

- [ ] **Step 2: Escribir el test que falla**

```python
# tests/test_search.py
import httpx
import pytest
import respx

from handoff_agent.tools import search

SEARXNG = "http://127.0.0.1:8080"


@respx.mock
def test_buscar_web_maps_searxng_results(conn):
    respx.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(200, json={"results": [
            {"title": "Acme Corp", "url": "https://acme.com", "content": "We build things"},
            {"title": "Acme on LinkedIn", "url": "https://linkedin.com/company/acme", "content": ""},
        ]})
    )
    results = search.buscar_web("acme corp")
    assert [r.url for r in results] == ["https://acme.com", "https://linkedin.com/company/acme"]
    assert results[0].snippet == "We build things"


@respx.mock
def test_buscar_web_respects_the_limit(conn):
    respx.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(200, json={"results": [
            {"title": f"r{i}", "url": f"https://e{i}.com", "content": ""} for i in range(20)
        ]})
    )
    assert len(search.buscar_web("algo", limit=3)) == 3


@respx.mock
def test_buscar_web_logs_the_action(conn):
    respx.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(200, json={"results": []}))
    search.buscar_web("acme")
    with conn.cursor() as cur:
        cur.execute("select action, payload from agent_actions")
        row = cur.fetchone()
    assert row[0] == "buscar_web"
    assert row[1]["query"] == "acme"


@respx.mock
def test_buscar_web_raises_a_clear_error_when_searxng_is_down(conn):
    respx.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(502))
    with pytest.raises(search.SearchUnavailable, match="SearXNG"):
        search.buscar_web("acme")
```

- [ ] **Step 3: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.tools'`

- [ ] **Step 4: Implementar `search.py`**

```python
# src/handoff_agent/tools/search.py
"""Web search through a self-hosted SearXNG.

Self-hosted rather than Tavily/Serper/Exa: no API key, no per-query billing,
and the queries never leave our infrastructure.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from .. import ledger
from ..config import load_settings


class SearchUnavailable(RuntimeError):
    """SearXNG did not answer. Callers should degrade, not crash the pipeline."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


def buscar_web(query: str, limit: int = 8) -> list[SearchResult]:
    """Search the public web. Returns at most `limit` results, best first."""
    settings = load_settings()
    try:
        response = httpx.get(
            f"{settings.searxng_url}/search",
            params={"q": query, "format": "json"},
            timeout=settings.http_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchUnavailable(f"SearXNG did not answer: {exc}") from exc

    results = [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("content", ""),
        )
        for item in payload.get("results", [])[:limit]
    ]
    ledger.record_action(
        action="buscar_web", payload={"query": query, "limit": limit},
        result={"count": len(results)},
    )
    return results
```

- [ ] **Step 5: Crear `tools/__init__.py` y ejecutar los tests**

```bash
touch src/handoff_agent/tools/__init__.py
uv run pytest tests/test_search.py -v
```
Expected: PASS, 4 tests

- [ ] **Step 6: Levantar SearXNG y comprobarlo de verdad**

```bash
docker compose -f docker-compose.searxng.yml up -d
sleep 15
curl -s "http://127.0.0.1:8080/search?q=anthropic&format=json" | head -c 200
```
Expected: JSON que empieza por `{"query": "anthropic"` — si devuelve HTML, `searxng/settings.yml` no se montó.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.searxng.yml searxng/ src/handoff_agent/tools/ tests/test_search.py
git commit -m "feat: buscar_web sobre SearXNG self-hosted"
```

---

### Task 9: `leer_sitio`

**Files:**
- Create: `src/handoff_agent/tools/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `load_settings()`, `ledger.record_action`
- Produces:
  - `PageContent` — dataclass con `url: str`, `title: str`, `text: str`
  - `leer_sitio(url: str, max_chars: int = 20_000) -> PageContent`

**Decisión:** en el Plan 1 se usa httpx + trafilatura, no Crawl4AI. Trafilatura extrae texto limpio sin arrastrar Playwright, y cubre sitios corporativos y páginas de careers, que son estáticas en su inmensa mayoría. Crawl4AI entra en el Plan 2, detrás de esta misma firma, el día que aparezca un sitio renderizado por JavaScript. Añadir un navegador headless ahora sería pagar peso e inestabilidad por un caso que todavía no tenemos.

- [ ] **Step 1: Escribir el test que falla**

```python
# tests/test_web.py
import httpx
import pytest
import respx

from handoff_agent.tools import web

HTML = """
<html><head><title>Acme — Careers</title></head>
<body><article>
<h1>Join Acme</h1>
<p>We are hiring a Support Lead and two Ops Associates.</p>
</article></body></html>
"""


@respx.mock
def test_leer_sitio_extracts_readable_text(conn):
    respx.get("https://acme.com/careers").mock(
        return_value=httpx.Response(200, html=HTML)
    )
    page = web.leer_sitio("https://acme.com/careers")
    assert "Support Lead" in page.text
    assert "<p>" not in page.text
    assert page.url == "https://acme.com/careers"


@respx.mock
def test_leer_sitio_truncates_to_max_chars(conn):
    long_html = "<html><body><article><p>" + ("palabra " * 5000) + "</p></article></body></html>"
    respx.get("https://acme.com/long").mock(return_value=httpx.Response(200, html=long_html))
    page = web.leer_sitio("https://acme.com/long", max_chars=100)
    assert len(page.text) <= 100


@respx.mock
def test_leer_sitio_raises_on_http_error(conn):
    respx.get("https://acme.com/404").mock(return_value=httpx.Response(404))
    with pytest.raises(web.PageUnavailable):
        web.leer_sitio("https://acme.com/404")


@respx.mock
def test_leer_sitio_logs_the_action(conn):
    respx.get("https://acme.com/").mock(return_value=httpx.Response(200, html=HTML))
    web.leer_sitio("https://acme.com/")
    with conn.cursor() as cur:
        cur.execute("select action, payload from agent_actions")
        row = cur.fetchone()
    assert row[0] == "leer_sitio"
    assert row[1]["url"] == "https://acme.com/"
```

- [ ] **Step 2: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_web.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.tools.web'`

- [ ] **Step 3: Implementar `web.py`**

```python
# src/handoff_agent/tools/web.py
"""Fetch a page and return clean, LLM-ready text.

trafilatura strips navigation, cookie banners and boilerplate, which is most of
what a corporate site is made of. Feeding raw HTML to the model would cost
several times more per page and read worse.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
import trafilatura

from .. import ledger
from ..config import load_settings


class PageUnavailable(RuntimeError):
    """The page could not be fetched or held no extractable text."""


@dataclass(frozen=True)
class PageContent:
    url: str
    title: str
    text: str


def leer_sitio(url: str, max_chars: int = 20_000) -> PageContent:
    """Fetch `url` and return its readable text, truncated to `max_chars`."""
    settings = load_settings()
    try:
        response = httpx.get(
            url,
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "HandoffResearchBot/0.1"},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise PageUnavailable(f"could not fetch {url}: {exc}") from exc

    extracted = trafilatura.extract(response.text, include_comments=False) or ""
    metadata = trafilatura.extract_metadata(response.text)
    title = getattr(metadata, "title", None) or ""

    if not extracted.strip():
        raise PageUnavailable(f"no extractable text at {url}")

    text = extracted[:max_chars]
    ledger.record_action(
        action="leer_sitio", payload={"url": url},
        result={"chars": len(text), "title": title},
    )
    return PageContent(url=url, title=title, text=text)
```

- [ ] **Step 4: Ejecutar los tests y comprobar que pasan**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/tools/web.py tests/test_web.py
git commit -m "feat: leer_sitio con extracción de texto limpio vía trafilatura"
```

---

### Task 10: `buscar_ofertas`

**Files:**
- Create: `src/handoff_agent/tools/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: `ledger.record_action`
- Produces:
  - `JobPosting` — dataclass con `title: str`, `company: str`, `location: str`, `url: str`, `site: str`
  - `buscar_ofertas(company: str, limit: int = 20) -> list[JobPosting]`

**Por qué importa:** "¿esta empresa está contratando ahora mismo?" es literalmente la señal de compra de Handoff, y JobSpy la consigue de la superficie pública de invitado de LinkedIn — sin cuenta, sin ToS violado y sin coste.

- [ ] **Step 1: Escribir el test que falla**

```python
# tests/test_jobs.py
import pandas as pd

from handoff_agent.tools import jobs


def _frame(rows):
    return pd.DataFrame(rows)


def test_buscar_ofertas_maps_the_dataframe(conn, monkeypatch):
    monkeypatch.setattr(jobs, "_scrape", lambda **kw: _frame([
        {"title": "Support Lead", "company": "Acme", "location": "Remote",
         "job_url": "https://x/1", "site": "linkedin"},
        {"title": "Ops Associate", "company": "Acme", "location": "NYC",
         "job_url": "https://x/2", "site": "indeed"},
    ]))
    postings = jobs.buscar_ofertas("Acme")
    assert [p.title for p in postings] == ["Support Lead", "Ops Associate"]
    assert postings[0].site == "linkedin"


def test_buscar_ofertas_returns_empty_list_when_nothing_found(conn, monkeypatch):
    monkeypatch.setattr(jobs, "_scrape", lambda **kw: _frame([]))
    assert jobs.buscar_ofertas("Empresa Inexistente") == []


def test_buscar_ofertas_survives_a_scraper_failure(conn, monkeypatch):
    def boom(**kw):
        raise RuntimeError("linkedin blocked us")

    monkeypatch.setattr(jobs, "_scrape", boom)
    # Un bloqueo de los job boards no puede tumbar el research entero:
    # el dossier se sostiene igual sobre web y búsqueda.
    assert jobs.buscar_ofertas("Acme") == []


def test_buscar_ofertas_logs_the_action(conn, monkeypatch):
    monkeypatch.setattr(jobs, "_scrape", lambda **kw: _frame([]))
    jobs.buscar_ofertas("Acme")
    with conn.cursor() as cur:
        cur.execute("select action, payload from agent_actions")
        row = cur.fetchone()
    assert row[0] == "buscar_ofertas"
    assert row[1]["company"] == "Acme"
```

- [ ] **Step 2: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.tools.jobs'`

- [ ] **Step 3: Implementar `jobs.py`**

```python
# src/handoff_agent/tools/jobs.py
"""Open job postings for a company, via JobSpy.

Hiring is Handoff's buying signal, so this is the highest-value corroboration
in the dossier. JobSpy reads LinkedIn's public guest surface, so no LinkedIn
account is involved and nothing here risks an account ban.

Job boards block scrapers routinely. A failure here degrades the dossier; it
must never take down a research run.
"""
from __future__ import annotations

from dataclasses import dataclass

from jobspy import scrape_jobs

from .. import ledger


@dataclass(frozen=True)
class JobPosting:
    title: str
    company: str
    location: str
    url: str
    site: str


def _scrape(**kwargs):
    """Indirection so tests can substitute the scraper without network access."""
    return scrape_jobs(**kwargs)


def buscar_ofertas(company: str, limit: int = 20) -> list[JobPosting]:
    """Return open postings for `company`. Empty list if the boards block us."""
    try:
        frame = _scrape(
            site_name=["linkedin", "indeed"],
            search_term=company,
            results_wanted=limit,
        )
    except Exception as exc:  # noqa: BLE001 — degradar es la política aquí
        ledger.record_action(
            action="buscar_ofertas", payload={"company": company},
            result={"error": str(exc), "count": 0},
        )
        return []

    postings = [
        JobPosting(
            title=str(row.get("title", "")),
            company=str(row.get("company", "")),
            location=str(row.get("location", "")),
            url=str(row.get("job_url", "")),
            site=str(row.get("site", "")),
        )
        for _, row in frame.iterrows()
    ][:limit]

    ledger.record_action(
        action="buscar_ofertas", payload={"company": company, "limit": limit},
        result={"count": len(postings)},
    )
    return postings
```

- [ ] **Step 4: Ejecutar los tests y comprobar que pasan**

Run: `uv run pytest tests/test_jobs.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/tools/jobs.py tests/test_jobs.py
git commit -m "feat: buscar_ofertas vía JobSpy, degradando si los job boards bloquean"
```

---

### Task 11: `historial_prospecto`

**Files:**
- Create: `src/handoff_agent/tools/prospects.py`
- Test: `tests/test_prospects.py`

**Interfaces:**
- Consumes: `db.fetch_one`, `db.fetch_all`
- Produces:
  - `upsert_prospect(slack_user_id: str, full_name: str | None = None) -> dict` — crea o devuelve la fila existente
  - `historial_prospecto(slack_user_id: str) -> dict` — `{"prospect": dict | None, "dossier": dict | None, "actions": list[dict]}`
  - `save_dossier(prospect_id: str, content: dict, sources: list) -> int` — devuelve la versión creada

**Por qué existe:** es la herramienta que responde "¿ya vimos a esta persona y qué hicimos con ella?". Sin ella no hay deduplicación, no hay descarte pegajoso y no hay bucle de aprendizaje.

- [ ] **Step 1: Escribir el test que falla**

```python
# tests/test_prospects.py
from handoff_agent.tools import prospects


def test_upsert_creates_a_prospect_once(conn):
    first = prospects.upsert_prospect("U1", full_name="Ada")
    second = prospects.upsert_prospect("U1")
    assert first["id"] == second["id"]
    with conn.cursor() as cur:
        cur.execute("select count(*) from prospects")
        assert cur.fetchone()[0] == 1


def test_upsert_does_not_overwrite_an_existing_name(conn):
    prospects.upsert_prospect("U1", full_name="Ada")
    again = prospects.upsert_prospect("U1", full_name=None)
    assert again["full_name"] == "Ada"


def test_historial_returns_none_for_an_unknown_person(conn):
    history = prospects.historial_prospecto("U-desconocido")
    assert history["prospect"] is None
    assert history["dossier"] is None
    assert history["actions"] == []


def test_save_dossier_increments_the_version(conn):
    person = prospects.upsert_prospect("U1")
    assert prospects.save_dossier(person["id"], {"summary": "v1"}, []) == 1
    assert prospects.save_dossier(person["id"], {"summary": "v2"}, []) == 2


def test_historial_returns_the_latest_dossier(conn):
    person = prospects.upsert_prospect("U1")
    prospects.save_dossier(person["id"], {"summary": "viejo"}, [])
    prospects.save_dossier(person["id"], {"summary": "nuevo"}, [])
    history = prospects.historial_prospecto("U1")
    assert history["dossier"]["content"]["summary"] == "nuevo"
    assert history["dossier"]["version"] == 2


def test_historial_includes_recent_actions(conn):
    from handoff_agent import ledger

    person = prospects.upsert_prospect("U1")
    ledger.record_action("buscar_web", {"query": "acme"}, prospect_id=person["id"])
    history = prospects.historial_prospecto("U1")
    assert history["actions"][0]["action"] == "buscar_web"
```

- [ ] **Step 2: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_prospects.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.tools.prospects'`

- [ ] **Step 3: Implementar `prospects.py`**

```python
# src/handoff_agent/tools/prospects.py
"""Everything the agent knows about a person before it decides anything.

The person, not the message, is the unit of work: research is paid for once and
every later message is scored against the dossier this returns.
"""
from __future__ import annotations

import json

from .. import db

RECENT_ACTIONS_LIMIT = 20


def upsert_prospect(slack_user_id: str, full_name: str | None = None) -> dict:
    """Return the person's row, creating it on first sight.

    Never overwrites a known name with NULL: a later sighting with less
    information must not erase what an earlier one established.
    """
    return db.fetch_one(
        """
        insert into prospects (slack_user_id, full_name)
        values (%s, %s)
        on conflict (slack_user_id) do update
            set full_name = coalesce(excluded.full_name, prospects.full_name),
                updated_at = now()
        returning *
        """,
        (slack_user_id, full_name),
    )


def save_dossier(prospect_id: str, content: dict, sources: list) -> int:
    """Store a new dossier version for the person. Returns the version number."""
    row = db.fetch_one(
        """
        insert into dossiers (prospect_id, version, content, sources)
        values (
            %s,
            (select coalesce(max(version), 0) + 1 from dossiers where prospect_id = %s),
            %s, %s
        )
        returning version
        """,
        (prospect_id, prospect_id, json.dumps(content), json.dumps(sources)),
    )
    return row["version"]


def historial_prospecto(slack_user_id: str) -> dict:
    """What we already know: the person, their latest dossier, recent actions."""
    prospect = db.fetch_one(
        "select * from prospects where slack_user_id = %s", (slack_user_id,)
    )
    if prospect is None:
        return {"prospect": None, "dossier": None, "actions": []}

    dossier = db.fetch_one(
        "select * from dossiers where prospect_id = %s order by version desc limit 1",
        (prospect["id"],),
    )
    actions = db.fetch_all(
        "select action, payload, result, created_at from agent_actions "
        "where prospect_id = %s order by created_at desc limit %s",
        (prospect["id"], RECENT_ACTIONS_LIMIT),
    )
    return {"prospect": prospect, "dossier": dossier, "actions": actions}
```

- [ ] **Step 4: Ejecutar los tests y comprobar que pasan**

Run: `uv run pytest tests/test_prospects.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/tools/prospects.py tests/test_prospects.py
git commit -m "feat: historial_prospecto, upsert por persona y dossiers versionados"
```

---

### Task 12: Servidor MCP `handoff-tools`

**Files:**
- Create: `src/handoff_agent/mcp_server.py`
- Modify: `pyproject.toml` (añadir el script de consola)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `buscar_web`, `leer_sitio`, `buscar_ofertas`, `historial_prospecto` de las tareas 8-11
- Produces: servidor MCP `handoff-tools` con cuatro herramientas del mismo nombre, más `resumen_costes`

**Por qué MCP y no funciones sueltas:** un solo contrato con tres consumidores — el pipeline automático por function-calling, el equipo depurando desde Claude Code, y el CEO Command Center reutilizándolas más adelante sin reescribir nada.

- [ ] **Step 1: Escribir el test que falla**

```python
# tests/test_mcp_server.py
import pytest

from handoff_agent import mcp_server


@pytest.mark.asyncio
async def test_server_exposes_the_expected_tools():
    tools = await mcp_server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {"buscar_web", "leer_sitio", "buscar_ofertas",
            "historial_prospecto", "resumen_costes"} <= names


@pytest.mark.asyncio
async def test_every_tool_has_a_description():
    tools = await mcp_server.mcp.list_tools()
    undocumented = [t.name for t in tools if not t.description]
    assert undocumented == [], f"herramientas sin descripción: {undocumented}"


def test_resumen_costes_reports_zero_on_an_empty_ledger(conn):
    summary = mcp_server.resumen_costes(days=30)
    assert summary["total_usd"] == "0.00"
    assert summary["llm_calls"] == 0


def test_resumen_costes_adds_up_both_ledgers(conn):
    from handoff_agent import ledger

    ledger.record_llm_call(
        stage="research", model="gpt-4.1",
        input_tokens=1_000_000, cached_tokens=0, output_tokens=0,
    )  # $2.00
    ledger.record_cost_event(source="twilio", cost_usd=0.50)
    summary = mcp_server.resumen_costes(days=30)
    assert summary["total_usd"] == "2.50"
    assert summary["llm_calls"] == 1
```

- [ ] **Step 2: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.mcp_server'`

- [ ] **Step 3: Implementar `mcp_server.py`**

```python
# src/handoff_agent/mcp_server.py
"""MCP server exposing the research toolbox.

One contract, three consumers: the automated pipeline via function calling,
the team debugging from Claude Code, and the CEO Command Center later on.
Tool names are Spanish because they are the team's interface.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from mcp.server.fastmcp import FastMCP

from . import db, ledger
from .tools import jobs, prospects, search, web

mcp = FastMCP("handoff-tools")


@mcp.tool()
def buscar_web(query: str, limit: int = 8) -> list[dict]:
    """Busca en la web pública vía SearXNG. Devuelve título, url y extracto."""
    return [asdict(r) for r in search.buscar_web(query, limit=limit)]


@mcp.tool()
def leer_sitio(url: str, max_chars: int = 20_000) -> dict:
    """Descarga una página y devuelve su texto legible, sin navegación ni banners."""
    return asdict(web.leer_sitio(url, max_chars=max_chars))


@mcp.tool()
def buscar_ofertas(company: str, limit: int = 20) -> list[dict]:
    """Ofertas de trabajo abiertas de una empresa. Lista vacía si los boards bloquean."""
    return [asdict(p) for p in jobs.buscar_ofertas(company, limit=limit)]


@mcp.tool()
def historial_prospecto(slack_user_id: str) -> dict:
    """Qué sabemos ya de esta persona: ficha, último dossier y acciones recientes."""
    history = prospects.historial_prospecto(slack_user_id)
    return {
        "prospect": _jsonable(history["prospect"]),
        "dossier": _jsonable(history["dossier"]),
        "actions": [_jsonable(a) for a in history["actions"]],
    }


@mcp.tool()
def resumen_costes(days: int = 30) -> dict:
    """Gasto de los últimos N días, sumando llamadas a LLM y costes externos."""
    since = datetime.now(UTC) - timedelta(days=days)
    total = ledger.spend_since(since)
    counts = db.fetch_one(
        "select (select count(*) from llm_calls where created_at >= %s) as llm_calls, "
        "       (select count(*) from cost_events where created_at >= %s) as cost_events",
        (since, since),
    )
    return {
        "days": days,
        "total_usd": f"{total:.2f}",
        "llm_calls": counts["llm_calls"],
        "cost_events": counts["cost_events"],
    }


def _jsonable(row: dict | None) -> dict | None:
    """Datetimes and UUIDs do not survive JSON on their own."""
    if row is None:
        return None
    return {k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if hasattr(v, "hex") else v)
            for k, v in row.items()}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Añadir el script de consola a `pyproject.toml`**

```toml
[project.scripts]
handoff-tools = "handoff_agent.mcp_server:main"
```

Y en `[tool.pytest.ini_options]`, añadir `asyncio_mode = "auto"`.

- [ ] **Step 5: Ejecutar los tests y comprobar que pasan**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS, 4 tests

- [ ] **Step 6: Comprobar la suite entera**

Run: `uv run pytest -v`
Expected: PASS, todo verde

- [ ] **Step 7: Registrar el servidor en Claude Code y probarlo a mano**

```bash
claude mcp add handoff-tools -- uv run --directory "$(pwd)" handoff-tools
```

Comprobación manual: en una sesión de Claude Code, pedir `resumen_costes` y ver que devuelve el gasto real de la base de datos.

- [ ] **Step 8: Commit**

```bash
git add src/handoff_agent/mcp_server.py pyproject.toml tests/test_mcp_server.py
git commit -m "feat: servidor MCP handoff-tools con el toolbox de research y costes"
```

---

### Task 13: Trazado opcional a Langfuse

**Files:**
- Create: `src/handoff_agent/tracing.py`
- Modify: `src/handoff_agent/llm.py`
- Test: `tests/test_tracing.py`

**Interfaces:**
- Consumes: `load_settings()`
- Produces:
  - `is_enabled() -> bool` — cierto solo si hay claves de Langfuse configuradas
  - `trace_llm_call(stage, model, prompt, completion, usage, latency_ms, prospect_id) -> str | None` — devuelve el `trace_id` o `None`

**Por qué así:** el spec pide Langfuse para el árbol de ejecución, pero las claves no llegan hasta que se cree la cuenta. Sin claves el trazado es un no-op silencioso: el pipeline funciona igual y el ledger de Supabase, que es la fuente de verdad, no depende de un tercero. Cuando aparezcan las claves, se activa sin tocar código.

- [ ] **Step 1: Escribir el test que falla**

```python
# tests/test_tracing.py
from handoff_agent import tracing


def test_tracing_is_disabled_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert tracing.is_enabled() is False


def test_tracing_is_enabled_with_both_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    assert tracing.is_enabled() is True


def test_trace_llm_call_is_a_noop_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert tracing.trace_llm_call(
        stage="triage", model="gpt-4.1", prompt="hola", completion="adios",
        usage={"input": 1, "cached": 0, "output": 1}, latency_ms=10,
    ) is None


def test_llm_complete_still_works_when_tracing_is_off(conn, monkeypatch):
    """El trazado nunca puede tumbar una llamada: Supabase es la fuente de verdad."""
    from tests.test_llm import FakeOpenAI
    from handoff_agent import llm

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI())
    assert llm.complete("hola", stage="triage").text == "hola"
```

- [ ] **Step 2: Ejecutar el test y comprobar que falla**

Run: `uv run pytest tests/test_tracing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.tracing'`

- [ ] **Step 3: Implementar `tracing.py`**

```python
# src/handoff_agent/tracing.py
"""Optional Langfuse tracing.

Supabase holds the numbers; Langfuse holds the execution tree that explains
them. Tracing is best-effort by design: if Langfuse is unreachable or
unconfigured, the pipeline carries on and the ledger is still complete.
"""
from __future__ import annotations

import logging

from .config import load_settings

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    settings = load_settings()
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def trace_llm_call(
    *,
    stage: str,
    model: str,
    prompt: str,
    completion: str,
    usage: dict,
    latency_ms: int,
    prospect_id: str | None = None,
) -> str | None:
    """Send one generation to Langfuse. Returns its trace id, or None."""
    if not is_enabled():
        return None

    settings = load_settings()
    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        trace = client.trace(name=stage, metadata={"prospect_id": prospect_id})
        trace.generation(
            name=stage, model=model, input=prompt, output=completion,
            usage_details={
                "input": usage.get("input", 0),
                "cache_read_input_tokens": usage.get("cached", 0),
                "output": usage.get("output", 0),
            },
            metadata={"latency_ms": latency_ms},
        )
        client.flush()
        return trace.id
    except Exception as exc:  # noqa: BLE001 — el trazado nunca rompe el pipeline
        logger.warning("langfuse tracing failed, continuing: %s", exc)
        return None
```

- [ ] **Step 4: Conectar el trazado en `llm.py`**

Sustituir el bloque que calcula el coste por este, que traza antes de registrar y guarda el `trace_id` en la misma fila del ledger:

```python
    trace_id = tracing.trace_llm_call(
        stage=stage,
        model=settings.openai_model,
        prompt=prompt,
        completion=response.choices[0].message.content or "",
        usage={"input": fresh_input, "cached": cached, "output": usage.completion_tokens},
        latency_ms=latency_ms,
        prospect_id=prospect_id,
    )

    cost = ledger.record_llm_call(
        stage=stage,
        model=settings.openai_model,
        input_tokens=fresh_input,
        cached_tokens=cached,
        output_tokens=usage.completion_tokens,
        latency_ms=latency_ms,
        prospect_id=prospect_id,
        trace_id=trace_id,
    )
```

Y añadir `tracing` al import: `from . import guards, ledger, tracing`.

- [ ] **Step 5: Ejecutar la suite entera**

Run: `uv run pytest -v`
Expected: PASS, todo verde

- [ ] **Step 6: Commit**

```bash
git add src/handoff_agent/tracing.py src/handoff_agent/llm.py tests/test_tracing.py
git commit -m "feat: trazado opcional a Langfuse, no-op sin claves configuradas"
```

---

## Definición de terminado

- `uv run pytest` pasa entero.
- `supabase db reset` reconstruye el esquema desde cero sin intervención manual.
- El servidor MCP responde a las cinco herramientas desde Claude Code.
- Investigar a una persona a mano deja rastro en `agent_actions` y coste en `llm_calls`.
- `.env` no está en el repo y `.env.example` documenta todas las claves.

## Lo que este plan deja fuera a propósito

- Slack: no se toca nada en el Plan 2.
- Crawl4AI: entra cuando aparezca un sitio que lo exija, detrás de `leer_sitio`.
- Políticas RLS del panel: Plan 4. Aquí RLS queda activado y denegando por defecto.
- El bucle agéntico que encadena herramientas: Plan 2. Aquí las herramientas existen y se prueban sueltas.
- Las herramientas MCP `puntuar_señal`, `redactar_acercamiento` y `reencolar_research` (Plan 3) y `deals_cerrados_similares` (Plan 2, necesita los datos históricos de Anthony). Las cuatro dependen del cerebro de detección, que aquí no existe.
- Detección de stack tecnológico con `webappanalyzer`: es corroboración secundaria del dossier y no cambia ninguna decisión de scoring. Entra en el Plan 2 si aporta.
