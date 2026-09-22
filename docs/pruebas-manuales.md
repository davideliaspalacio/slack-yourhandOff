# Probar los flujos desde la terminal

**Antes de abrir una terminal:** la página **Test mode** del panel (después de
**Settings**) hace ahora lo mismo que la sección 1/2/3 de aquí abajo -- crear
una persona, mandar su mensaje y ver research, tarjeta y gasto -- sin
necesitar la terminal ni ninguna variable de entorno: rellena un formulario y
llama a `panel_simular_persona` (RLS, `supabase/migrations/0010_modo_pruebas.sql`),
que dispara el mismo `research_jobs` que drena el worker de Railway en
producción. El panel nunca ejecuta Python: solo escribe la fila que el worker
ya sabe recoger, así que investigar tarda lo que tarde el worker en pasar por
la cola (unos 2 minutos), no al instante. Un botón **Delete all test data**
en la misma página llama a `panel_borrar_simulados` y hace lo mismo que la
sección 9 (`limpiar`).

Esta terminal sigue haciendo falta para lo que el panel no puede: los tres
botones de la tarjeta firmados como Slack (sección 5), el SMS (sección 6), la
previsualización y el envío del resumen diario (sección 7) y, en general,
inspeccionar o limpiar cualquier cosa a mano en runs locales.

## Desde la terminal

`scripts/simular.py` ejercita el pipeline entero -- research, tarjeta, botones,
SMS, resumen diario -- sin depender del Slack del Founders Club: la app
lectora todavía no está instalada ahí, y crear cuentas de Slack solo para
probar no es una opción. Cada subcomando corre el código real (nunca una
copia): lo único que cambia es que el "perfil de Slack" sale de la base y de
lo que se pasa por línea de comandos, no de una llamada a Slack de verdad.

**Estos scripts nunca hablan con el Slack del Founders Club.** Las únicas
llamadas externas de verdad, según qué subcomando se use, son: OpenAI (el
research), Serper (la búsqueda web), el webhook de enriquecimiento, el bot de
Handoff publicando la tarjeta (Slack, pero el de Handoff), Twilio (SMS) y
Resend (el resumen diario).

Todos los comandos van contra `DATABASE_URL`. Por defecto exigen que sea la de
Supabase Cloud (igual que `tarjeta_prueba.py`/`tarjeta_real.py`); con
`--local` aceptan cualquier otra, pero entonces hace falta el stack local
levantado (`supabase start`, ver el arranque en el README). Contra Cloud
imprimen un aviso: **escriben en producción y gastan dinero real.**

## Antes de empezar

```bash
uv sync
```

Y, según el flujo, las variables de la tabla de más abajo en el entorno (o en
`.env`).

## 1. Mensaje entrante, sin correo de trabajo (dominio adivinado)

Simula a alguien escribiendo en el canal del Founders Club sin que su perfil
de Slack tenga un correo de empresa: se prueba el camino de "adivinar el
dominio y verificarlo" de `research/gather.py`.

```bash
uv run python scripts/simular.py mensaje "Ada Ruiz" "Acme" "buscamos alguien para soporte"
```

Qué hace: crea (o reutiliza) un prospecto con un `slack_user_id` falso pero
determinista (mismo nombre y empresa -> mismo id, así repetir el comando
reproduce el camino de "dossier vigente" en vez de crear a alguien nuevo cada
vez), guarda el mensaje en `slack_messages` tal como lo dejaría el vigilante
del Slack, corre `ingest.resolver.resolve_pending()` y después
`run_next_job` en bucle hasta vaciar la cola (tope de 5 vueltas).

Qué significa éxito: por cada tarea, una línea con el estado del research
(`investigado`/`incompleto`/...), el coste, la banda y si salió tarjeta
(consulta `deliveries`). Al final, el `prospect_id` y, si `PANEL_URL` está
configurado, el enlace a su ficha en el panel.

Coste: un research completo (búsqueda + GPT-4.1), unos pocos centavos. Si la
banda es media o alta y `HANDOFF_SLACK_BOT_TOKEN`/`HANDOFF_SLACK_CHANNEL_ID`
están puestos, sale una tarjeta de verdad en el Slack de Handoff.

