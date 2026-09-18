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
| 2b. Ingesta | slack-watcher, resolver, cola en Postgres, research runner, bucle `handoff worker`, Serper (resultados de Google) por delante de SearXNG | **Hecho** |
| 3. Scoring y entrega | scoring, tarjeta de Slack, SMS, email, botones | Pendiente |
| 4. Panel web | Login, listado, ficha con dossier, costes, ajustes y las tres acciones | **Hecho**, en la rama `feat/panel` |

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
| `LANGFUSE_BASE_URL` | Región del proyecto de Langfuse (opcional) | `https://cloud.langfuse.com` (Europa, por defecto) o `https://us.cloud.langfuse.com` (EE. UU.) |

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

## Ingesta desde Slack (Plan 2b)

Cada hora (o al llamar a mano) el agente lee los canales del Founders Club,
detecta quién escribió o quién es nuevo en el padrón, y encola su research. Una
cola en Postgres (`research_jobs`, una tarea abierta por persona) evita pagar
dos veces por la misma persona cuando dos disparadores coinciden.

```bash
uv run handoff vigilar   # una lectura de Slack + resolución de la cola
uv run handoff cola      # estado de la cola: pendientes, en curso, hechos, fallidos
uv run handoff worker    # vigila y procesa sin parar; Ctrl+C para detener
```

`vigilar` y `worker` fallan con un aviso claro y código 1 si falta
`SLACK_USER_TOKEN` o `SLACK_CHANNEL_IDS`, sin tocar la red.

### Variables nuevas

| Variable | Hace falta para | Dónde se saca |
|---|---|---|
| `SLACK_USER_TOKEN` | Leer el Slack del Founders Club | api.slack.com/apps → OAuth & Permissions, User OAuth Token |
| `SLACK_CHANNEL_IDS` | Qué canales vigilar (IDs separados por comas) | el ID de cada canal, empieza por `C` |
| `SLACK_LOOKBACK_HOURS` | Cuánto mira hacia atrás la primera lectura de un canal | por defecto 1 |
| `SLACK_POLL_SECONDS` | Cada cuánto vuelve a leer `handoff worker` | por defecto 3600 |
| `HANDOFF_ALERT_WEBHOOK_URL` | Avisos operativos (fallo de auth de Slack, ciclo del worker caído) | un webhook del Slack de Handoff, nunca el del Founders Club |
| `SERPER_API_KEY` | Búsqueda web con resultados de Google, sin CAPTCHA (Serper antes que SearXNG) | serper.dev → *API Key* |
| `PRICE_SERPER_PER_QUERY` | Contabilizar el coste de Serper en `cost_events` | factura de Serper; por defecto $0,001 |

Además, en la tabla `config` hay un tope nuevo:

```sql
insert into config (key, value) values ('research_por_hora', '20'::jsonb)
    on conflict (key) do update set value = excluded.value;
```

`research_por_hora` limita cuántas tareas puede terminar `handoff worker` en
una hora (20 si la fila no existe). Un valor que no sea un entero no negativo
se ignora, con un aviso en el log, y se usa el valor por defecto.

### Montar un Slack de pruebas

1. Crear un workspace de Slack de pruebas.
2. En api.slack.com/apps → *Create New App* → *From scratch*, y en *OAuth &
   Permissions* añadir los **User Token Scopes** `channels:history`,
   `channels:read`, `groups:history`, `groups:read`, `users:read` y
   `users:read.email`. Ningún scope de escritura.
3. *Install to Workspace* y copiar el *User OAuth Token* (`xoxp-…`) a
   `SLACK_USER_TOKEN`.
4. Copiar el ID de cada canal (empieza por `C`) a `SLACK_CHANNEL_IDS`.
5. `uv run handoff vigilar` (la primera lectura es solo línea base), escribir
   un mensaje con otra cuenta, `uv run handoff vigilar` otra vez para ver el
   encolado, y `uv run handoff worker` para investigarlo.

### Límites conocidos

- Todavía no se leen las respuestas dentro de hilos (`conversations.replies`).
- Sin backfill a propósito: la primera lectura de un canal nunca encola a todo
  el padrón (ver Invariantes, más abajo).
- El lock por persona (una tarea de research abierta por persona) solo cubre
  el camino automático, la cola. Las ejecuciones manuales de
  `handoff research` no lo comprueban: dos a la vez sobre la misma persona
  pagan el research dos veces.

## Desplegar el worker en Railway

