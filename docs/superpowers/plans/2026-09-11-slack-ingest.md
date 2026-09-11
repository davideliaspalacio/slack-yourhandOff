# Plan 2b — Búsqueda fiable e ingesta desde Slack

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que el agente lea el Slack del Founders Club cada hora, detecte a quien escribe o entra nuevo, y encole e investigue a cada persona una sola vez con una búsqueda que no dependa de un buscador que nos bloquea.

**Architecture:** Un lector de Slack de solo lectura (`slack_client.py`) alimenta tres piezas en `src/handoff_agent/ingest/`: el `watcher` guarda mensajes y compara padrones, el `resolver` decide qué hacer con cada mensaje y encola research, y el `research_runner` toma tareas de una cola en Postgres (`FOR UPDATE SKIP LOCKED`, una tarea abierta por persona) y llama al `research_person` que ya existe. Un bucle (`handoff worker`) lo orquesta. La búsqueda pasa a usar Brave Search API cuando hay clave y SearXNG como respaldo.

**Tech Stack:** Lo de los planes 1 y 2a, más `slack-sdk` 3.44 (ya añadido en `pyproject.toml`).

**Base:** rama `feat/slack-ingest` desde `main` @ 5e817b2.

## Global Constraints

Siguen vigentes todas las del Plan 1 y el Plan 2a. En particular:

- **Ninguna llamada a un servicio de pago sin pasar por el ledger.** Cada consulta cobrada de Brave escribe una fila en `cost_events`.
- **Todo cambio de esquema es un archivo en `supabase/migrations/`.** En la base de desarrollo se aplica con `supabase migration up`, **nunca** con `supabase db reset`, que borraría los datos de desarrollo.
- **RLS activado en toda tabla nueva.**
- **Los tests nunca tocan la base de desarrollo** ni llaman a servicios reales: Slack, Brave, OpenAI y el webhook se falsean siempre.
- **Nunca se investiga a una persona `descartado`. Nunca se tira un dossier válido ya pagado.**

Nuevas en este plan:

- **El agente nunca escribe en el Slack del Founders Club.** El lector solo expone lecturas (`owner_id`, `history`, `members`, `user_profile`) y un test fija esa superficie. Las alertas operativas van al Slack de Handoff por un webhook distinto.
- **Sin backfill.** La primera lectura de un canal mira solo `SLACK_LOOKBACK_HOURS` hacia atrás, y el primer padrón es una línea base: nunca se encola a todos los miembros.
- **Bots, mensajes de sistema y el propio dueño del token nunca disparan research.**
- **Una sola tarea de research abierta por persona.** Dos disparadores a la vez no pagan dos investigaciones.
- **Un fallo de autenticación de Slack nunca es silencioso:** log CRITICAL, fila en `agent_actions` y aviso por webhook si está configurado.
- **Las paradas del sistema (kill switch, tope mensual) devuelven la tarea a la cola sin gastar un intento.**

---

## Estructura de archivos

```
searxng/settings.yml                 más motores activos; Google CSE fuera
supabase/migrations/0004_slack_ingest.sql
src/handoff_agent/
  config.py                          + Brave, Slack y webhook
  tools/search.py                    Brave primero, SearXNG de respaldo
  research/gather.py                 dominio adivinado más estricto + domain_guessed
  research/followup.py               conserva domain_guessed
  research/synthesize.py             avisa al modelo si el dominio es adivinado
  slack_client.py                    lector de solo lectura del Founders Club
  ops_alerts.py                      alertas operativas (log, ledger, webhook)
  cli.py                             + vigilar, cola, worker
  ingest/
    __init__.py
    queue.py                         cola de research en Postgres
    watcher.py                       mensajes nuevos y miembros nuevos
    resolver.py                      qué hacer con cada mensaje
    profile_hints.py                 empresa desde el título, dominio desde el email
    research_runner.py               una tarea de la cola → research_person
    loop.py                          el bucle del worker
tests/
  slack_fakes.py                     FakeReader compartido
  test_slack_client.py  test_ops_alerts.py  test_queue.py  test_watcher.py
  test_resolver.py  test_profile_hints.py  test_research_runner.py
  test_loop.py  test_ingest_e2e.py
```

**Fuera de este plan:** respuestas dentro de hilos (`conversations.replies`), scoring de mensajes, permalinks y tarjetas (Plan 3), lock por persona para las ejecuciones manuales desde la CLI (el camino automático ya lo cubre la cola), poda de `member_snapshots`.

---

### Task 1: Búsqueda — Brave primero, SearXNG de respaldo

**Files:**
- Modify: `src/handoff_agent/config.py`, `src/handoff_agent/tools/search.py`, `searxng/settings.yml`, `.env.example`, `tests/conftest.py`
- Test: `tests/test_search.py`

**Interfaces:**
- Produces: `Settings.brave_search_api_key: str | None`, `Settings.price_brave_per_query: float`; `buscar_web(query, limit=8, prospect_id=None) -> list[SearchResult]` con la misma firma que hoy; `BRAVE_URL`, `BRAVE_MAX_COUNT = 20`.

**Por qué:** SearXNG raspa las páginas de resultados de otros buscadores desde nuestra IP. En la prueba real del 2026-09-10 DuckDuckGo respondió con CAPTCHA (29 veces) y los motores quedaron suspendidos hasta una hora. Desde una IP de centro de datos (Railway) pasará antes. Brave Search API es oficial, sin CAPTCHA y con precio por consulta: desde febrero de 2026 no tiene plan gratuito, solo unos créditos mensuales, así que cada consulta va al ledger.

- [ ] **Step 1: Que ninguna variable de entorno real se cuele en los tests**

En `tests/conftest.py`, dentro del fixture `test_environment`, después de las dos líneas `monkeypatch.setenv(...)`:

```python
    # Si algún día hay claves reales en .env, load_dotenv las mete en el entorno
    # y los tests acabarían llamando a Brave, a Slack o al webhook de verdad.
    for name in ENV_THAT_MUST_NOT_LEAK:
        monkeypatch.delenv(name, raising=False)
```

y a nivel de módulo, junto a `TABLES_TO_CLEAN`:

```python
ENV_THAT_MUST_NOT_LEAK = [
    "BRAVE_SEARCH_API_KEY",
    "SLACK_USER_TOKEN",
    "SLACK_CHANNEL_IDS",
    "HANDOFF_ALERT_WEBHOOK_URL",
]
```

- [ ] **Step 2: Escribir los tests que fallan**

Añadir a `tests/test_search.py` (y `from decimal import Decimal` arriba):

```python
BRAVE = "https://api.search.brave.com/res/v1/web/search"


@pytest.fixture
def brave_key(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")


@respx.mock
def test_brave_is_used_when_its_key_is_configured(conn, brave_key):
    route = respx.get(BRAVE).mock(
        return_value=httpx.Response(200, json={"web": {"results": [
            {"title": "Acme", "url": "https://acme.com", "description": "We build things"},
        ]}})
    )
    results = search.buscar_web("acme", limit=5)
    assert [r.url for r in results] == ["https://acme.com"]
    assert results[0].snippet == "We build things"
    assert route.calls[0].request.headers["X-Subscription-Token"] == "brave-test-key"


@respx.mock
def test_every_brave_query_lands_in_the_cost_ledger(conn, brave_key):
    respx.get(BRAVE).mock(return_value=httpx.Response(200, json={"web": {"results": []}}))
    search.buscar_web("acme")
    with conn.cursor() as cur:
        cur.execute("select source, cost_usd from cost_events")
        source, cost = cur.fetchone()
    assert source == "brave_search"
    assert Decimal(cost) == Decimal("0.005000")


@respx.mock
def test_brave_asks_for_at_most_twenty_results(conn, brave_key):
    route = respx.get(BRAVE).mock(return_value=httpx.Response(200, json={"web": {"results": []}}))
    search.buscar_web("acme", limit=50)
    assert route.calls[0].request.url.params["count"] == "20"


@respx.mock
def test_a_brave_failure_falls_back_to_searxng_and_is_not_billed(conn, brave_key):
    respx.get(BRAVE).mock(return_value=httpx.Response(503))
    respx.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(200, json={"results": [
            {"title": "Acme", "url": "https://acme.com", "content": ""},
        ]})
    )
    assert [r.url for r in search.buscar_web("acme")] == ["https://acme.com"]
    with conn.cursor() as cur:
        cur.execute("select payload, result from agent_actions where action = 'buscar_web'")
        payload, result = cur.fetchone()
        cur.execute("select count(*) from cost_events")
        billed = cur.fetchone()[0]
    assert payload["proveedor"] == "searxng"
    assert "brave" in result["fallo_brave"]
    assert billed == 0


@respx.mock
def test_both_providers_down_is_a_search_outage_naming_both(conn, brave_key):
    respx.get(BRAVE).mock(return_value=httpx.Response(503))
    respx.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(502))
    with pytest.raises(search.SearchUnavailable, match="brave.*SearXNG"):
        search.buscar_web("acme")


@respx.mock
def test_without_a_brave_key_brave_is_never_called(conn):
    brave = respx.get(BRAVE).mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(200, json={"results": []}))
    search.buscar_web("acme")
    assert not brave.called
```

- [ ] **Step 3: Ejecutarlos y comprobar que fallan**

Run: `uv run pytest tests/test_search.py -v`
Expected: FAIL en los tests de Brave (no existe `BRAVE_SEARCH_API_KEY` en `Settings`, nadie llama a Brave).

- [ ] **Step 4: Configuración**

En `src/handoff_agent/config.py`, añadir al final de `Settings`:

```python
    brave_search_api_key: str | None
    price_brave_per_query: float
```

y en `load_settings()`:

```python
        brave_search_api_key=os.environ.get("BRAVE_SEARCH_API_KEY") or None,
        price_brave_per_query=float(os.environ.get("PRICE_BRAVE_PER_QUERY", "0.005")),
```

En `.env.example`, tras el bloque de SearXNG:

```bash
# Brave Search API — opcional. Si hay clave, se usa antes que SearXNG.
# Desde feb-2026 no tiene plan gratuito (créditos mensuales y pago por consulta):
# contrastar el precio en api-dashboard.search.brave.com.
BRAVE_SEARCH_API_KEY=
PRICE_BRAVE_PER_QUERY=0.005
```

- [ ] **Step 5: Reescribir `tools/search.py`**

```python
"""Web search: Brave Search API when configured, self-hosted SearXNG otherwise.

SearXNG scrapes other engines' result pages from our own IP. Under load those
engines answer with CAPTCHA and SearXNG goes quiet: it happened in the first
real batch (2026-09-10), and from a datacenter IP it happens sooner. Brave's
official API has no CAPTCHA and a predictable price, so it goes first when a
key is set, and SearXNG stays as the free fallback.

Brave bills per query, so every successful call writes a cost_events row: no
paid call may bypass the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .. import ledger
from ..config import Settings, load_settings

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_MAX_COUNT = 20


class SearchUnavailable(RuntimeError):
    """No provider answered. Callers should degrade, not crash the pipeline."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


def _describe_engine(entry) -> str:
    """SearXNG da cada motor como un par [motor, motivo]."""
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        return f"{entry[0]} ({entry[1]})"
    return str(entry)


def _search_brave(query: str, limit: int, settings: Settings) -> list[SearchResult]:
    response = httpx.get(
        BRAVE_URL,
        params={"q": query, "count": min(limit, BRAVE_MAX_COUNT)},
        headers={
            "X-Subscription-Token": settings.brave_search_api_key,
            "Accept": "application/json",
        },
        timeout=settings.http_timeout_seconds,
    )
    response.raise_for_status()
    items = (response.json().get("web") or {}).get("results") or []
    return [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("description", ""),
        )
        for item in items[:limit]
    ]


def _search_searxng(query: str, limit: int, settings: Settings) -> list[SearchResult]:
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

    items = payload.get("results") or []
    stalled = payload.get("unresponsive_engines") or []
    if not items and stalled:
        # Con los motores vetados (CAPTCHA) SearXNG contesta 200 y cero
        # resultados. Eso no es "no hay nada": es que no hubo búsqueda.
        raise SearchUnavailable(
            "SearXNG sin resultados; motores sin respuesta: "
            + ", ".join(_describe_engine(entry) for entry in stalled)
        )
    return [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("content", ""),
        )
        for item in items[:limit]
    ]


def buscar_web(query: str, limit: int = 8, prospect_id: str | None = None) -> list[SearchResult]:
    """Search the public web. Returns at most `limit` results, best first.

    `prospect_id` attributes the action (and any cost) to a person so it shows
    up in that person's history; without it the action is logged unattributed.
    """
    settings = load_settings()
    provider = "searxng"
    brave_failure: str | None = None
    results: list[SearchResult] | None = None

    if settings.brave_search_api_key:
        try:
            results = _search_brave(query, limit, settings)
        except (httpx.HTTPError, ValueError) as exc:
            brave_failure = f"brave: {exc}"
        else:
            provider = "brave"
            ledger.record_cost_event(
                source="brave_search",
                cost_usd=settings.price_brave_per_query,
                description=f"búsqueda web: {query[:80]}",
                prospect_id=prospect_id,
            )

    if results is None:
        try:
            results = _search_searxng(query, limit, settings)
        except SearchUnavailable as exc:
            if brave_failure:
                raise SearchUnavailable(f"{brave_failure}; {exc}") from exc
            raise

    outcome = {"count": len(results)}
    if brave_failure:
        outcome["fallo_brave"] = brave_failure
    ledger.record_action(
        action="buscar_web",
        payload={"query": query, "limit": limit, "proveedor": provider},
        result=outcome,
        prospect_id=prospect_id,
    )
    return results
```

