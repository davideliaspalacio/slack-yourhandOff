# Founders Club Sales Agent

Agente que vigila el Slack del Founders Club, investiga a quien participa y
entrega a Anthony dossiers accionables con un ángulo de acercamiento.
**El agente recomienda; Anthony actúa.**

- Diseño: [`docs/superpowers/specs/2026-09-09-founders-club-sales-agent-design.md`](docs/superpowers/specs/2026-09-09-founders-club-sales-agent-design.md)
- Plan 1: [`docs/superpowers/plans/2026-09-09-fundacion-toolbox-mcp.md`](docs/superpowers/plans/2026-09-09-fundacion-toolbox-mcp.md)

## Estado

| Plan | Alcance | Estado |
|---|---|---|
| 1. Fundación | Supabase, ledger de costes, guardarraíles, toolbox MCP | **Hecho** |
| 2. Ingesta y research | slack-watcher, resolver, research worker | Pendiente |
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

**Antes de producción:** contrastar `PRICE_INPUT_PER_M`, `PRICE_CACHED_INPUT_PER_M`
y `PRICE_OUTPUT_PER_M` con la página de precios de OpenAI. Los valores por
defecto son una estimación y de ellos sale toda la contabilidad.

## Usar el toolbox desde Claude Code

`.mcp.json` ya registra el servidor. Cinco herramientas:

| Herramienta | Qué hace |
|---|---|
| `buscar_web` | Búsqueda web vía SearXNG self-hosted |
| `leer_sitio` | Descarga una página y devuelve texto limpio |
| `buscar_ofertas` | Vacantes abiertas de una empresa, vía JobSpy |
| `historial_prospecto` | Qué sabemos ya de una persona |
| `resumen_costes` | Gasto de los últimos N días |

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
uv run pytest                                   # 79 tests
supabase db reset                               # rehace el esquema desde cero
docker compose -f docker-compose.searxng.yml logs -f
```

Parar todo el gasto sin redeploy:

```sql
update config set value = 'true'::jsonb where key = 'kill_switch';
```
