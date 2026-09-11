# Founders Club Sales Agent

Agente que vigila el Slack del Founders Club, investiga a quien participa y
entrega a Anthony dossiers accionables con un ángulo de acercamiento.
**El agente recomienda; Anthony actúa.**

- Diseño: [`docs/superpowers/specs/2026-09-09-founders-club-sales-agent-design.md`](docs/superpowers/specs/2026-09-09-founders-club-sales-agent-design.md)
- Plan 1: [`docs/superpowers/plans/2026-09-09-fundacion-toolbox-mcp.md`](docs/superpowers/plans/2026-09-09-fundacion-toolbox-mcp.md)
- Plan 2a: [`docs/superpowers/plans/2026-09-10-research-worker.md`](docs/superpowers/plans/2026-09-10-research-worker.md)

## Estado

| Plan | Alcance | Estado |
|---|---|---|
| 1. Fundación | Supabase, ledger de costes, guardarraíles, toolbox MCP | **Hecho** |
| 2a. Research worker | recolección, síntesis GPT-4.1, seguimiento, CLI `handoff`, `investigar_persona` | **Hecho** |
| 2b. Ingesta | slack-watcher, resolver | Pendiente |
| 3. Scoring y entrega | scoring, tarjeta de Slack, SMS, email, botones | Pendiente |
| 4. Panel web | Auth, listado, dossier, descarte, costes | Pendiente |

## Arrancar en local