- [ ] **Step 6: Más motores en SearXNG**

La imagen trae Bing, Mojeek y Qwant desactivados y Google CSE activo pero sin configurar (no responde nunca). Sustituir `searxng/settings.yml` por:

```yaml
use_default_settings: true
server:
  # secret_key NO va aquí: este archivo está en el repo y una clave commiteada
  # la comparten todos los despliegues hechos desde él. SearXNG la toma de la
  # variable de entorno SEARXNG_SECRET, definida en .env.
  limiter: false
search:
  formats:
    - html
    - json
# Repartir la carga entre más motores retrasa el CAPTCHA de cada uno.
engines:
  - name: bing
    disabled: false
  - name: mojeek
    disabled: false
  - name: qwant
    disabled: false
  - name: google cse
    disabled: true
```

Recrear el contenedor y comprobarlo:

```bash
set -a; source .env; set +a
docker compose -f docker-compose.searxng.yml up -d --force-recreate
sleep 20
curl -s http://127.0.0.1:8080/config | python3 -c "import sys,json; e={x['name']:x['enabled'] for x in json.load(sys.stdin)['engines']}; print({k: e.get(k) for k in ['bing','mojeek','qwant','google cse','brave','duckduckgo']})"
```
Expected: `bing`, `mojeek`, `qwant` y `brave` a `True`; `google cse` a `False`.

- [ ] **Step 7: Ejecutar la suite y comprobar que pasa**

Run: `uv run pytest -q`
Expected: PASS, todo verde.

- [ ] **Step 8: Commit**

```bash
git add src/handoff_agent/config.py src/handoff_agent/tools/search.py searxng/settings.yml .env.example tests/conftest.py tests/test_search.py
git commit -m "feat: Brave Search API como buscador principal, SearXNG de respaldo y más motores"
```

---

### Task 2: Dominio adivinado más estricto

**Files:**
- Modify: `src/handoff_agent/research/gather.py`, `src/handoff_agent/research/followup.py`, `src/handoff_agent/research/synthesize.py`
- Test: `tests/test_gather.py`, `tests/test_followup.py`, `tests/test_synthesize.py`

**Interfaces:**
- Consumes: `jobs.LEGAL_SUFFIXES` (ya existe en `tools/jobs.py`).
- Produces: `company_tokens(company: str) -> list[str]`; `resolve_domain(company, prospect_id, tally=None)` con la misma firma; `Gathered.domain_guessed: bool = False` (último campo, con valor por defecto).

**Por qué:** hoy `resolve_domain` se queda con el primer resultado que no está en la lista de directorios. Puede ser un periódico, un blog o una empresa homónima, y sus páginas acaban etiquetadas como `home`, `about` y `careers` de la persona. En el Plan 2b los perfiles de Slack casi nunca traen dominio, así que este camino pasa a ser el principal.

- [ ] **Step 1: Escribir los tests que fallan**

Añadir a `tests/test_gather.py`:

```python
def test_resolve_domain_rejects_sites_that_are_not_the_company(monkeypatch):
    monkeypatch.setattr(g.search, "buscar_web", fake_search(default=[
        SearchResult("Acme raises $20M - TechCrunch", "https://techcrunch.com/acme", ""),
        SearchResult("Some blog", "https://randomblog.io/acme-review", "Acme review"),
        SearchResult("Acme — Home", "https://acme.io/", ""),
    ]))
    assert g.resolve_domain("Acme", "pid") == "acme.io"


def test_resolve_domain_returns_none_when_no_result_is_the_company(monkeypatch):
    monkeypatch.setattr(g.search, "buscar_web", fake_search(default=[
        SearchResult("Top 10 CRMs", "https://randomblog.io/crm", ""),
    ]))
    assert g.resolve_domain("Northwind Ops", "pid") is None


def test_multi_word_companies_match_their_compact_domain(monkeypatch):
    monkeypatch.setattr(g.search, "buscar_web", fake_search(default=[
        SearchResult("Customer support software", "https://www.helpscout.com/", ""),
    ]))
    assert g.resolve_domain("Help Scout", "pid") == "helpscout.com"


def test_a_company_site_can_match_by_page_title(monkeypatch):
    monkeypatch.setattr(g.search, "buscar_web", fake_search(default=[
        SearchResult("Northwind Ops | Logistics software", "https://nwops.com/", ""),
    ]))
    assert g.resolve_domain("Northwind Ops", "pid") == "nwops.com"


def test_legal_suffixes_do_not_have_to_appear_in_the_domain(monkeypatch):
    monkeypatch.setattr(g.search, "buscar_web", fake_search(default=[
        SearchResult("Acme", "https://acme.com/", ""),
    ]))
    assert g.resolve_domain("Acme, Inc.", "pid") == "acme.com"


def test_company_tokens_drop_punctuation_accents_and_legal_suffixes():
    assert g.company_tokens("Café Olé, S.A.") == ["cafe", "ole"]
    assert g.company_tokens("Acme Inc") == ["acme"]


def test_a_guessed_domain_is_flagged_and_a_given_one_is_not(monkeypatch):
    monkeypatch.setattr(g.search, "buscar_web", fake_search(default=[
        SearchResult("Acme", "https://acme.com/", ""),
    ]))
    monkeypatch.setattr(g.web, "leer_sitio", fake_page())
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])
    assert g.gather("pid", "Ada", "Acme").domain_guessed is True
    assert g.gather("pid", "Ada", "Acme", domain="acme.com").domain_guessed is False
```

Añadir a `tests/test_followup.py`:

```python
def test_run_followup_keeps_the_guessed_domain_flag(monkeypatch):
    monkeypatch.setattr(f.search, "buscar_web", lambda q, limit=5, prospect_id=None: [])
    guessed = Gathered("acme.com", [], [], [], domain_guessed=True)
    assert f.run_followup("pid", guessed, ["x"]).domain_guessed is True
```

Añadir a `tests/test_synthesize.py`:

```python
def test_the_prompt_warns_when_the_domain_was_guessed():
    guessed = Gathered(domain="acme.com", evidence=[], jobs=[], domain_guessed=True)
    given = Gathered(domain="acme.com", evidence=[], jobs=[])
    assert "adivinado" in s.build_prompt("Ada", "Acme", guessed)
    assert "adivinado" not in s.build_prompt("Ada", "Acme", given)
```

- [ ] **Step 2: Ejecutarlos y comprobar que fallan**

Run: `uv run pytest tests/test_gather.py tests/test_followup.py tests/test_synthesize.py -v`
Expected: FAIL — `resolve_domain` devuelve `techcrunch.com`; `company_tokens` y `domain_guessed` no existen.

- [ ] **Step 3: Implementar en `gather.py`**

Ampliar `NOT_A_COMPANY_SITE` con agregadores y medios (sin grandes empresas: su propio dominio también se investiga):

```python
    "github.com", "medium.com", "substack.com", "reddit.com", "quora.com",
    "techcrunch.com", "forbes.com", "businessinsider.com", "nytimes.com",
    "wsj.com", "cnbc.com", "prnewswire.com", "businesswire.com",
    "ycombinator.com", "producthunt.com", "wellfound.com", "angel.co",
    "apollo.io", "rocketreach.co", "owler.com", "craft.co", "tracxn.com",
    "cbinsights.com", "dnb.com", "trustpilot.com", "yelp.com", "bbb.org",
```

Añadir `import re` e `import unicodedata`, y estas funciones antes de `resolve_domain`:

```python
def _ascii_words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9\s]", " ", folded.casefold()).split()


def company_tokens(company: str) -> list[str]:
    """The words of a company name that should appear in its site or title."""
    words = _ascii_words(company)
    while words and words[-1] in jobs.LEGAL_SUFFIXES:
        words.pop()
    return words


def _looks_like_company_site(host: str, title: str, company: str) -> bool:
    """A search hit counts as the company's own site only if the company's name
    shows up in its host ("helpscout" in helpscout.com) or as a phrase in the
    page title. Otherwise a news story or a namesake would be labelled as the
    person's home, about and careers pages."""
    tokens = company_tokens(company)
    if not tokens:
        return False
    compact_host = re.sub(r"[^a-z0-9]", "", host.casefold())
    if "".join(tokens) in compact_host:
        return True
    title_words = _ascii_words(title)
    n = len(tokens)
    return any(title_words[i : i + n] == tokens for i in range(len(title_words) - n + 1))
```

Y en `resolve_domain`, dentro del bucle, tras descartar los directorios:

```python
        if host and not any(host == d or host.endswith("." + d) for d in NOT_A_COMPANY_SITE):
            if _looks_like_company_site(host, result.title, company):
                return host
```

Añadir el campo al final de `Gathered`:

```python
    domain_guessed: bool = False
```

En `gather()`, marcar cuándo el dominio se adivinó:

```python
    domain_guessed = False
    if not domain and company:
        try:
            domain = resolve_domain(company, prospect_id, tally)
            domain_guessed = domain is not None
        except search.SearchUnavailable as exc:
            errors.append(f"dominio: {exc}")
```

y pasar `domain_guessed=domain_guessed` en el `Gathered(...)` que devuelve.

- [ ] **Step 4: Conservar la marca en `followup.py`**

En el `Gathered(...)` que construye `run_followup`, añadir `domain_guessed=gathered.domain_guessed`.

- [ ] **Step 5: Avisar al modelo en `synthesize.py`**

Sustituir la línea `f"Dominio detectado: {gathered.domain or 'ninguno'}",` de `build_prompt` por `_domain_line(gathered),` y añadir:

```python
def _domain_line(gathered: Gathered) -> str:
    line = f"Dominio detectado: {gathered.domain or 'ninguno'}"
    if gathered.domain and gathered.domain_guessed:
        line += (
            " (adivinado por búsqueda: comprueba en la evidencia que es la web de esta"
            " empresa; si no lo es, ignora esas páginas y dilo en huecos)"
        )
    return line
```

- [ ] **Step 6: Ejecutar los tests y la suite**

Run: `uv run pytest tests/test_gather.py tests/test_followup.py tests/test_synthesize.py -v && uv run pytest -q`
Expected: PASS, todo verde.

- [ ] **Step 7: Commit**

```bash
git add src/handoff_agent/research/ tests/test_gather.py tests/test_followup.py tests/test_synthesize.py
git commit -m "fix: el dominio adivinado tiene que llevar el nombre de la empresa y el modelo sabe que es adivinado"
```

---

### Task 3: Esquema de la ingesta

**Files:**
- Create: `supabase/migrations/0004_slack_ingest.sql`
- Modify: `tests/conftest.py`
- Test: `tests/test_schema.py`

**Interfaces:**
- Produces: tablas `slack_messages`, `member_snapshots`, `research_jobs`; índice único parcial `research_jobs_one_open`; fila `config.research_por_hora = 20`.

- [ ] **Step 1: Escribir la migración**