Variables: `DATABASE_URL`, `OPENAI_API_KEY`; `SERPER_API_KEY` recomendada (sin
ella, SearXNG local); `HANDOFF_SLACK_BOT_TOKEN`/`HANDOFF_SLACK_CHANNEL_ID` si
se quiere ver la tarjeta de verdad; `PANEL_URL` opcional.

## 2. Mensaje entrante con correo de trabajo

Mismo comando, con `--email`: alimenta el perfil simulado con un correo de
empresa, así que `ingest.profile_hints.domain_from_email` da el dominio
directamente y el research nunca pasa por adivinar ni verificar.

```bash
uv run python scripts/simular.py mensaje "Leo Gil" "Beta Software" "hola" --email leo@betasoftware.com
```

## 3. "Company not confirmed" (dominio adivinado y no confirmado)

Para ver el aviso **"Company not confirmed: the guessed website (...)"** que
muestra el panel bajo **Help the research**, hace falta que la verificación
del dominio adivinado falle. Como esto depende de qué encuentra la búsqueda de
verdad (no es un flag del script), la manera de forzarlo es usar un nombre de
empresa común y sin dominio de correo:

```bash
uv run python scripts/simular.py mensaje "Sam Ortiz" "Acme" "quiero una demo"
```

Si `research/gather.py` no logra confirmar que el dominio adivinado es el de
verdad, el dossier queda con `empresa_no_confirmada`; `estado <prospect_id>`
lo confirma (columna "empresa_no_confirmada: sí"), y ese mismo aviso es el que
vería David en el panel. Una banda "alta" con la empresa sin confirmar baja a
"media" (`delivery/bands.py`): no dispara SMS.

## 4. El panel "Help the research" -> `cola`

Cuando alguien usa en el panel el botón **Save and re-research** de la
sección **Help the research** (empresa, web, enlaces, notas), la función de
Supabase `panel_ayudar_research` encola una tarea manual, pero nada la
procesa sola en local -- eso lo hace `handoff worker` en producción. Para
drenarla a mano:

```bash
uv run python scripts/simular.py cola
```

Repite el mismo bucle de research que `mensaje`, pero sin insertar ningún
mensaje nuevo: solo toma lo que ya esté `pendiente` en `research_jobs` (hasta
`--max`, 20 por defecto) y lo procesa con el mismo lector sin Slack. Sirve
igual después de tocar `company_domain_override`/`company_name_override` a
mano en la base, si no se quiere pasar por el panel.

```bash
uv run python scripts/simular.py cola --max 5
```

## 5. Los tres botones de la tarjeta

`POST /slack/acciones` es el receptor de los clics de **Contacted**,
**Discard** y (en tarjetas viejas) **Research more**. Para probarlo hace
falta tenerlo corriendo:

```bash
uv run handoff web
```

Y, en otra terminal, simular el clic ya firmado como lo firmaría Slack de
verdad (`SLACK_SIGNING_SECRET` tiene que ser el mismo en las dos terminales):

```bash
uv run python scripts/simular.py boton contactado <prospect_id>
uv run python scripts/simular.py boton descartar <prospect_id>
uv run python scripts/simular.py boton investigar_mas <prospect_id>
```

Si la persona ya tiene una tarjeta de verdad publicada (`deliveries`, kind
`slack`), el comando usa su canal y `ts` reales -- así el receptor también
intenta repintar la tarjeta. Si no, usa `HANDOFF_SLACK_CHANNEL_ID` y un `ts`
inventado (el repintado falla en silencio, como cualquier fallo de Slack: ver
`web/app.py:_repaint`).

Imprime el estado de la persona antes y después del clic, y el código HTTP
devuelto.

**Comprobación del 401 sin firma** (`SLACK_SIGNING_SECRET` mal puesto, o
alguien que no es Slack llamando a la URL):

```bash
uv run python scripts/simular.py boton contactado <prospect_id> --sin-firma
```

Tiene que salir `HTTP 401` y el estado de la persona sin cambiar.

Variables: `DATABASE_URL`, `SLACK_SIGNING_SECRET`, `HANDOFF_SLACK_CHANNEL_ID`
(si la persona no tiene tarjeta real); `--url` apunta a otro receptor si no es
`http://127.0.0.1:8000`.

## 6. SMS: horario y tope diario

```bash
uv run python scripts/simular.py sms <prospect_id>
```