Requisitos: Docker, [uv](https://docs.astral.sh/uv/), [Supabase CLI](https://supabase.com/docs/guides/cli).

```bash
uv sync
cp .env.example .env          # rellenar; ver la sección de credenciales
supabase start                # Postgres, Auth, PostgREST, Studio
supabase db reset             # aplica las migraciones desde cero
docker compose -f docker-compose.searxng.yml up -d
uv run pytest
```

Los puertos de Supabase están desplazados a `5433x` para no chocar con otros
proyectos Supabase de la misma máquina. `supabase status` da las URLs reales.

## Credenciales

Solo `DATABASE_URL` bloquea el arranque. El resto se validan donde se usan, así
que las herramientas que no tocan un LLM funcionan mientras se conecta lo demás.

| Variable | Hace falta para | Dónde se saca |
|---|---|---|
| `DATABASE_URL` | Todo | `supabase status` |
| `SEARXNG_SECRET` | Levantar SearXNG | `openssl rand -hex 32` |
| `OPENAI_API_KEY` | Cualquier herramienta con LLM | platform.openai.com |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Trazas (opcional) | cloud.langfuse.com |

Sin claves de Langfuse el trazado es un no-op silencioso: el ledger de costes
vive en Supabase y no depende de ningún tercero.

Los precios de `PRICE_INPUT_PER_M`, `PRICE_CACHED_INPUT_PER_M` y
`PRICE_OUTPUT_PER_M` se verificaron contra la página de OpenAI el 2026-09-10
($2,00 / $0,50 / $8,00 por millón en gpt-4.1). Toda la contabilidad sale de ellos:
si OpenAI los cambia o se cambia de modelo, hay que actualizarlos aquí.

## Usar el toolbox desde Claude Code

`.mcp.json` ya registra el servidor. Seis herramientas:

| Herramienta | Qué hace |
|---|---|
| `buscar_web` | Búsqueda web vía SearXNG self-hosted |
| `leer_sitio` | Descarga una página y devuelve texto limpio |
| `buscar_ofertas` | Vacantes abiertas de una empresa, vía JobSpy |
| `historial_prospecto` | Qué sabemos ya de una persona |
| `resumen_costes` | Gasto de los últimos N días |
| `investigar_persona` | Investiga a una persona y guarda el dossier (**cuesta dinero**) |

## Research desde la terminal

```bash
uv run handoff research "Ada Ruiz" --empresa Acme [--dominio acme.com] [--forzar]
uv run handoff research-batch empresas.csv --salida informe.md [--pausa 15]
uv run handoff dossier manual:ada-ruiz-acme     # último dossier guardado
uv run handoff costes --dias 30                 # gasto de los últimos N días
```

`research` y `research-batch` llaman a GPT-4.1 y gastan dinero. No se repite a
nadie con un dossier de menos de 6 meses salvo con `--forzar`; quien quedó
`incompleto` se vuelve a investigar la próxima vez, y lo mismo quien tenga como
último un dossier degradado (ver abajo). El CSV del lote lleva las
columnas `nombre,empresa,dominio`; `--pausa` son los segundos de espera entre
filas (15 por defecto), para no disparar el CAPTCHA de los buscadores.

**Antes de un lote pagado, comprobar la salud de SearXNG.** Los motores que usa
pueden quedar vetados por CAPTCHA, y entonces cualquier búsqueda vuelve vacía.
El research lo detecta —el motivo empieza por "búsqueda degradada"— pero el
dossier sale pobre y el dinero ya se ha gastado. Una consulta de prueba lo dice
en segundos (el comando va más abajo).

Qué pasa con un dossier degradado (la recolección inicial intentó buscar y
ninguna búsqueda —dominio, linkedin, prensa— trajo resultados):

- Se guarda igual, con `"degradado": true` en su contenido. Los dossiers sanos
  no llevan esa clave.
- La regla de los 6 meses no lo protege: la próxima ejecución lo vuelve a
  investigar sin `--forzar`.
- Quien era `nuevo` o `incompleto` queda `incompleto`. Quien ya estaba
  `investigado`, `contactado` o `cliente` conserva su estado; el motivo lo dice
  ("se conserva el estado …").
- Ojo con el coste: una empresa poco conocida con dominio conocido cuyas
  búsquedas vuelven vacías sin error de ningún motor también cuenta como
  degradada. Se vuelve a investigar —y a pagar— en cada lote hasta que alguna
  búsqueda devuelva algo.

Comprobación de salud de SearXNG:

```bash
curl -s "http://127.0.0.1:8080/search?q=test&format=json" | jq '.results | length, .unresponsive_engines'
```

## Invariantes del proyecto

Romper cualquiera de estas es un bug, no una preferencia:

1. **Todo cambio de esquema es un archivo en `supabase/migrations/`.** Lo que se
   crea clicando en Studio no viaja a Supabase Cloud.
2. **Ninguna llamada de pago sin pasar por el ledger.** El LLM se toca solo a
   través de `llm.complete()`; usar el SDK de OpenAI directamente se salta el
   kill switch y el registro de coste.
3. **El agente nunca escribe en el Slack del Founders Club.** Ni mensajes, ni
   reacciones, ni DMs.
4. **Todo lo que se descarga de la web es entrada hostil.** Las URLs vienen de
   buscadores y de links que pega gente en un Slack ajeno.
5. **RLS activado en toda tabla nueva.** Al migrar a Cloud la anon key es
   pública; denegar por defecto es lo único que separa internet de los dossiers.

## Comandos

```bash
uv run pytest                                   # 226 tests
supabase db reset                               # rehace el esquema desde cero
docker compose -f docker-compose.searxng.yml logs -f
```

La suite nunca toca los datos de desarrollo: en cada sesión borra y recrea la
base `handoff_test` desde las migraciones, conectándose como administrador por
`TEST_ADMIN_DATABASE_URL` (por defecto el Postgres local de Supabase en el
puerto 54332).

Parar todo el gasto sin redeploy:

```sql
update config set value = 'true'::jsonb where key = 'kill_switch';
```

## Pendiente para el Plan 2b

- **Lock por persona** alrededor de `research_person`: hoy dos ejecuciones
  simultáneas sobre la misma persona pagan el research dos veces.
- **Dominio adivinado**: `resolve_domain` toma el primer resultado que no es un
  directorio o una red social; puede equivocarse de empresa.
- **Fallback de búsqueda**: si SearXNG queda vetado no hay otro motor ni una API
  de búsqueda de pago detrás.