```sql
-- supabase/migrations/0004_slack_ingest.sql

-- Mensajes leídos del Slack del Founders Club. Solo lectura: el agente nunca
-- escribe allí.
create table slack_messages (
    id          uuid primary key default gen_random_uuid(),
    channel_id  text not null,
    ts          text not null,
    user_id     text,
    text        text,
    subtype     text,
    thread_ts   text,
    status      text not null default 'nuevo'
                check (status in ('nuevo', 'ignorado', 'archivado', 'pendiente_scoring')),
    created_at  timestamptz not null default now(),
    unique (channel_id, ts)
);

create index slack_messages_status_idx on slack_messages (status);
create index slack_messages_user_idx on slack_messages (user_id);

-- Padrón de cada canal en cada lectura, para detectar miembros nuevos por diferencia.
create table member_snapshots (
    id          uuid primary key default gen_random_uuid(),
    channel_id  text not null,
    members     text[] not null,
    taken_at    timestamptz not null default now()
);

create index member_snapshots_channel_idx on member_snapshots (channel_id, taken_at desc);

-- Cola de research. Una sola tarea abierta por persona: dos disparadores a la
-- vez (escribió y además entró como miembro nuevo) no pagan dos investigaciones.
create table research_jobs (
    id             uuid primary key default gen_random_uuid(),
    slack_user_id  text not null,
    reason         text not null check (reason in ('mensaje', 'miembro_nuevo', 'manual')),
    status         text not null default 'pendiente'
                   check (status in ('pendiente', 'en_curso', 'hecho', 'fallido')),
    attempts       integer not null default 0,
    last_error     text,
    not_before     timestamptz,
    outcome        jsonb,
    created_at     timestamptz not null default now(),
    started_at     timestamptz,
    finished_at    timestamptz
);

create unique index research_jobs_one_open on research_jobs (slack_user_id)
    where status in ('pendiente', 'en_curso');

create index research_jobs_claim_idx on research_jobs (status, created_at);

-- Throttling del research (spec §8): investigar en ráfaga tumba la búsqueda.
insert into config (key, value) values ('research_por_hora', '20'::jsonb)
on conflict (key) do nothing;

alter table slack_messages   enable row level security;
alter table member_snapshots enable row level security;
alter table research_jobs    enable row level security;
```

- [ ] **Step 2: Limpiar las tablas nuevas entre tests**

En `tests/conftest.py`, poner al principio de `TABLES_TO_CLEAN` las tablas nuevas:

```python
TABLES_TO_CLEAN = [
    "research_jobs", "slack_messages", "member_snapshots",
    "agent_actions", "llm_calls", "cost_events", "dossiers", "prospects",
]
```

- [ ] **Step 3: Escribir los tests que fallan**

Añadir a `tests/test_schema.py`:

```python
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
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute("insert into research_jobs (slack_user_id, reason) values ('U1', 'miembro_nuevo')")


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
        cur.execute("insert into slack_messages (channel_id, ts) values ('C1', '1726000000.000100')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute("insert into slack_messages (channel_id, ts) values ('C1', '1726000000.000100')")


def test_config_ships_with_an_hourly_research_limit(conn):
    with conn.cursor() as cur:
        cur.execute("select value from config where key = 'research_por_hora'")
        assert cur.fetchone()[0] == 20
```

- [ ] **Step 4: Ejecutar los tests**

Run: `uv run pytest tests/test_schema.py -v`
Expected: PASS. La base `handoff_test` se recrea desde las migraciones en cada sesión, así que la 0004 ya se aplica ahí.

- [ ] **Step 5: Aplicar la migración a la base de desarrollo sin borrarla**

```bash
supabase migration up
```
Expected: `Applying migration 0004_slack_ingest.sql...`. **Nunca** `supabase db reset`: borraría los dossiers de desarrollo.

- [ ] **Step 6: Commit**

```bash
git add supabase/migrations/0004_slack_ingest.sql tests/conftest.py tests/test_schema.py
git commit -m "feat: tablas de mensajes, padrones y cola de research con una tarea abierta por persona"
```

---

### Task 4: Lector de Slack de solo lectura

**Files:**
- Create: `src/handoff_agent/slack_client.py`
- Modify: `src/handoff_agent/config.py`, `.env.example`
- Test: `tests/test_slack_client.py`

**Interfaces:**
- Produces:
  - `Settings.slack_user_token: str | None`, `Settings.slack_channel_ids: tuple[str, ...]`, `Settings.slack_lookback_hours: float`, `Settings.slack_poll_seconds: int`, `Settings.alert_webhook_url: str | None`
  - `SlackAuthFailed(RuntimeError)`, `SlackUnavailable(RuntimeError)`, `AUTH_ERRORS`
  - `UserProfile` — dataclass congelada: `user_id, real_name, title, email, is_bot, deleted`
  - `FoundersClubReader(token=None, client=None)` con `owner_id() -> str`, `history(channel, oldest) -> list[dict]`, `members(channel) -> set[str]`, `user_profile(user_id) -> UserProfile`

- [ ] **Step 1: Escribir los tests que fallan**

```python
# tests/test_slack_client.py
import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from handoff_agent import slack_client as sc

READ_METHODS = {"auth_test", "conversations_history", "conversations_members", "users_info"}


class FakeWebClient:
    """Doble del WebClient: respuestas en cola y registro de cada llamada."""

    def __init__(self, *, history_pages=(), member_pages=(), users=None, error=None):
        self.history_pages = list(history_pages)
        self.member_pages = list(member_pages)
        self.users = users or {}
        self.error = error
        self.calls = []

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        if self.error:
            raise self.error

    def auth_test(self, **kwargs):
        self._record("auth_test", kwargs)
        return {"ok": True, "user_id": "UOWNER"}

    def conversations_history(self, **kwargs):
        self._record("conversations_history", kwargs)
        return self.history_pages.pop(0)

    def conversations_members(self, **kwargs):
        self._record("conversations_members", kwargs)
        return self.member_pages.pop(0)

    def users_info(self, **kwargs):
        self._record("users_info", kwargs)
        return {"ok": True, "user": self.users[kwargs["user"]]}


def test_the_reader_only_exposes_reads():
    """El agente nunca escribe en el Founders Club: la superficie queda fijada."""
    public = {name for name in dir(sc.FoundersClubReader) if not name.startswith("_")}
    assert public == {"owner_id", "history", "members", "user_profile"}


def test_history_follows_cursors_across_pages():
    fake = FakeWebClient(history_pages=[
        {"ok": True, "messages": [{"ts": "2"}], "has_more": True,
         "response_metadata": {"next_cursor": "abc"}},
        {"ok": True, "messages": [{"ts": "1"}], "has_more": False},
    ])
    reader = sc.FoundersClubReader(client=fake)
    assert [m["ts"] for m in reader.history("C1", oldest="0")] == ["2", "1"]
    assert fake.calls[1][1]["cursor"] == "abc"
    assert fake.calls[0][1]["oldest"] == "0"


def test_members_follows_cursors_and_returns_a_set():
    fake = FakeWebClient(member_pages=[
        {"ok": True, "members": ["U1", "U2"], "response_metadata": {"next_cursor": "x"}},
        {"ok": True, "members": ["U3"], "response_metadata": {"next_cursor": ""}},
    ])
    assert sc.FoundersClubReader(client=fake).members("C1") == {"U1", "U2", "U3"}


def test_user_profile_maps_the_fields_we_use():
    fake = FakeWebClient(users={"U1": {
        "real_name": "Ada Ruiz", "is_bot": False, "deleted": False,
        "profile": {"title": "CEO @ Acme", "email": "ada@acme.com"},
    }})
    profile = sc.FoundersClubReader(client=fake).user_profile("U1")
    assert profile == sc.UserProfile("U1", "Ada Ruiz", "CEO @ Acme", "ada@acme.com", False, False)


@pytest.mark.parametrize("error", ["invalid_auth", "token_revoked", "missing_scope"])
def test_auth_errors_need_a_human(error):
    fake = FakeWebClient(error=SlackApiError("nope", {"ok": False, "error": error}))
    with pytest.raises(sc.SlackAuthFailed, match=error):
        sc.FoundersClubReader(client=fake).owner_id()


def test_other_slack_errors_are_retryable():
    fake = FakeWebClient(error=SlackApiError("nope", {"ok": False, "error": "internal_error"}))
    with pytest.raises(sc.SlackUnavailable):
        sc.FoundersClubReader(client=fake).members("C1")


def test_a_missing_token_is_an_auth_failure():
    with pytest.raises(sc.SlackAuthFailed, match="SLACK_USER_TOKEN"):
        sc.FoundersClubReader(token=None)


def test_the_real_client_retries_on_rate_limits():
    reader = sc.FoundersClubReader(token="xoxp-test")
    assert any(isinstance(h, RateLimitErrorRetryHandler) for h in reader._client.retry_handlers)


def test_only_read_methods_ever_reach_slack():
    fake = FakeWebClient(
        history_pages=[{"ok": True, "messages": [], "has_more": False}],
        member_pages=[{"ok": True, "members": [], "response_metadata": {}}],
        users={"U1": {"real_name": "Ada", "profile": {}}},
    )
    reader = sc.FoundersClubReader(client=fake)
    reader.owner_id()
    reader.history("C1", oldest="0")
    reader.members("C1")
    reader.user_profile("U1")
    assert {name for name, _ in fake.calls} <= READ_METHODS
```

- [ ] **Step 2: Ejecutarlos y comprobar que fallan**

Run: `uv run pytest tests/test_slack_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.slack_client'`

- [ ] **Step 3: Configuración de Slack y del webhook**

En `Settings`, al final:

```python
    slack_user_token: str | None
    slack_channel_ids: tuple[str, ...]
    slack_lookback_hours: float
    slack_poll_seconds: int
    alert_webhook_url: str | None
```

En `load_settings()`:

```python
        slack_user_token=os.environ.get("SLACK_USER_TOKEN") or None,
        slack_channel_ids=tuple(
            c.strip() for c in os.environ.get("SLACK_CHANNEL_IDS", "").split(",") if c.strip()
        ),
        slack_lookback_hours=float(os.environ.get("SLACK_LOOKBACK_HOURS", "1")),
        slack_poll_seconds=int(os.environ.get("SLACK_POLL_SECONDS", "3600")),
        alert_webhook_url=os.environ.get("HANDOFF_ALERT_WEBHOOK_URL") or None,
```

En `.env.example`:

```bash
# Slack del Founders Club — token de USUARIO (xoxp) con scopes de solo lectura:
# channels:history, channels:read, groups:history, groups:read, users:read,
# users:read.email. El agente nunca escribe allí.
SLACK_USER_TOKEN=
# IDs de los canales a vigilar, separados por comas (C0123,C0456)
SLACK_CHANNEL_IDS=
# Primera lectura de un canal: cuántas horas hacia atrás. Sin backfill.
SLACK_LOOKBACK_HOURS=1
SLACK_POLL_SECONDS=3600

# Alertas operativas (token caído, parada del sistema) — webhook entrante del
# Slack DE HANDOFF, nunca del Founders Club. Opcional.
HANDOFF_ALERT_WEBHOOK_URL=
```

- [ ] **Step 4: Implementar `slack_client.py`**