El worker (`handoff worker`) corre como un servicio siempre encendido, sin
puerto ni web. Railway lee `railway.json` y construye el `Dockerfile` del repo.

### Pasos

1. En Railway: *New Project* → *Deploy from GitHub repo* → este repositorio.
2. Cargar las variables de la tabla de abajo en el servicio.
3. Desplegar y revisar los logs: tiene que aparecer una línea `slack: N mensajes
   nuevos...` por cada lectura.
4. Comprobar en *Settings* que el servicio tiene **una sola réplica**.

Las migraciones no se aplican al desplegar: se suben con `supabase db push`.

### Variables

| Variable | Obligatoria | Nota |
|---|---|---|
| `DATABASE_URL` | Sí | Cadena **Session pooler** de Supabase Cloud (*Connect* en el panel). La conexión directa no funciona en redes solo IPv4. |
| `OPENAI_API_KEY` | Sí | Clave de producción, no la de desarrollo. |
| `SLACK_USER_TOKEN` | Sí | Token `xoxp` de solo lectura. Sin él, el worker explica qué falta y sale. |
| `SLACK_CHANNEL_IDS` | Sí | IDs de canal separados por comas. |
| `SERPER_API_KEY` | Sí, salvo que se despliegue SearXNG | `SEARXNG_URL` apunta por defecto a `127.0.0.1`, que en Railway no existe: sin Serper no hay búsqueda. |
| `HANDOFF_ALERT_WEBHOOK_URL` | Recomendada | Sin ella los avisos solo quedan en los logs. |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | No | Sin ellas no hay trazas; el gasto se registra igual. |
| `LANGFUSE_BASE_URL` | Solo con Langfuse | Tiene que coincidir con la región del proyecto. Por defecto es Europa: un proyecto de EE. UU. necesita `https://us.cloud.langfuse.com` o las trazas no llegan. |
| `SLACK_POLL_SECONDS`, `SLACK_LOOKBACK_HOURS` | No | 3600 y 1 por defecto. |
| `OPENAI_MODEL`, `PRICE_*`, `HTTP_TIMEOUT_SECONDS` | No | Tienen valores por defecto (ver `.env.example`). |

### Por qué `railway.json` está así

- **`numReplicas: 1`.** La cola reclama por reloj las tareas de más de 30 minutos
  y no distingue un worker muerto de uno lento: con dos réplicas, una
  investigación lenta puede pagarse dos veces.
- **`overlapSeconds: 0`.** Es el tiempo que el despliegue viejo convive con el
  nuevo *antes* de recibir la señal de parada. Durante ese solape los dos cogen
  tareas, que es el mismo riesgo que tener dos réplicas.
- **`drainingSeconds: 300`.** Margen entre el SIGTERM y el SIGKILL. Aquí sí
  pueden convivir dos: el viejo, al recibir la señal, deja de coger tareas y
  solo termina la que tiene entre manos. Si una investigación dura más, la tarea
  se recupera sola a los 30 minutos. Con la base caída, el apagado puede tardar
  hasta 30 segundos más: la espera de una conexión no se entera de la señal.
  Comprobado en local, con el contenedor sin red: mientras el worker está en una
  de sus esperas, `docker stop` sale limpio (código 0) en menos de un segundo.
- **Sin `startCommand`.** Arranca el `CMD` del `Dockerfile`, en forma exec, para
  que Python sea el PID 1 y reciba la señal de parada. Un shell delante se la
  tragaría.
- **`watchPatterns`.** Solo redespliega si cambia el código, las dependencias o
  la propia configuración. Cada redespliegue corta una investigación en curso,
  así que un cambio en `docs/` no debe provocar uno.
- **`sleepApplication: false`.** El worker no recibe tráfico; Railway no debe
  dormirlo por inactividad.

### Probar la imagen en local

```bash
docker build -t handoff-worker:local .
docker run --rm --env-file .env handoff-worker:local handoff cola
```

Con `--env-file .env`, `DATABASE_URL` apunta a `127.0.0.1`, que dentro del
contenedor es el propio contenedor: para llegar a la base local hay que
cambiarlo por `host.docker.internal`.

El primer build tarda unos minutos: `python-jobspy` fija `numpy==1.26.3`, que
no tiene wheel para Python 3.13 y se compila. Los siguientes reutilizan esa capa
mientras no cambie `uv.lock`.

### Segundo servicio: el resumen diario

El email diario (`handoff digest`) no comparte servicio con el worker: es un
segundo servicio de Railway sobre el mismo repositorio, con su propia
configuración (`railway.digest.json` en vez de `railway.json`).