Llama a `delivery.sms.maybe_send(prospect_id, "alta")` de verdad. Si
`TWILIO_*` está configurado y la persona no está fuera de horario
(`sms_hora_inicio`/`sms_hora_fin` en la tabla `config`, hora de
`zona_horaria`) ni se llegó al tope diario (`sms_por_dia`), manda un SMS real
y cuesta lo que cueste un SMS de Twilio (`PRICE_TWILIO_PER_SMS`, unos
centavos). Si no, dice por qué no salió: sin configurar, parada del sistema
(kill switch o tope mensual, leído de `agent_actions`), o probablemente fuera
de horario / tope diario alcanzado (`sms.maybe_send` no distingue estos dos
últimos casos en su valor de retorno; el comando enseña cuántos SMS van hoy
para poder revisarlo a ojo).

Para forzar el caso "fuera de horario" o "tope alcanzado" sin esperar al
reloj, ajusta la tabla `config` a mano:

```sql
update config set value = '3'::jsonb where key = 'sms_hora_inicio';
update config set value = '4'::jsonb where key = 'sms_hora_fin';
```

Variables: `DATABASE_URL`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_FROM`, `TWILIO_TO`.

## 7. El resumen diario

Previsualizar sin mandar nada (usa `digest.build`, nunca toca Resend):

```bash
uv run python scripts/simular.py digest --ver
uv run python scripts/simular.py digest --ver --dia 2026-09-20
```

Mandarlo de verdad (`delivery.digest.send_daily`; cuesta lo que cueste Resend,
`PRICE_RESEND_PER_EMAIL`, gratis en el nivel gratuito):

```bash
uv run python scripts/simular.py digest
```

Sin `--dia`, resume el día que le tocaría al cron real (ayer, en la zona
configurada). Un mismo día no se reenvía dos veces (`agent_actions` guarda la
marca); para forzar un reenvío en pruebas, borra esa fila a mano o usa
`--dia` con una fecha distinta.

Variables: `DATABASE_URL`, `RESEND_API_KEY`, `DIGEST_FROM`, `DIGEST_TO`.

## 8. Ver el estado de una persona

```bash
uv run python scripts/simular.py estado <prospect_id>
```

Estado, última versión de dossier, encaje y banda, si el dossier trae
`empresa_no_confirmada`/`datos_proveedor`, los overrides del panel (empresa,
web, enlaces, notas), sus entregas, tareas de research abiertas y el gasto
acumulado en esa persona.

## 9. Limpiar

Borra **solo** lo simulado: toda persona cuyo `slack_user_id` empiece por
`USIM` (este script) o `UPRUEBA` (`tarjeta_prueba.py`), en cascada (dossiers,
entregas, tareas, acciones), más sus `slack_messages`. Nunca toca nada más.

```bash
uv run python scripts/simular.py limpiar
```

Pide escribir `borrar` para confirmar; `--si` se lo salta (útil para
encadenar en un script de demo).

```bash
uv run python scripts/simular.py limpiar --si
```

## Variables de entorno usadas por estos comandos

| Variable | Hace falta para | De dónde sale |
|---|---|---|
| `DATABASE_URL` | Todos | `supabase status`, o la de Supabase Cloud |
| `OPENAI_API_KEY` | `mensaje`, `cola` (el research llama a GPT-4.1) | platform.openai.com |
| `SERPER_API_KEY` | `mensaje`, `cola` (si no, SearXNG local) | serper.dev |
| `HANDOFF_SLACK_BOT_TOKEN` | `mensaje`, `cola` (para que salga la tarjeta de verdad) | la app de Slack de Handoff |
| `HANDOFF_SLACK_CHANNEL_ID` | `mensaje`, `cola`, `boton` (canal de Handoff) | el canal de Handoff |
| `SLACK_SIGNING_SECRET` | `boton` | la app de Slack de Handoff |
| `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`/`TWILIO_FROM`/`TWILIO_TO` | `sms` | Twilio |
| `RESEND_API_KEY`/`DIGEST_FROM`/`DIGEST_TO` | `digest` (sin `--ver`) | Resend |
| `PANEL_URL` | `mensaje` (solo para imprimir el enlace) | Railway/donde se sirva el panel |

## Después de probar

`limpiar` borra los datos, pero no el gasto ya registrado en `llm_calls` /
`cost_events` (es dinero de verdad ya gastado; no tiene sentido "deshacerlo").
`uv run handoff costes --dias 1` da el gasto del día para llevar la cuenta.