```python
# src/handoff_agent/slack_client.py
"""Read-only access to the Founders Club Slack.

The workspace belongs to a third party and Anthony is one member among 1,400.
The agent never writes there — no messages, no reactions, no DMs — so this
wrapper exposes reads only, and a test pins that surface. Anything the agent
posts goes to Handoff's own Slack, through a different credential.
"""

from __future__ import annotations

from dataclasses import dataclass

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

# No se arreglan reintentando: hace falta una persona (token nuevo o más scopes).
AUTH_ERRORS = frozenset({
    "invalid_auth", "not_authed", "token_revoked", "token_expired",
    "account_inactive", "missing_scope",
})
PAGE_SIZE = 200
RATE_LIMIT_RETRIES = 3


class SlackAuthFailed(RuntimeError):
    """The token is missing, revoked or lacks a scope. Needs a human."""


class SlackUnavailable(RuntimeError):
    """Slack answered with an error worth retrying later."""


@dataclass(frozen=True)
class UserProfile:
    user_id: str
    real_name: str
    title: str
    email: str
    is_bot: bool
    deleted: bool


class FoundersClubReader:
    def __init__(self, token: str | None = None, client=None) -> None:
        if client is None:
            if not token:
                raise SlackAuthFailed("SLACK_USER_TOKEN no está configurado")
            client = WebClient(
                token=token,
                retry_handlers=[RateLimitErrorRetryHandler(max_retry_count=RATE_LIMIT_RETRIES)],
            )
        self._client = client

    def _call(self, method: str, **kwargs):
        try:
            return getattr(self._client, method)(**kwargs)
        except SlackApiError as exc:
            response = exc.response if exc.response is not None else {}
            error = response.get("error", "") if hasattr(response, "get") else ""
            if error in AUTH_ERRORS:
                raise SlackAuthFailed(f"Slack rechazó el token: {error}") from exc
            raise SlackUnavailable(f"{method}: {error or exc}") from exc

    def owner_id(self) -> str:
        """The member whose token this is (Anthony, in production)."""
        return self._call("auth_test")["user_id"]

    def history(self, channel: str, oldest: str) -> list[dict]:
        messages: list[dict] = []
        cursor = None
        while True:
            page = self._call(
                "conversations_history", channel=channel, oldest=oldest,
                limit=PAGE_SIZE, cursor=cursor,
            )
            messages.extend(page.get("messages") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not page.get("has_more") or not cursor:
                return messages

    def members(self, channel: str) -> set[str]:
        found: set[str] = set()
        cursor = None
        while True:
            page = self._call("conversations_members", channel=channel, limit=PAGE_SIZE, cursor=cursor)
            found.update(page.get("members") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return found

    def user_profile(self, user_id: str) -> UserProfile:
        user = self._call("users_info", user=user_id)["user"]
        profile = user.get("profile") or {}
        return UserProfile(
            user_id=user_id,
            real_name=user.get("real_name") or profile.get("real_name") or "",
            title=profile.get("title") or "",
            email=profile.get("email") or "",
            is_bot=bool(user.get("is_bot")),
            deleted=bool(user.get("deleted")),
        )
```

- [ ] **Step 5: Ejecutar los tests y la suite**

Run: `uv run pytest tests/test_slack_client.py -v && uv run pytest -q`
Expected: PASS, todo verde.

- [ ] **Step 6: Commit**

```bash
git add src/handoff_agent/slack_client.py src/handoff_agent/config.py .env.example tests/test_slack_client.py
git commit -m "feat: lector del Slack del Founders Club que solo puede leer"
```

---

### Task 5: Alertas operativas

**Files:**
- Create: `src/handoff_agent/ops_alerts.py`
- Test: `tests/test_ops_alerts.py`

**Interfaces:**
- Consumes: `ledger.record_action`, `Settings.alert_webhook_url` (Task 4)
- Produces: `alert(kind: str, message: str) -> None`; `WEBHOOK_TIMEOUT_SECONDS = 10`

- [ ] **Step 1: Escribir los tests que fallan**

```python
# tests/test_ops_alerts.py
import logging

import httpx
import respx

from handoff_agent import ops_alerts

WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


def test_an_alert_is_logged_and_recorded(conn, caplog):
    with caplog.at_level(logging.CRITICAL):
        ops_alerts.alert("slack_auth", "token revocado")
    assert "token revocado" in caplog.text
    with conn.cursor() as cur:
        cur.execute("select payload from agent_actions where action = 'alerta_operativa'")
        assert cur.fetchone()[0] == {"tipo": "slack_auth", "mensaje": "token revocado"}


@respx.mock
def test_an_alert_reaches_the_handoff_webhook_when_configured(conn, monkeypatch):
    monkeypatch.setenv("HANDOFF_ALERT_WEBHOOK_URL", WEBHOOK)
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
    ops_alerts.alert("slack_auth", "token revocado")
    assert route.called
    assert "token revocado" in route.calls[0].request.content.decode()


@respx.mock
def test_a_broken_webhook_never_raises(conn, monkeypatch):
    """Una alerta no puede tumbar el proceso del que avisa."""
    monkeypatch.setenv("HANDOFF_ALERT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(return_value=httpx.Response(500))
    ops_alerts.alert("slack_auth", "token revocado")


@respx.mock
def test_without_a_webhook_no_request_is_made(conn):
    # respx.mock sin rutas hace fallar cualquier petición: si pasa, no hubo ninguna.
    ops_alerts.alert("slack_auth", "token revocado")
```

- [ ] **Step 2: Ejecutarlos y comprobar que fallan**

Run: `uv run pytest tests/test_ops_alerts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.ops_alerts'`

- [ ] **Step 3: Implementar `ops_alerts.py`**

```python
# src/handoff_agent/ops_alerts.py
"""Operational alerts: problems that need a person, never swallowed.

Each alert goes to the log at CRITICAL, to the agent_actions ledger and — if
HANDOFF_ALERT_WEBHOOK_URL is set — to an incoming webhook in Handoff's own
Slack. Never to the Founders Club. Failing to deliver an alert is logged, not
raised: an alert must not take down the process it is reporting on.
"""

from __future__ import annotations

import logging

import httpx

from . import ledger
from .config import load_settings

logger = logging.getLogger(__name__)
WEBHOOK_TIMEOUT_SECONDS = 10


def alert(kind: str, message: str) -> None:
    logger.critical("ALERTA %s: %s", kind, message)
    try:
        ledger.record_action("alerta_operativa", {"tipo": kind, "mensaje": message})
    except Exception:  # noqa: BLE001 - la alerta no puede depender de la base
        logger.exception("no se pudo registrar la alerta en agent_actions")

    url = load_settings().alert_webhook_url
    if not url:
        return
    try:
        httpx.post(
            url,
            json={"text": f":rotating_light: *{kind}*: {message}"},
            timeout=WEBHOOK_TIMEOUT_SECONDS,
        ).raise_for_status()
    except httpx.HTTPError:
        logger.exception("no se pudo enviar la alerta al webhook de Handoff")
```

- [ ] **Step 4: Ejecutar los tests y la suite**

Run: `uv run pytest tests/test_ops_alerts.py -v && uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/ops_alerts.py tests/test_ops_alerts.py
git commit -m "feat: alertas operativas por log, ledger y webhook del Slack de Handoff"
```

---

### Task 6: Cola de research

**Files:**
- Create: `src/handoff_agent/ingest/__init__.py`, `src/handoff_agent/ingest/queue.py`
- Test: `tests/test_queue.py`

**Interfaces:**
- Consumes: `db`, tabla `research_jobs` (Task 3)
- Produces: `MAX_ATTEMPTS = 3`, `REASONS`; `enqueue(slack_user_id, reason) -> bool`; `claim_next() -> dict | None`; `complete(job_id, outcome: dict)`; `fail(job_id, error) -> str` (devuelve el estado nuevo); `give_up(job_id, error)`; `release(job_id)`; `finished_last_hour() -> int`; `hourly_limit() -> int`; `status_counts() -> dict[str, int]`; `recent_failures(limit=5) -> list[dict]`

- [ ] **Step 1: Escribir los tests que fallan**

```python
# tests/test_queue.py
from concurrent.futures import ThreadPoolExecutor

import pytest

from handoff_agent import db
from handoff_agent.ingest import queue


def test_enqueue_creates_one_pending_job(conn):
    assert queue.enqueue("U1", "mensaje") is True
    assert queue.status_counts() == {"pendiente": 1}


def test_only_one_open_job_per_person_even_under_concurrency(conn):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: queue.enqueue("U1", "mensaje"), range(8)))
    assert results.count(True) == 1
    assert queue.status_counts() == {"pendiente": 1}


def test_unknown_reasons_are_rejected(conn):
    with pytest.raises(ValueError):
        queue.enqueue("U1", "porque sí")


def test_claim_takes_the_oldest_pending_job(conn):
    queue.enqueue("U1", "mensaje")
    queue.enqueue("U2", "miembro_nuevo")
    job = queue.claim_next()
    assert job["slack_user_id"] == "U1"
    assert job["status"] == "en_curso"
    assert job["attempts"] == 1


def test_claim_returns_none_when_nothing_is_pending(conn):
    assert queue.claim_next() is None


def test_claim_skips_jobs_scheduled_for_later(conn):
    queue.enqueue("U1", "mensaje")
    db.execute("update research_jobs set not_before = now() + interval '1 hour'")
    assert queue.claim_next() is None


def test_concurrent_workers_never_claim_the_same_job(conn):
    queue.enqueue("U1", "mensaje")
    queue.enqueue("U2", "mensaje")
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = [job for job in pool.map(lambda _: queue.claim_next(), range(4)) if job]
    assert sorted(job["slack_user_id"] for job in claimed) == ["U1", "U2"]


def test_complete_stores_the_outcome(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    queue.complete(job["id"], {"estado": "investigado"})
    row = db.fetch_one("select status, outcome, finished_at from research_jobs")
    assert row["status"] == "hecho"
    assert row["outcome"] == {"estado": "investigado"}
    assert row["finished_at"] is not None


def test_failures_retry_with_backoff_then_give_up(conn):
    queue.enqueue("U1", "mensaje")
    for attempt in range(1, queue.MAX_ATTEMPTS + 1):
        db.execute("update research_jobs set not_before = null")
        job = queue.claim_next()
        status = queue.fail(job["id"], f"fallo {attempt}")
        assert status == ("fallido" if attempt == queue.MAX_ATTEMPTS else "pendiente")
    row = db.fetch_one("select attempts, last_error from research_jobs")
    assert row["attempts"] == queue.MAX_ATTEMPTS
    assert row["last_error"] == f"fallo {queue.MAX_ATTEMPTS}"


def test_a_retry_waits_before_it_can_be_claimed_again(conn):
    queue.enqueue("U1", "mensaje")
    queue.fail(queue.claim_next()["id"], "timeout")
    assert queue.claim_next() is None


def test_release_hands_the_job_back_without_burning_an_attempt(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    queue.release(job["id"])
    again = queue.claim_next()
    assert again["id"] == job["id"]
    assert again["attempts"] == 1


def test_give_up_fails_at_once(conn):
    queue.enqueue("U1", "mensaje")
    queue.give_up(queue.claim_next()["id"], "sin nombre ni empresa")
    assert queue.status_counts() == {"fallido": 1}


def test_finished_last_hour_counts_done_and_failed_jobs(conn):
    for user in ("U1", "U2", "U3"):
        queue.enqueue(user, "mensaje")
    queue.complete(queue.claim_next()["id"], {})
    queue.give_up(queue.claim_next()["id"], "x")
    assert queue.finished_last_hour() == 2


def test_the_hourly_limit_comes_from_config(conn):
    assert queue.hourly_limit() == 20


def test_recent_failures_lists_the_newest_first(conn):
    queue.enqueue("U1", "mensaje")
    queue.give_up(queue.claim_next()["id"], "primero")
    queue.enqueue("U2", "mensaje")
    queue.give_up(queue.claim_next()["id"], "segundo")
    assert [f["last_error"] for f in queue.recent_failures()] == ["segundo", "primero"]
```

- [ ] **Step 2: Ejecutarlos y comprobar que fallan**

Run: `uv run pytest tests/test_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff_agent.ingest'`

- [ ] **Step 3: Implementar `ingest/queue.py`**

```bash
mkdir -p src/handoff_agent/ingest && touch src/handoff_agent/ingest/__init__.py
```