1. En el mismo proyecto de Railway: *New* → *GitHub Repo* → el mismo
   repositorio.
2. En *Settings* → *Config-as-code*, apuntar el servicio a
   `railway.digest.json`.
3. Cargar `DATABASE_URL` (la misma que el worker) más `RESEND_API_KEY`,
   `DIGEST_FROM` y `DIGEST_TO` (ver `.env.example`).
4. Railway ejecuta `handoff digest` con el cron `0 13 * * *` (9:00 en Nueva
   York) y sale solo. `restartPolicyType: "NEVER"`: a diferencia del worker,
   un cron no debe reintentarse solo porque el proceso saliera con código
   distinto de cero -- un email que ya salió no puede reenviarse por un
   reintento ciego (`digest.send_daily` ya se protege de esto por su cuenta,
   pero tampoco hace falta que Railway lo intente).

### Tercer servicio: el receptor de los botones

Los botones de la tarjeta ("Contactado", "Descartar", "Investigar más")
mandan un `block_actions` a `POST /slack/acciones`. Es un tercer servicio de
Railway (`handoff web`, FastAPI servido con Uvicorn) sobre el mismo
repositorio, con su propia configuración (`railway.web.json`).

1. En el mismo proyecto de Railway: *New* → *GitHub Repo* → el mismo
   repositorio.
2. En *Settings* → *Config-as-code*, apuntar el servicio a
   `railway.web.json`.
3. En *Settings* → *Networking*, pulsar **Generate Domain** para obtener un
   dominio público -- este servicio, a diferencia del worker y del digest, sí
   recibe tráfico de fuera.
4. En la app de Slack: *Interactivity & Shortcuts* → **Request URL**:
   `https://<dominio>/slack/acciones`.
5. Cargar las variables de la tabla de abajo.

### Variables

| Variable | Obligatoria | Nota |
|---|---|---|
| `DATABASE_URL` | Sí | La misma que el worker. |
| `HANDOFF_SLACK_BOT_TOKEN` | Sí | El mismo token de bot que usa `slack_writer` para publicar y repintar la tarjeta. |
| `HANDOFF_SLACK_CHANNEL_ID` | Sí | El canal de Handoff; cualquier `channel.id` que llegue distinto se rechaza (200, sin tocar la base). |
| `SLACK_SIGNING_SECRET` | Sí | En la app de Slack: *Settings* → *Basic Information* → **Signing Secret**. Sin ella, el servicio rechaza toda petición con 401 -- falla cerrado. |

`handoff web` lee `$PORT` del entorno en tiempo de ejecución, así que el
`startCommand` de `railway.web.json` puede ser `"handoff web"` sin shell de
por medio: Railway no expande `$PORT` en un comando así, pero el proceso
Python sí lo lee él mismo.

## Panel web (Plan 4)

Next.js en `panel/`. Lee Supabase directamente con la sesión del usuario: no hay
API propia, y quien decide qué se ve y qué se puede cambiar son las políticas
RLS de `supabase/migrations/0006_panel.sql`.

```bash
cd panel && npm install
npm run dev          # http://localhost:3000
```

Necesita `panel/.env.local` con `NEXT_PUBLIC_SUPABASE_URL` y
`NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` (la clave pública, nunca la de servicio).

**Quién entra.** Supabase deja registrarse a cualquiera, así que iniciar sesión
no basta: solo ven datos los correos de la tabla `panel_users`. No están en el
repositorio porque es público; se añaden a mano:

```sql
insert into panel_users (email) values ('nombre@yourhandoff.com');
```

**Qué puede hacer.** Leer personas, dossiers, mensajes, entregas y costes, y
tres acciones: cambiar el estado de una persona, volver a investigarla y ajustar
umbrales. No puede borrar, ni escribir dossiers, ni tocar el apagado de
emergencia o los topes de dinero. Todo eso lo comprueba `tests/test_panel_rls.py`.

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
uv run pytest                                   # 428 tests
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

## Pendiente

- **Dominio adivinado**: `resolve_domain` toma el primer resultado que no es un
  directorio o una red social; sigue pudiendo equivocarse de empresa (más
  estricto desde el Plan 2b, pero no infalible).
- **Lock por persona en la CLI manual**: `handoff research`/`research-batch` no
  pasan por la cola, así que su propio candado sigue pendiente (ver "Límites
  conocidos" de la ingesta, arriba).
- **Respuestas en hilos, scoring y permalinks**: fuera de este plan, quedan
  para el Plan 3.