```python
# src/handoff_agent/ingest/queue.py
"""Research queue in Postgres.

One open job per person (a partial unique index), claimed with
FOR UPDATE SKIP LOCKED so several workers never take the same job. System
stops (kill switch, monthly cap) hand the job back untouched: they are not the
person's fault and must not burn a retry.
"""

from __future__ import annotations

import json

from .. import db

MAX_ATTEMPTS = 3
REASONS = ("mensaje", "miembro_nuevo", "manual")
DEFAULT_HOURLY_LIMIT = 20


def enqueue(slack_user_id: str, reason: str) -> bool:
    """Queue research for a person. False if they already have an open job."""
    if reason not in REASONS:
        raise ValueError(f"motivo desconocido: {reason!r}")
    inserted = db.execute(
        """
        insert into research_jobs (slack_user_id, reason) values (%s, %s)
        on conflict (slack_user_id) where status in ('pendiente', 'en_curso') do nothing
        """,
        (slack_user_id, reason),
    )
    return inserted == 1


def claim_next() -> dict | None:
    return db.fetch_one(
        """
        update research_jobs
        set status = 'en_curso', started_at = now(), attempts = attempts + 1
        where id = (
            select id from research_jobs
            where status = 'pendiente' and (not_before is null or not_before <= now())
            order by created_at
            for update skip locked
            limit 1
        )
        returning *
        """
    )


def complete(job_id, outcome: dict) -> None:
    db.execute(
        "update research_jobs set status = 'hecho', outcome = %s, finished_at = now(), "
        "last_error = null where id = %s",
        (json.dumps(outcome), job_id),
    )


def fail(job_id, error: str) -> str:
    """Retry later with a growing wait, or give up after MAX_ATTEMPTS."""
    row = db.fetch_one(
        """
        update research_jobs set
            status      = case when attempts >= %s then 'fallido' else 'pendiente' end,
            not_before  = case when attempts >= %s then null
                               else now() + attempts * interval '15 minutes' end,
            finished_at = case when attempts >= %s then now() else null end,
            last_error  = %s
        where id = %s
        returning status
        """,
        (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, error, job_id),
    )
    return row["status"]


def give_up(job_id, error: str) -> None:
    db.execute(
        "update research_jobs set status = 'fallido', finished_at = now(), last_error = %s "
        "where id = %s",
        (error, job_id),
    )


def release(job_id) -> None:
    """Hand a job back after a system stop, without counting the attempt."""
    db.execute(
        "update research_jobs set status = 'pendiente', started_at = null, "
        "attempts = greatest(attempts - 1, 0) where id = %s",
        (job_id,),
    )


def finished_last_hour() -> int:
    row = db.fetch_one(
        "select count(*) as n from research_jobs "
        "where status in ('hecho', 'fallido') and finished_at > now() - interval '1 hour'"
    )
    return row["n"]


def hourly_limit() -> int:
    row = db.fetch_one("select value from config where key = 'research_por_hora'")
    return int(row["value"]) if row else DEFAULT_HOURLY_LIMIT


def status_counts() -> dict[str, int]:
    rows = db.fetch_all("select status, count(*) as n from research_jobs group by status")
    return {row["status"]: row["n"] for row in rows}


def recent_failures(limit: int = 5) -> list[dict]:
    return db.fetch_all(
        "select slack_user_id, last_error, finished_at from research_jobs "
        "where status = 'fallido' order by finished_at desc limit %s",
        (limit,),
    )
```

- [ ] **Step 4: Ejecutar los tests y la suite**

Run: `uv run pytest tests/test_queue.py -v && uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/ingest/ tests/test_queue.py
git commit -m "feat: cola de research en Postgres con SKIP LOCKED y reintentos con espera"
```

---

### Task 7: Watcher — mensajes y miembros nuevos

**Files:**
- Create: `src/handoff_agent/ingest/watcher.py`, `tests/slack_fakes.py`
- Test: `tests/test_watcher.py`

**Interfaces:**
- Consumes: la superficie de `FoundersClubReader` (Task 4), `SlackUnavailable`, `queue.enqueue` (Task 6)
- Produces: `SYSTEM_SUBTYPES`; `TickResult` (`messages_new`, `messages_ignored`, `members_new`, `errors`); `classify(message, owner_id) -> str`; `watch_tick(reader, channels, lookback_hours, now=time.time) -> TickResult`. En `tests/slack_fakes.py`: `FakeReader`, que usan las Tasks 8–12.

- [ ] **Step 1: Crear el `FakeReader` compartido**

```python
# tests/slack_fakes.py
"""A reader with the same surface as FoundersClubReader, for the ingest tests."""

from handoff_agent.slack_client import SlackUnavailable, UserProfile


class FakeReader:
    def __init__(self, owner: str = "UOWNER"):
        self.owner = owner
        self.messages: dict[str, list[dict]] = {}
        self.channel_members: dict[str, set[str]] = {}
        self.profiles: dict[str, UserProfile] = {}
        self.auth_error: Exception | None = None
        self.unavailable_channels: set[str] = set()
        self.history_calls: list[tuple[str, str]] = []

    # --- la misma superficie que FoundersClubReader ---
    def owner_id(self) -> str:
        if self.auth_error:
            raise self.auth_error
        return self.owner

    def history(self, channel: str, oldest: str) -> list[dict]:
        self.history_calls.append((channel, oldest))
        if channel in self.unavailable_channels:
            raise SlackUnavailable(f"{channel}: internal_error")
        return [m for m in self.messages.get(channel, []) if float(m["ts"]) > float(oldest)]

    def members(self, channel: str) -> set[str]:
        return set(self.channel_members.get(channel, set()))

    def user_profile(self, user_id: str) -> UserProfile:
        if self.auth_error:
            raise self.auth_error
        return self.profiles[user_id]

    # --- ayudas para los tests ---
    def post(self, channel: str, user: str, text: str, ts: str, **extra) -> None:
        self.messages.setdefault(channel, []).append({"user": user, "text": text, "ts": ts, **extra})

    def set_members(self, channel: str, *user_ids: str) -> None:
        self.channel_members[channel] = set(user_ids)

    def add_profile(self, user_id, real_name, title="", email="", is_bot=False, deleted=False):
        self.profiles[user_id] = UserProfile(user_id, real_name, title, email, is_bot, deleted)
```

- [ ] **Step 2: Escribir los tests que fallan**

```python
# tests/test_watcher.py
import pytest

from handoff_agent import db
from handoff_agent.ingest import queue, watcher
from handoff_agent.slack_client import SlackAuthFailed
from tests.slack_fakes import FakeReader

NOW = 1_726_000_000.0


def ts(seconds_ago: float) -> str:
    return f"{NOW - seconds_ago:.6f}"


@pytest.fixture
def reader():
    return FakeReader()


def tick(reader, channels=("C1",)):
    return watcher.watch_tick(reader, list(channels), lookback_hours=1, now=lambda: NOW)


def test_the_first_read_only_looks_back_the_configured_hours(conn, reader):
    """Sin backfill: lo de antes de la ventana no se lee."""
    reader.post("C1", "U1", "reciente", ts(600))
    reader.post("C1", "U2", "de hace dos horas", ts(7200))
    assert tick(reader).messages_new == 1
    assert db.fetch_one("select user_id from slack_messages")["user_id"] == "U1"


def test_later_reads_start_after_the_last_stored_message(conn, reader):
    reader.post("C1", "U1", "a", ts(600))
    tick(reader)
    reader.post("C1", "U2", "b", ts(300))
    assert tick(reader).messages_new == 1
    assert reader.history_calls[-1][1] == ts(600)


@pytest.mark.parametrize(
    ("user", "extra"),
    [
        ("U1", {"bot_id": "B1"}),
        ("U1", {"subtype": "channel_join"}),
        ("U1", {"subtype": "bot_message"}),
        ("UOWNER", {}),
    ],
)
def test_bots_system_messages_and_the_owner_are_ignored(conn, reader, user, extra):
    reader.post("C1", user, "x", ts(60), **extra)
    result = tick(reader)
    assert (result.messages_new, result.messages_ignored) == (0, 1)
    assert db.fetch_one("select status from slack_messages")["status"] == "ignorado"


def test_a_message_with_a_file_is_a_real_message(conn, reader):
    reader.post("C1", "U1", "mira esto", ts(60), subtype="file_share")
    assert tick(reader).messages_new == 1


def test_the_first_member_list_is_a_baseline_and_queues_nobody(conn, reader):
    """El primer padrón nunca encola a todos los miembros: sería el backfill
    de 1.400 personas que se descartó."""
    reader.set_members("C1", "U1", "U2", "U3")
    assert tick(reader).members_new == 0
    assert queue.status_counts() == {}
    assert db.fetch_one("select count(*) as n from member_snapshots")["n"] == 1


def test_new_members_are_queued_on_later_reads(conn, reader):
    reader.set_members("C1", "U1")
    tick(reader)
    reader.set_members("C1", "U1", "U2", "UOWNER")
    assert tick(reader).members_new == 1
    job = db.fetch_one("select slack_user_id, reason from research_jobs")
    assert job == {"slack_user_id": "U2", "reason": "miembro_nuevo"}


def test_one_channel_down_does_not_stop_the_others(conn, reader):
    reader.unavailable_channels.add("C1")
    reader.post("C2", "U1", "hola", ts(60))
    result = tick(reader, channels=("C1", "C2"))
    assert result.messages_new == 1
    assert len(result.errors) == 1 and "C1" in result.errors[0]


def test_an_auth_failure_propagates(conn, reader):
    reader.auth_error = SlackAuthFailed("Slack rechazó el token: token_revoked")
    with pytest.raises(SlackAuthFailed):
        tick(reader)
```

- [ ] **Step 3: Ejecutarlos y comprobar que fallan**

Run: `uv run pytest tests/test_watcher.py -v`
Expected: FAIL — `ImportError: cannot import name 'watcher'`

- [ ] **Step 4: Implementar `ingest/watcher.py`**

```python
# src/handoff_agent/ingest/watcher.py
"""Hourly read of the Founders Club Slack: new messages and new members.

No backfill, on purpose: the first read of a channel looks back
SLACK_LOOKBACK_HOURS only, and the first member snapshot is a baseline.
Otherwise the first run would queue research on all 1,400 members, which was
ruled out. Later reads start right after the last stored message.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import db
from ..slack_client import SlackUnavailable
from . import queue

# Mensajes que no escribió una persona o que no dicen nada: nunca disparan research.
SYSTEM_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "bot_message", "channel_topic",
    "channel_purpose", "channel_name", "channel_archive", "channel_unarchive",
    "pinned_item", "unpinned_item", "tombstone", "message_deleted", "message_changed",
})


@dataclass
class TickResult:
    messages_new: int = 0
    messages_ignored: int = 0
    members_new: int = 0
    errors: list[str] = field(default_factory=list)


def classify(message: dict, owner_id: str) -> str:
    user = message.get("user")
    if (
        not user
        or user == owner_id
        or message.get("bot_id")
        or message.get("subtype") in SYSTEM_SUBTYPES
    ):
        return "ignorado"
    return "nuevo"


def _oldest(channel: str, lookback_hours: float, now: Callable[[], float]) -> str:
    row = db.fetch_one(
        "select ts from slack_messages where channel_id = %s order by ts::numeric desc limit 1",
        (channel,),
    )
    if row:
        return row["ts"]
    return f"{now() - lookback_hours * 3600:.6f}"


def _store(channel: str, message: dict, status: str) -> bool:
    inserted = db.execute(
        """
        insert into slack_messages (channel_id, ts, user_id, text, subtype, thread_ts, status)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (channel_id, ts) do nothing
        """,
        (
            channel, message["ts"], message.get("user"), message.get("text"),
            message.get("subtype"), message.get("thread_ts"), status,
        ),
    )
    return inserted == 1


def _diff_members(reader, channel: str, owner_id: str, result: TickResult) -> None:
    current = reader.members(channel)
    previous = db.fetch_one(
        "select members from member_snapshots where channel_id = %s "
        "order by taken_at desc limit 1",
        (channel,),
    )
    db.execute(
        "insert into member_snapshots (channel_id, members) values (%s, %s)",
        (channel, sorted(current)),
    )
    if previous is None:
        return  # línea base: nunca se encola a todo el padrón
    for user_id in sorted(current - set(previous["members"]) - {owner_id}):
        if queue.enqueue(user_id, "miembro_nuevo"):
            result.members_new += 1


def watch_tick(
    reader,
    channels: list[str],
    lookback_hours: float,
    now: Callable[[], float] = time.time,
) -> TickResult:
    """Read every channel once. SlackAuthFailed propagates: it needs a person."""
    result = TickResult()
    owner_id = reader.owner_id()
    for channel in channels:
        try:
            for message in reader.history(channel, oldest=_oldest(channel, lookback_hours, now)):
                status = classify(message, owner_id)
                if _store(channel, message, status):
                    if status == "nuevo":
                        result.messages_new += 1
                    else:
                        result.messages_ignored += 1
            _diff_members(reader, channel, owner_id, result)
        except SlackUnavailable as exc:
            result.errors.append(f"{channel}: {exc}")
    return result
```

- [ ] **Step 5: Ejecutar los tests y la suite**

Run: `uv run pytest tests/test_watcher.py -v && uv run pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/handoff_agent/ingest/watcher.py tests/slack_fakes.py tests/test_watcher.py
git commit -m "feat: watcher horario de mensajes y miembros nuevos, sin backfill"
```

---

### Task 8: Resolver

**Files:**
- Create: `src/handoff_agent/ingest/resolver.py`
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: `slack_messages` (Task 3), `prospects`, `queue.enqueue` (Task 6)
- Produces: `RESEARCH_AGAIN = ("nuevo", "incompleto")`; `ResolveResult` (`enqueued`, `archived`, `to_scoring`); `resolve_pending(limit=500) -> ResolveResult`

**Reglas (spec §4):** autor desconocido, o cuyo último research quedó `incompleto` → se encola research y el mensaje queda `pendiente_scoring`; autor `descartado` → el mensaje se archiva y no se investiga; cualquier otro → `pendiente_scoring` sin research. El scoring es el Plan 3.

- [ ] **Step 1: Escribir los tests que fallan**

```python
# tests/test_resolver.py
from handoff_agent import db
from handoff_agent.ingest import queue, resolver
from handoff_agent.tools import prospects


def store(user: str, ts: str, status: str = "nuevo") -> None:
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, status) "
        "values ('C1', %s, %s, 'hola', %s)",
        (ts, user, status),
    )


def person(user: str, state: str) -> None:
    prospects.upsert_prospect(user, full_name="X")
    db.execute("update prospects set state = %s where slack_user_id = %s", (state, user))


def message_status(ts: str) -> str:
    return db.fetch_one("select status from slack_messages where ts = %s", (ts,))["status"]


def test_an_unknown_author_gets_a_research_job(conn):
    store("U1", "1.0")
    result = resolver.resolve_pending()
    assert (result.enqueued, result.to_scoring) == (1, 1)
    assert message_status("1.0") == "pendiente_scoring"
    assert db.fetch_one("select reason from research_jobs")["reason"] == "mensaje"


def test_two_messages_from_the_same_stranger_make_one_job(conn):
    store("U1", "1.0")
    store("U1", "2.0")
    assert resolver.resolve_pending().enqueued == 1
    assert queue.status_counts() == {"pendiente": 1}


def test_a_discarded_author_is_archived_and_never_researched(conn):
    person("U1", "descartado")
    store("U1", "1.0")
    assert resolver.resolve_pending().archived == 1
    assert message_status("1.0") == "archivado"
    assert queue.status_counts() == {}


def test_an_investigated_author_goes_straight_to_scoring(conn):
    person("U1", "investigado")
    store("U1", "1.0")
    assert resolver.resolve_pending().enqueued == 0
    assert message_status("1.0") == "pendiente_scoring"


def test_an_incomplete_author_is_researched_again(conn):
    person("U1", "incompleto")
    store("U1", "1.0")
    assert resolver.resolve_pending().enqueued == 1


def test_ignored_messages_are_left_alone(conn):
    store("U1", "1.0", status="ignorado")
    resolver.resolve_pending()
    assert message_status("1.0") == "ignorado"
    assert queue.status_counts() == {}
```

- [ ] **Step 2: Ejecutarlos y comprobar que fallan**

Run: `uv run pytest tests/test_resolver.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolver'`

- [ ] **Step 3: Implementar `ingest/resolver.py`**

```python
# src/handoff_agent/ingest/resolver.py
"""What to do with each message the watcher stored.

The person, not the message, is the unit of work: an unknown author, or one
whose last research came out incomplete, gets a research job; a discarded
author's message is archived; everyone else's message waits for scoring
(Plan 3). The queue's one-open-job-per-person rule absorbs repeats.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import db
from . import queue

RESEARCH_AGAIN = ("nuevo", "incompleto")
BATCH = 500


@dataclass
class ResolveResult:
    enqueued: int = 0
    archived: int = 0
    to_scoring: int = 0


def resolve_pending(limit: int = BATCH) -> ResolveResult:
    result = ResolveResult()
    rows = db.fetch_all(
        "select id, user_id from slack_messages where status = 'nuevo' "
        "order by ts::numeric limit %s",
        (limit,),
    )
    for row in rows:
        person = db.fetch_one(
            "select state from prospects where slack_user_id = %s", (row["user_id"],)
        )
        if person is not None and person["state"] == "descartado":
            status = "archivado"
            result.archived += 1
        else:
            if (person is None or person["state"] in RESEARCH_AGAIN) and queue.enqueue(
                row["user_id"], "mensaje"
            ):
                result.enqueued += 1
            status = "pendiente_scoring"
            result.to_scoring += 1
        db.execute("update slack_messages set status = %s where id = %s", (status, row["id"]))
    return result
```

- [ ] **Step 4: Ejecutar los tests y la suite**

Run: `uv run pytest tests/test_resolver.py -v && uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/ingest/resolver.py tests/test_resolver.py
git commit -m "feat: resolver que encola research por persona y archiva a los descartados"
```

---

### Task 9: Pistas del perfil de Slack

**Files:**
- Create: `src/handoff_agent/ingest/profile_hints.py`
- Test: `tests/test_profile_hints.py`

**Interfaces:**
- Produces: `FREE_MAIL_DOMAINS`, `ROLE_WORDS`; `company_from_title(title: str) -> str | None`; `domain_from_email(email: str) -> str | None`

**Por qué:** el perfil de Slack casi nunca trae la empresa en un campo propio, pero el título suele decirla ("CEO @ Acme") y el email de trabajo da el dominio real. Un dominio sacado del email ahorra el camino del dominio adivinado, que es el más frágil.

- [ ] **Step 1: Escribir los tests que fallan**

```python
# tests/test_profile_hints.py
import pytest

from handoff_agent.ingest import profile_hints as ph


@pytest.mark.parametrize(
    ("title", "company"),
    [
        ("CEO @ Acme", "Acme"),
        ("CEO @Acme", "Acme"),
        ("Founder at Northwind Ops", "Northwind Ops"),
        ("CEO, Acme Inc", "Acme Inc"),
        ("CEO | Acme", "Acme"),
        ("Co-founder & CEO - Acme", "Acme"),
        ("CEO en Rappi", "Rappi"),
    ],
)
def test_company_is_read_from_common_title_shapes(title, company):
    assert ph.company_from_title(title) == company


@pytest.mark.parametrize("title", ["", "CEO", "CEO, Founder", "Co-founder", "@", "x" * 200])
def test_titles_without_a_company_give_none(title):
    assert ph.company_from_title(title) is None


@pytest.mark.parametrize(
    ("email", "domain"),
    [
        ("ada@acme.com", "acme.com"),
        ("Ada@Acme.COM", "acme.com"),
        ("ada@gmail.com", None),
        ("ada@outlook.com", None),
        ("", None),
        ("no-es-un-email", None),
    ],
)
def test_a_work_email_gives_the_company_domain(email, domain):
    assert ph.domain_from_email(email) == domain
```

- [ ] **Step 2: Ejecutarlos y comprobar que fallan**

Run: `uv run pytest tests/test_profile_hints.py -v`
Expected: FAIL — `ImportError: cannot import name 'profile_hints'`

- [ ] **Step 3: Implementar `ingest/profile_hints.py`**

```python
# src/handoff_agent/ingest/profile_hints.py
"""What a Slack profile can tell the research before it starts.

The title often names the company ("CEO @ Acme") and a work email gives the
real domain, which spares the fragile guessed-domain path. Both are hints: a
wrong company here is caught by the grounding rules downstream, and a missing
one just means the research works from the name alone.
"""

from __future__ import annotations

import re

FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "proton.me", "protonmail.com", "gmx.com", "gmx.net", "yandex.com",
    "zoho.com", "hey.com", "fastmail.com", "mail.com",
})

ROLE_WORDS = frozenset({
    "ceo", "cto", "coo", "cfo", "cmo", "cro", "cpo", "founder", "co-founder",
    "cofounder", "co founder", "owner", "president", "partner", "director",
    "managing director", "vp", "head", "chair", "chairman", "fundador",
    "cofundador", "socio", "dueño",
})

# "@", " at ", " en ", "|", ",", "·" y guiones rodeados de espacios. Un guion
# pegado ("Co-founder") no separa.
_SEPARATORS = re.compile(r"\s*@\s*|\s+at\s+|\s+en\s+|\s*[|,·]\s*|\s+[-–—]\s+", re.IGNORECASE)
MAX_COMPANY_CHARS = 60


def company_from_title(title: str) -> str | None:
    parts = [part.strip() for part in _SEPARATORS.split(title or "") if part and part.strip()]
    if len(parts) < 2:
        return None
    candidate = parts[-1]
    if (
        candidate.casefold() in ROLE_WORDS
        or len(candidate) > MAX_COMPANY_CHARS
        or not re.search(r"[^\W\d_]", candidate)
    ):
        return None
    return candidate


def domain_from_email(email: str) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().casefold()
    if "." not in domain or domain in FREE_MAIL_DOMAINS:
        return None
    return domain
```

- [ ] **Step 4: Ejecutar los tests y la suite**

Run: `uv run pytest tests/test_profile_hints.py -v && uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/ingest/profile_hints.py tests/test_profile_hints.py
git commit -m "feat: empresa desde el título y dominio desde el email de trabajo del perfil de Slack"
```

---

### Task 10: Research runner — de la cola a `research_person`

**Files:**
- Create: `src/handoff_agent/ingest/research_runner.py`
- Test: `tests/test_research_runner.py`

**Interfaces:**
- Consumes: `queue` (Task 6), `profile_hints` (Task 9), `worker.research_person` y `worker.SYSTEM_STOPS` (Plan 2a), `SlackAuthFailed`/`SlackUnavailable` (Task 4), `FakeReader` (Task 7)
- Produces: `RunResult` — dataclass congelada: `status: str` (`limitado | hecho | omitido | reintento | fallido | detenido`), `slack_user_id: str | None = None`, `detail: str | None = None`; `run_next_job(reader) -> RunResult | None`

**Reglas:**

| Situación | La tarea queda | Qué devuelve |
|---|---|---|
| Se alcanzó `research_por_hora` | sin tocar | `limitado` |
| Cola vacía | — | `None` |
| Bot o usuario eliminado | `hecho`, outcome `omitido` | `omitido` |
| Research terminado | `hecho` con el outcome | `hecho` |
| `ValueError` (ni nombre ni empresa) | `fallido` sin reintento | `fallido` |
| Otro error del research o de Slack | reintento con espera (`fail`) | `reintento` o `fallido` |
| Kill switch, tope mensual o token caído | devuelta (`release`), sin gastar intento | se propaga la excepción |

- [ ] **Step 1: Escribir los tests que fallan**

```python
# tests/test_research_runner.py
from decimal import Decimal

import pytest

from handoff_agent import db, guards
from handoff_agent.ingest import queue
from handoff_agent.ingest import research_runner as runner
from handoff_agent.research.worker import ResearchOutcome
from handoff_agent.slack_client import SlackAuthFailed
from tests.slack_fakes import FakeReader


@pytest.fixture
def reader():
    fake = FakeReader()
    fake.add_profile("U1", "Ada Ruiz", title="CEO @ Acme", email="ada@acme.com")
    return fake


def stub_research(monkeypatch, error=None):
    seen = []

    def research(full_name=None, company=None, domain=None, slack_user_id=None, force=False):
        seen.append({"full_name": full_name, "company": company, "domain": domain,
                     "slack_user_id": slack_user_id})
        if error:
            raise error
        return ResearchOutcome("pid", slack_user_id, "investigado", 1, Decimal("0.02"), None)

    monkeypatch.setattr(runner.worker, "research_person", research)
    return seen


def job() -> dict:
    return db.fetch_one("select status, attempts, outcome, not_before from research_jobs")


def test_an_empty_queue_returns_none(conn, reader):
    assert runner.run_next_job(reader) is None


def test_a_job_is_researched_with_what_the_profile_tells_us(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    seen = stub_research(monkeypatch)
    assert runner.run_next_job(reader).status == "hecho"
    assert seen == [{"full_name": "Ada Ruiz", "company": "Acme", "domain": "acme.com",
                     "slack_user_id": "U1"}]
    assert job()["status"] == "hecho"
    assert job()["outcome"]["estado"] == "investigado"


@pytest.mark.parametrize("flag", ["is_bot", "deleted"])
def test_bots_and_deleted_users_are_skipped_without_research(conn, reader, monkeypatch, flag):
    reader.add_profile("U2", "Beep", **{flag: True})
    queue.enqueue("U2", "miembro_nuevo")
    seen = stub_research(monkeypatch)
    assert runner.run_next_job(reader).status == "omitido"
    assert seen == []
    assert job()["outcome"]["estado"] == "omitido"


@pytest.mark.parametrize("stop", [guards.MonthlyBudgetExceeded("tope"), guards.KillSwitchActive("off")])
def test_a_system_stop_hands_the_job_back_and_propagates(conn, reader, monkeypatch, stop):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch, error=stop)
    with pytest.raises(type(stop)):
        runner.run_next_job(reader)
    assert (job()["status"], job()["attempts"]) == ("pendiente", 0)


def test_a_slack_auth_failure_hands_the_job_back_and_propagates(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch)
    reader.auth_error = SlackAuthFailed("token_revoked")
    with pytest.raises(SlackAuthFailed):
        runner.run_next_job(reader)
    assert (job()["status"], job()["attempts"]) == ("pendiente", 0)


def test_a_research_crash_is_retried_later(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch, error=RuntimeError("SearXNG caído"))
    assert runner.run_next_job(reader).status == "reintento"
    assert job()["status"] == "pendiente"
    assert job()["not_before"] is not None


def test_nobody_to_research_is_given_up_at_once(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch, error=ValueError("hace falta al menos un nombre o una empresa"))
    assert runner.run_next_job(reader).status == "fallido"
    assert job()["status"] == "fallido"


def test_the_hourly_limit_holds_back_new_jobs(conn, reader, monkeypatch):
    queue.enqueue("U1", "mensaje")
    stub_research(monkeypatch)
    db.execute("update config set value = '0'::jsonb where key = 'research_por_hora'")
    try:
        assert runner.run_next_job(reader).status == "limitado"
        assert job()["status"] == "pendiente"
    finally:
        db.execute("update config set value = '20'::jsonb where key = 'research_por_hora'")
```

- [ ] **Step 2: Ejecutarlos y comprobar que fallan**

Run: `uv run pytest tests/test_research_runner.py -v`
Expected: FAIL — `ImportError: cannot import name 'research_runner'`

- [ ] **Step 3: Implementar `ingest/research_runner.py`**

```python
# src/handoff_agent/ingest/research_runner.py
"""Take one job from the queue and research that person.

The Slack profile feeds the research: the real name, a company parsed from the
title and, best of all, a company domain from a work email. System stops and
a dead token hand the job back untouched and propagate — they need a person,
not a retry.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..research import worker
from ..slack_client import SlackAuthFailed, SlackUnavailable
from . import queue
from .profile_hints import company_from_title, domain_from_email


@dataclass(frozen=True)
class RunResult:
    status: str
    slack_user_id: str | None = None
    detail: str | None = None


def _retry_or_fail(job_id, uid: str, error: str) -> RunResult:
    status = queue.fail(job_id, error)
    return RunResult("fallido" if status == "fallido" else "reintento", uid, error)


def run_next_job(reader) -> RunResult | None:
    if queue.finished_last_hour() >= queue.hourly_limit():
        return RunResult("limitado")
    job = queue.claim_next()
    if job is None:
        return None
    job_id, uid = job["id"], job["slack_user_id"]

    try:
        profile = reader.user_profile(uid)
    except SlackAuthFailed:
        queue.release(job_id)
        raise
    except SlackUnavailable as exc:
        return _retry_or_fail(job_id, uid, str(exc))

    if profile.is_bot or profile.deleted:
        reason = "bot o usuario eliminado"
        queue.complete(job_id, {"estado": "omitido", "motivo": reason})
        return RunResult("omitido", uid, reason)

    company = company_from_title(profile.title)
    domain = domain_from_email(profile.email)
    try:
        outcome = worker.research_person(
            profile.real_name or None, company, domain, slack_user_id=uid
        )
    except worker.SYSTEM_STOPS:
        queue.release(job_id)
        raise
    except ValueError as exc:  # ni nombre ni empresa: no hay a quién investigar
        queue.give_up(job_id, str(exc))
        return RunResult("fallido", uid, str(exc))
    except Exception as exc:  # noqa: BLE001 - se reintenta con espera
        return _retry_or_fail(job_id, uid, f"{type(exc).__name__}: {exc}")

    queue.complete(
        job_id,
        {
            "estado": outcome.status,
            "version": outcome.version,
            "coste_usd": str(outcome.cost_usd),
            "motivo": outcome.reason,
            "empresa": company,
            "dominio": domain,
        },
    )
    return RunResult("hecho", uid, outcome.status)
```

- [ ] **Step 4: Ejecutar los tests y la suite**

Run: `uv run pytest tests/test_research_runner.py -v && uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/ingest/research_runner.py tests/test_research_runner.py
git commit -m "feat: research runner que lleva cada tarea de la cola a research_person"
```

---

### Task 11: Bucle del worker y comandos de la CLI

**Files:**
- Create: `src/handoff_agent/ingest/loop.py`
- Modify: `src/handoff_agent/cli.py`
- Test: `tests/test_loop.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `watch_tick` (Task 7), `resolve_pending` (Task 8), `run_next_job`/`RunResult` (Task 10), `ops_alerts.alert` (Task 5), `FoundersClubReader`/`SlackAuthFailed` (Task 4)
- Produces: `IDLE_SECONDS = 30`, `SYSTEM_STOP_PAUSE_SECONDS = 600`; `run_loop(reader, *, channels, lookback_hours, poll_seconds, should_stop=..., sleep=time.sleep, clock=time.monotonic, max_cycles=None) -> int`; comandos `handoff vigilar`, `handoff cola`, `handoff worker`

- [ ] **Step 1: Escribir los tests del bucle**

```python
# tests/test_loop.py
import pytest

from handoff_agent import guards
from handoff_agent.ingest import loop
from handoff_agent.ingest.research_runner import RunResult
from handoff_agent.ingest.resolver import ResolveResult
from handoff_agent.ingest.watcher import TickResult
from handoff_agent.slack_client import SlackAuthFailed
from tests.slack_fakes import FakeReader


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def wiring(monkeypatch):
    calls = {"watch": 0, "alerts": []}

    def watch(*args, **kwargs):
        calls["watch"] += 1
        return TickResult()

    monkeypatch.setattr(loop, "watch_tick", watch)
    monkeypatch.setattr(loop, "resolve_pending", lambda: ResolveResult())
    monkeypatch.setattr(loop, "run_next_job", lambda reader: None)
    monkeypatch.setattr(loop.ops_alerts, "alert", lambda kind, msg: calls["alerts"].append(kind))
    return calls


def run(clock, max_cycles, poll_seconds=100):
    return loop.run_loop(
        FakeReader(), channels=["C1"], lookback_hours=1, poll_seconds=poll_seconds,
        sleep=clock.sleep, clock=clock.clock, max_cycles=max_cycles,
    )


def test_slack_is_read_at_start_and_then_once_per_poll_interval(wiring):
    clock = FakeClock()
    run(clock, max_cycles=10)
    # Cola vacía: cada vuelta duerme IDLE_SECONDS (30). En 10 vueltas el reloj
    # llega a 300 s y las lecturas caen en t=0, t=120 y t=240.
    assert wiring["watch"] == 3
    assert clock.sleeps == [loop.IDLE_SECONDS] * 10


def test_a_dead_token_raises_an_alert_and_the_loop_keeps_going(wiring, monkeypatch):
    def watch(*args, **kwargs):
        raise SlackAuthFailed("token_revoked")

    monkeypatch.setattr(loop, "watch_tick", watch)
    assert run(FakeClock(), max_cycles=2) == 2
    assert "slack_auth" in wiring["alerts"]


def test_a_system_stop_raises_an_alert_and_pauses(wiring, monkeypatch):
    def stopped(reader):
        raise guards.MonthlyBudgetExceeded("spent $150 of $150")

    monkeypatch.setattr(loop, "run_next_job", stopped)
    clock = FakeClock()
    run(clock, max_cycles=1)
    assert wiring["alerts"] == ["parada_del_sistema"]
    assert clock.sleeps == [loop.SYSTEM_STOP_PAUSE_SECONDS]


def test_a_busy_queue_does_not_sleep(wiring, monkeypatch):
    monkeypatch.setattr(loop, "run_next_job", lambda reader: RunResult("hecho", "U1"))
    clock = FakeClock()
    run(clock, max_cycles=3)
    assert clock.sleeps == []


def test_should_stop_ends_the_loop_before_any_work(wiring):
    cycles = loop.run_loop(
        FakeReader(), channels=["C1"], lookback_hours=1, poll_seconds=100,
        should_stop=lambda: True,
    )
    assert cycles == 0
    assert wiring["watch"] == 0
```

- [ ] **Step 2: Escribir los tests de la CLI**

Añadir a `tests/test_cli.py` (con `from tests.slack_fakes import FakeReader` arriba):

```python
def test_vigilar_without_a_token_explains_what_is_missing(capsys):
    assert cli.main(["vigilar"]) == 1
    assert "SLACK_USER_TOKEN" in capsys.readouterr().out


def test_vigilar_reads_once_and_resolves(conn, monkeypatch, capsys):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C1")
    fake = FakeReader()
    fake.post("C1", "U1", "hola", "9999999999.000001")
    monkeypatch.setattr(cli, "FoundersClubReader", lambda token: fake)
    monkeypatch.setattr(cli, "watch_tick", lambda reader, channels, lookback_hours: cli_tick())
    assert cli.main(["vigilar"]) == 0
    assert "mensajes nuevos" in capsys.readouterr().out


def cli_tick():
    from handoff_agent.ingest.watcher import TickResult

    return TickResult(messages_new=1)


def test_cola_prints_the_queue_counts(conn, capsys):
    from handoff_agent.ingest import queue

    queue.enqueue("U1", "mensaje")
    assert cli.main(["cola"]) == 0
    assert '"pendiente": 1' in capsys.readouterr().out


def test_worker_without_channels_explains_what_is_missing(monkeypatch, capsys):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    assert cli.main(["worker"]) == 1
    assert "SLACK_CHANNEL_IDS" in capsys.readouterr().out


def test_worker_runs_the_loop_with_the_settings(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C1,C2")
    monkeypatch.setenv("SLACK_POLL_SECONDS", "900")
    seen = {}
    monkeypatch.setattr(cli, "FoundersClubReader", lambda token: FakeReader())
    monkeypatch.setattr(cli, "run_loop", lambda reader, **kwargs: seen.update(kwargs) or 0)
    assert cli.main(["worker"]) == 0
    assert seen["channels"] == ["C1", "C2"]
    assert seen["poll_seconds"] == 900
```

- [ ] **Step 3: Ejecutarlos y comprobar que fallan**

Run: `uv run pytest tests/test_loop.py tests/test_cli.py -v`
Expected: FAIL — no existen `ingest.loop` ni los comandos nuevos.

- [ ] **Step 4: Implementar `ingest/loop.py`**

```python
# src/handoff_agent/ingest/loop.py
"""The long-running worker: read Slack once per poll interval, drain the
research queue in between.

A dead token or a system stop raises an operational alert and the loop keeps
running: the token may be renewed and the cap may be raised without anyone
restarting the process.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .. import ops_alerts
from ..research.worker import SYSTEM_STOPS
from ..slack_client import SlackAuthFailed
from .research_runner import run_next_job
from .resolver import resolve_pending
from .watcher import watch_tick

logger = logging.getLogger(__name__)
IDLE_SECONDS = 30
SYSTEM_STOP_PAUSE_SECONDS = 600


def run_loop(
    reader,
    *,
    channels: list[str],
    lookback_hours: float,
    poll_seconds: int,
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_cycles: int | None = None,
) -> int:
    next_watch = clock()
    cycles = 0
    while not should_stop():
        if clock() >= next_watch:
            next_watch = clock() + poll_seconds
            try:
                tick = watch_tick(reader, channels, lookback_hours)
                resolved = resolve_pending()
                logger.info(
                    "slack: %d mensajes nuevos, %d miembros nuevos; %d encolados",
                    tick.messages_new, tick.members_new, resolved.enqueued,
                )
                for error in tick.errors:
                    logger.warning("slack: %s", error)
            except SlackAuthFailed as exc:
                ops_alerts.alert("slack_auth", str(exc))

        try:
            result = run_next_job(reader)
        except SYSTEM_STOPS as exc:
            ops_alerts.alert("parada_del_sistema", str(exc))
            sleep(SYSTEM_STOP_PAUSE_SECONDS)
        except SlackAuthFailed as exc:
            ops_alerts.alert("slack_auth", str(exc))
            sleep(IDLE_SECONDS)
        else:
            if result is None or result.status == "limitado":
                sleep(IDLE_SECONDS)
            elif result.status != "hecho":
                logger.info("research %s: %s %s", result.slack_user_id, result.status, result.detail)

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
    return cycles
```

- [ ] **Step 5: Comandos en `cli.py`**

Imports nuevos (sin importar `mcp_server`: hay un test que lo impide):

```python
import logging
import signal
import threading

from . import ops_alerts
from .config import load_settings
from .ingest import queue
from .ingest.loop import run_loop
from .ingest.resolver import resolve_pending
from .ingest.watcher import watch_tick
from .slack_client import FoundersClubReader, SlackAuthFailed
```

Funciones:

```python
def _slack_reader(settings):
    """None, con un mensaje claro, si falta configuración de Slack."""
    if not settings.slack_user_token:
        print("falta SLACK_USER_TOKEN en .env (token de usuario xoxp del Founders Club)")
        return None
    if not settings.slack_channel_ids:
        print("falta SLACK_CHANNEL_IDS en .env (IDs de los canales a vigilar, separados por comas)")
        return None
    return FoundersClubReader(settings.slack_user_token)


def cmd_vigilar(args) -> int:
    settings = load_settings()
    reader = _slack_reader(settings)
    if reader is None:
        return 1
    try:
        tick = watch_tick(reader, list(settings.slack_channel_ids), settings.slack_lookback_hours)
    except SlackAuthFailed as exc:
        ops_alerts.alert("slack_auth", str(exc))
        print(f"detenido: {exc}")
        return 1
    resolved = resolve_pending()
    print(
        f"mensajes nuevos: {tick.messages_new} · ignorados: {tick.messages_ignored} · "
        f"miembros nuevos: {tick.members_new}"
    )
    print(
        f"encolados: {resolved.enqueued} · archivados: {resolved.archived} · "
        f"a scoring: {resolved.to_scoring}"
    )
    for error in tick.errors:
        print(f"error: {error}")
    return 0


def cmd_cola(args) -> int:
    print(json.dumps(queue.status_counts(), indent=2))
    for failure in queue.recent_failures():
        print(f"fallido {failure['slack_user_id']}: {failure['last_error']}")
    return 0


def cmd_worker(args) -> int:
    settings = load_settings()
    reader = _slack_reader(settings)
    if reader is None:
        return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    run_loop(
        reader,
        channels=list(settings.slack_channel_ids),
        lookback_hours=settings.slack_lookback_hours,
        poll_seconds=settings.slack_poll_seconds,
        should_stop=stop.is_set,
    )
    return 0
```

Y en `build_parser()`:

```python
    watch = sub.add_parser("vigilar", help="lee el Slack una vez y encola research")
    watch.set_defaults(func=cmd_vigilar)

    jobs_cmd = sub.add_parser("cola", help="estado de la cola de research")
    jobs_cmd.set_defaults(func=cmd_cola)

    loop_cmd = sub.add_parser("worker", help="vigila el Slack y procesa la cola sin parar")
    loop_cmd.set_defaults(func=cmd_worker)
```

- [ ] **Step 6: Ejecutar los tests y la suite**

Run: `uv run pytest tests/test_loop.py tests/test_cli.py -v && uv run pytest -q`
Expected: PASS, incluido `test_the_cli_does_not_load_the_mcp_server`.

- [ ] **Step 7: Commit**

```bash
git add src/handoff_agent/ingest/loop.py src/handoff_agent/cli.py tests/test_loop.py tests/test_cli.py
git commit -m "feat: bucle del worker y comandos vigilar, cola y worker"
```

---

### Task 12: Prueba de extremo a extremo y documentación

**Files:**
- Create: `tests/test_ingest_e2e.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: todo lo anterior; `ScriptedOpenAI` de `tests/test_synthesize.py`; `make_dossier` de `tests/test_dossier.py`; `FakeReader` de `tests/slack_fakes.py`.

- [ ] **Step 1: Escribir la prueba de extremo a extremo**

Solo se falsean las fronteras: Slack (`FakeReader`), las tres herramientas de red y el cliente de OpenAI. Todo lo demás corre de verdad contra la base de tests.

```python
# tests/test_ingest_e2e.py
import json

from handoff_agent import db, llm
from handoff_agent.ingest import queue
from handoff_agent.ingest.research_runner import run_next_job
from handoff_agent.ingest.resolver import resolve_pending
from handoff_agent.ingest.watcher import watch_tick
from handoff_agent.tools import jobs, search, web
from handoff_agent.tools.search import SearchResult
from handoff_agent.tools.web import PageContent, PageUnavailable
from tests.slack_fakes import FakeReader
from tests.test_dossier import make_dossier
from tests.test_synthesize import ScriptedOpenAI

NOW = 1_726_000_000.0
LINKEDIN = "https://www.linkedin.com/in/adaruiz"
HOME = "https://acme.com/"


def fake_tools(monkeypatch):
    def buscar(query, limit=8, prospect_id=None):
        if "linkedin.com/in" in query:
            return [SearchResult("Ada Ruiz - CEO - Acme", LINKEDIN, "CEO at Acme")]
        return []

    def leer(url, max_chars=20_000, prospect_id=None):
        if url == HOME:
            return PageContent(url, HOME, "Acme", "Acme builds logistics software. We are hiring support.")
        raise PageUnavailable(f"404 at {url}")

    monkeypatch.setattr(search, "buscar_web", buscar)
    monkeypatch.setattr(web, "leer_sitio", leer)
    monkeypatch.setattr(jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])


def grounded_dossier() -> str:
    return json.dumps(make_dossier(
        persona={"nombre": "Ada Ruiz", "cargo": "CEO", "fuente": LINKEDIN},
        empresa={**make_dossier()["empresa"], "fuentes": [HOME]},
        contratacion={"vacantes_abiertas": None, "roles": [], "roles_deslocalizables": [],
                      "fuentes": []},
        senales_contexto=[{"hecho": "Contrata soporte", "fuente": HOME}],
        busquedas_sugeridas=[],
    ))


def test_a_stranger_who_writes_ends_up_with_a_dossier(conn, monkeypatch):
    reader = FakeReader()
    reader.set_members("C1", "U1", "UBOT")
    reader.add_profile("U1", "Ada Ruiz", title="CEO @ Acme", email="ada@acme.com")
    reader.post("C1", "U1", "Our support queue is out of control", f"{NOW - 60:.6f}")
    reader.post("C1", "UBOT", "beep", f"{NOW - 50:.6f}", bot_id="B1")
    fake_tools(monkeypatch)
    monkeypatch.setattr(llm, "_client", lambda: ScriptedOpenAI(grounded_dossier()))

    tick = watch_tick(reader, ["C1"], lookback_hours=1, now=lambda: NOW)
    assert (tick.messages_new, tick.messages_ignored, tick.members_new) == (1, 1, 0)
    assert resolve_pending().enqueued == 1
    assert run_next_job(reader).status == "hecho"

    person = db.fetch_one(
        "select id, state, company_domain from prospects where slack_user_id = 'U1'"
    )
    assert person["state"] == "investigado"
    assert person["company_domain"] == "acme.com"
    assert db.fetch_one(
        "select version from dossiers where prospect_id = %s", (person["id"],)
    )["version"] == 1
    assert queue.status_counts() == {"hecho": 1}
    assert db.fetch_one(
        "select count(*) as n from research_jobs where slack_user_id = 'UBOT'"
    )["n"] == 0


def test_a_new_member_after_the_baseline_is_queued_once(conn):
    reader = FakeReader()
    reader.set_members("C1", "U1")
    watch_tick(reader, ["C1"], lookback_hours=1, now=lambda: NOW)
    reader.set_members("C1", "U1", "U2")
    watch_tick(reader, ["C1"], lookback_hours=1, now=lambda: NOW)
    watch_tick(reader, ["C1"], lookback_hours=1, now=lambda: NOW)
    assert db.fetch_all("select slack_user_id, reason from research_jobs") == [
        {"slack_user_id": "U2", "reason": "miembro_nuevo"}
    ]
```

- [ ] **Step 2: Ejecutarla**

Run: `uv run pytest tests/test_ingest_e2e.py -v`
Expected: PASS. Si falla, el fallo está en el cableado entre tareas: corregirlo en el módulo responsable, no en el test.

- [ ] **Step 3: Documentar en `README.md`**

Añadir una sección "Ingesta desde Slack (Plan 2b)" con:

- Los comandos `uv run handoff vigilar` (una lectura y resolución), `uv run handoff cola` (estado de la cola) y `uv run handoff worker` (bucle continuo; Ctrl+C para parar).
- Las variables nuevas: `SLACK_USER_TOKEN`, `SLACK_CHANNEL_IDS`, `SLACK_LOOKBACK_HOURS`, `SLACK_POLL_SECONDS`, `HANDOFF_ALERT_WEBHOOK_URL`, `BRAVE_SEARCH_API_KEY`, `PRICE_BRAVE_PER_QUERY`, y el límite `research_por_hora` en la tabla `config`.
- Cómo montar el Slack de pruebas:
  1. Crear un workspace de Slack de pruebas.
  2. En api.slack.com/apps → *Create New App* → *From scratch*, y en *OAuth & Permissions* añadir los **User Token Scopes** `channels:history`, `channels:read`, `groups:history`, `groups:read`, `users:read` y `users:read.email`. Ningún scope de escritura.
  3. *Install to Workspace* y copiar el *User OAuth Token* (`xoxp-…`) a `SLACK_USER_TOKEN`.
  4. Copiar el ID de cada canal (empieza por `C`) a `SLACK_CHANNEL_IDS`.
  5. `uv run handoff vigilar` (la primera lectura es solo línea base), escribir un mensaje con otra cuenta, `uv run handoff vigilar` otra vez para ver el encolado, y `uv run handoff worker` para investigarlo.
- Límites conocidos: todavía no se leen las respuestas dentro de hilos; sin backfill a propósito; el lock por persona solo cubre el camino automático (la cola), no las ejecuciones manuales de `handoff research`.
- Actualizar la tabla de estado (Plan 2b hecho) y la lista "Pendiente" (quitar lo que este plan cierra).

- [ ] **Step 4: Suite, ruff y comprobación final**

```bash
uv run pytest -q
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
uv run handoff vigilar   # sin token: debe explicar qué falta y salir con 1, sin tocar la red
```
Expected: todo en verde; el último comando imprime el aviso de `SLACK_USER_TOKEN`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_ingest_e2e.py README.md
git commit -m "test: ingesta de extremo a extremo y README del Plan 2b"
```

---

## Definición de terminado

- `uv run pytest` en verde contra `handoff_test`, sin warnings ni líneas "LEDGER GAP".
- `supabase migration up` aplicó la 0004 en desarrollo sin borrar datos.
- SearXNG con Bing, Mojeek, Qwant y Brave activos, comprobado en `/config`.
- `handoff vigilar`, `handoff cola` y `handoff worker` responden; sin token explican qué falta.
- La prueba de extremo a extremo lleva a un desconocido que escribe hasta un dossier guardado, sin investigar al bot ni encolar el padrón inicial.

## Validación que queda para cuando haya credenciales

- **Slack de pruebas:** seguir el runbook del README con un token real.
- **Brave:** poner `BRAVE_SEARCH_API_KEY`, lanzar una búsqueda y comprobar que `cost_events` recibe la fila `brave_search`; contrastar `PRICE_BRAVE_PER_QUERY` con la factura real.
