# Founders Club Sales Agent — Diseño

**Fecha:** 2026-09-09
**Estado:** validado, pendiente de plan de implementación
**Autor:** David (engineer) — cliente interno: Anthony (CEO, Handoff)
**Revisión:** 3 — la capa de datos pasa a Supabase (Docker local → Cloud); Langfuse pasa a Cloud

---

## 1. Contexto y objetivo

Handoff es una empresa de staffing. Anthony, el CEO, es miembro del **Founders Club**, una
comunidad de Slack con 1.400+ founders (ARR medio ~$17M, 18 empresas por encima de $100M).
En los primeros 90 días la comunidad ya produjo **6 clientes**.

El setup actual es un briefing diario, insuficiente para lo activo que está el canal: las
oportunidades se pasan porque nadie puede leerlo todo a tiempo.

**Objetivo:** un agente que vigile el Founders Club Slack, investigue a quien participa,
detecte oportunidades de venta y entregue a Anthony un dossier accionable con un ángulo de
acercamiento sugerido — antes de que la oportunidad se enfríe.

**Human-in-the-loop, sin excepción: el agente recomienda, Anthony actúa.**

---

## 2. Alcance

### Dentro (V1)

- Dos disparadores de research: **quien escribe** en el canal y **quien entra** como
  miembro nuevo.
- Motor de research: web, careers page, ofertas de trabajo, LinkedIn público.
- Puntuación de señales de compra sobre los mensajes, con el dossier ya disponible.
- Entrega: Slack de Handoff (canal de trabajo), Twilio SMS y email (solo aviso).
- Panel web: histórico, dossiers, descarte por persona y re-investigar.
- Capa agéntica sobre MCP y observabilidad de costes de extremo a extremo.

### Fuera (V1)

- **Backfill del padrón completo.** No se investiga a los 1.400. Quien nunca escribe ni
  acaba de entrar, no entra al sistema.
- Prefiltro de keywords que decida quién merece research. Se eliminó deliberadamente.
- Agente secundario de engagement en comunidad (bienvenidas, comentar intros, LinkedIn).
- CEO Command Center (Prioridad 2) y AI Council of Advisors (Prioridad 3): specs propios.
- Cualquier escritura del agente en el Founders Club Slack.
- CRM y edición de pipeline más allá de los estados de la sección 10.

---

## 3. Decisiones tomadas

| Decisión | Elegido | Razón |
|---|---|---|
| Disparador de research | Quien escribe (1ª vez) + quien entra como miembro nuevo | Cubre las dos visiones; ambos alimentan el mismo dossier por persona |
| Unidad de trabajo | **La persona, no el mensaje** | El research se paga una vez; los mensajes siguientes son baratos |
| Prefiltro de keywords | **Ninguno** | Perdía en silencio las señales blandas, que son las que peor casan con listas |
| Acceso a Slack | User token de Anthony (OAuth `xoxp`) | El workspace es de terceros; no hay vía de admin. Polling, sin Events API |
| Interfaz | Slack como canal de trabajo + panel web | Punto medio entre alertas puras y dashboard completo |
| Enriquecimiento | Open source primero, API de pago después | Coste; la capa queda tras una interfaz intercambiable |
| LLM | OpenAI GPT-4.1 para todo lo human-facing | Decisión de Handoff. Aislado tras interfaz propia |
| Arquitectura | Workers Python + Supabase, Docker | Encaja con el stack OSS y con Railway |
| Hosting | Railway | Ya es donde vive el backend de Handoff |
| Capa de datos | **Supabase** — Docker local vía CLI, migración a Cloud después | Postgres + Auth + PostgREST + Realtime + Studio en una pieza |
| Capa de herramientas | Servidor MCP | Un contrato, tres consumidores |
| Cola | Postgres de Supabase (`SELECT … FOR UPDATE SKIP LOCKED`) | Volumen bajo; un servicio menos que mantener |
| Observabilidad | Ledger en Supabase + **Langfuse Cloud** para trazas | El JOIN con deals exige el ledger propio; el árbol de ejecución exige Langfuse |

---

## 4. Arquitectura

El pipeline está **invertido respecto a lo habitual**: el research ocurre *antes* de
puntuar, no después. Cuando llega un mensaje, el sistema ya sabe quién lo escribió.

```
┌─ slack-watcher ──────────────────────────────────────┐
│  cada hora:                                          │
│   · conversations.history  → mensajes nuevos         │
│   · conversations.members  → diff vs snapshot        │
│  escribe eventos crudos en Supabase                  │
└──────────────────────────────────────────────────────┘
                        ↓
┌─ resolver ───────────────────────────────────────────┐
│  ¿el autor ya existe en `prospects`?                 │
│    no  → encola research                             │
│    sí, descartado → se archiva el mensaje y para     │
│    sí, activo → pasa directo a scoring               │
└──────────────────────────────────────────────────────┘
                        ↓
┌─ research (una vez por persona) ─────────────────────┐
│  SearXNG → dominio, LinkedIn público, prensa         │
│  Crawl4AI → home, about, /careers                    │
│  JobSpy → ofertas abiertas de esa empresa            │
│  GPT-4.1 → dossier estructurado                      │
└──────────────────────────────────────────────────────┘
                        ↓
┌─ scoring (por mensaje, con el dossier delante) ──────┐
│  GPT-4.1 → evidencia estructurada                    │
│  fórmula determinista → score y banda                │
│  GPT-4.1 → ángulo de acercamiento si supera umbral   │
└──────────────────────────────────────────────────────┘
                        ↓
┌─ delivery ───────────────────────────────────────────┐
│  Slack Handoff · Twilio SMS · email · panel web      │
└──────────────────────────────────────────────────────┘

Transversales:
  TOOLBOX (servidor MCP)        ← todos los workers pasan por aquí
  LEDGER (Supabase + Langfuse Cloud) ← toda acción y todo coste
Apoyo: SearXNG (Docker), Supabase (Docker local → Cloud), Langfuse Cloud
```

Los workers son procesos separados que se comunican por tablas. `research` puede ejecutarse
aislado sobre una persona concreta — así funciona el botón *re-investigar*.

### Por qué se invirtió el orden

Con el dossier disponible antes de puntuar, el clasificador deja de depender de que la
frase contenga palabras reconocibles. La misma frase cambia de valor según quién la diga:

> *"my week is just meetings and firefighting"*

- **Sin dossier:** ninguna keyword. Se pierde o pasa como ruido.
- **Con dossier:** lo dice el CEO de una empresa de 60 personas con 8 vacantes abiertas,
  4 de ops, que levantó Series A hace 3 meses. Señal caliente y evidente.

Esa es la fuente real de la inteligencia del sistema: contexto previo, no reglas más listas.

---

## 5. Stack open source

| Necesidad | Herramienta | Licencia | Nota |
|---|---|---|---|
| Búsqueda web | SearXNG self-hosted | AGPL | 70+ motores, sin API key ni coste por query |
| Lectura de sitios | Crawl4AI | Apache-2.0 | Markdown limpio listo para LLM |
| Extracción ligera | trafilatura | Apache-2.0 | Para páginas simples, sin navegador |
| Ofertas de trabajo | JobSpy (speedyapply) | MIT | LinkedIn/Indeed/Glassdoor/Google. **No requiere cuenta de LinkedIn** |
| Stack del sitio | ruleset webappanalyzer | MIT | Detección de tecnología |
| Slack | `slack_sdk` oficial | MIT | |
| SMS | SDK oficial de Twilio | MIT | |
| Observabilidad | Langfuse | MIT | Cloud (tier gratuito) en V1; self-hostable después sin cambiar SDK |

### LinkedIn: decisión explícita

StaffSpy hace lo que queremos pero **exige una cuenta real de LinkedIn y va contra sus
términos**. Proxycurl, que facturaba ~$10M/año, cerró en 2025 tras perder la demanda de
LinkedIn; el precedente legal existe y el riesgo inmediato es el baneo de la cuenta usada.

**En V1 no se scrapea LinkedIn.** El perfil se cubre solo con lo público indexado
(SearXNG + fetch). Al escalar a pago entra un proveedor compliant (Coresignal, PDL,
ScrapIn, Bright Data) detrás de la misma interfaz `EnrichmentProvider`.

---

## 6. Modelo de datos

```
slack_messages     mensajes crudos ingeridos (ts, canal, autor, texto, permalink)
member_snapshots   estado del padrón del canal por ejecución, para el diff
prospects          persona + empresa canónicas, con ESTADO (ver sección 10)
dossiers           salida del research: hallazgos, fuentes, fecha, versión
signals            señal detectada sobre un mensaje: evidencia, score, banda
deliveries         qué se envió, por qué canal, cuándo, con qué resultado
feedback           acciones de Anthony (contactado / descartar / investigar más)
agent_actions      log append-only: cada llamada a herramienta y cada decisión
llm_calls          modelo, tokens, coste, latencia, etapa, prospecto
cost_events        costes no-LLM unificados (Twilio, proxies, compute)
config             umbrales, topes de presupuesto, kill switch
```

`prospects` es la entidad canónica y la que lleva el estado. Un mismo founder detectado por
los dos caminos —entró como miembro nuevo y además escribió— es **una sola fila**. La
deduplicación por persona es lo que impide investigar y alertar dos veces.

`dossiers` guarda fecha y versión: un dossier caduca (sección 9) y se puede re-generar sin
perder el anterior.

### Supabase como capa de datos

**Entorno local:** la pila se levanta con la CLI, no con un `docker-compose.yml` propio:

```bash
supabase init && supabase start
```

Eso da Postgres, PostgREST, Auth (GoTrue), Realtime y Studio en Docker. **Migrar a Supabase
Cloud después es `supabase link` + `supabase db push`.**

**Regla innegociable:** todo cambio de esquema vive como archivo en `supabase/migrations/`.
Nada de crear o alterar tablas clicando en Studio — lo que se hace clicando no viaja a Cloud
y rompe la reproducibilidad del entorno.

**Reparto de accesos:**

| Consumidor | Vía | Rol |
|---|---|---|
| Workers (watcher, resolver, research, scoring, delivery) | Conexión directa a Postgres | `service_role`, se salta RLS |
| Panel web | PostgREST + Realtime | `authenticated`, sujeto a RLS |

Los workers necesitan conexión directa porque la cola usa `FOR UPDATE SKIP LOCKED`, que
PostgREST no expone. El panel no toca Postgres directamente: lee por PostgREST y se
actualiza en vivo por Realtime cuando entra un prospecto nuevo.

**RLS activado en todas las tablas.** El panel es interno y de pocos usuarios, pero las
políticas son baratas de escribir ahora y caras de retrofitear después, sobre todo con la
migración a Cloud por delante.

---

## 7. Capa agéntica: servidor MCP `handoff-tools`

Las herramientas se exponen como **servidor MCP**, no como funciones internas.

**Herramientas:** `buscar_web`, `leer_sitio`, `buscar_ofertas`, `historial_prospecto`,
`deals_cerrados_similares`, `puntuar_señal`, `redactar_acercamiento`, `reencolar_research`.

**Por qué MCP:** un solo contrato con tres consumidores — el pipeline automático por
function-calling de GPT-4.1; David y Jake desde Claude Code para depurar en vivo; y el CEO
Command Center (Prioridad 2) reutilizándolas sin reescribir nada.

### Contexto y bucle de retroalimentación

Antes de puntuar, el agente consulta por MCP:

1. **¿Qué sabemos ya de esta persona?** — su dossier, sus mensajes previos, su estado.
2. **¿Qué hizo Anthony con prospectos parecidos?** — descartes previos bajan el score.
3. **¿A qué se parecen los 6 deals cerrados?** — los datos históricos de Anthony entran
   como ejemplos recuperables, no hardcodeados en el prompt.

Cada acción de Anthony es una etiqueta de entrenamiento gratis. El sistema mejora con el
uso, sin sesiones de feedback ni etiquetado manual.

---

## 8. Observabilidad de costes

**Requisito explícito: absolutamente todo trazado.**

Dos sistemas con trabajos distintos, y la duplicación entre ellos es deliberada.

**Supabase — fuente de verdad.** `llm_calls` guarda modelo, tokens de entrada/salida/
cacheados, coste, latencia, etapa del pipeline y persona asociada. `cost_events` unifica lo
que no es LLM: cada SMS de Twilio, proxies, compute de Railway y cualquier API de pago que
entre después.

**Langfuse Cloud — depuración.** Trazas jerárquicas de cada ejecución agéntica, versionado
y comparación de prompts, y evals.

| | Supabase | Langfuse |
|---|---|---|
| Cuánto costó | ✅ | ✅ |
| JOIN con `prospects`, `feedback`, `deliveries` | ✅ | ❌ |
| Árbol de la ejecución agéntica | ❌ | ✅ |
| Versionado y comparación de prompts | ❌ | ✅ |

**Por qué el ledger no puede vivir solo en Langfuse:** la métrica de coste por deal cerrado
es un `JOIN` contra tablas de negocio. Langfuse guarda sus datos en otra base y ese JOIN no
existe.

**Por qué Langfuse no puede faltar:** cuando un research de $0,30 se dispara a $2, en
`llm_calls` se ven 14 filas sueltas; en Langfuse se ve que el agente entró en bucle
llamando a `buscar_web` siete veces con la misma query. Con una capa agéntica, el árbol es
la herramienta de depuración.

**Cloud y no self-hosted en V1:** Langfuse v3 self-hosted son 6 contenedores (web, worker,
ClickHouse, Redis, MinIO y su propio Postgres) con un mínimo de 4 vCPU y 8 GB de RAM.
Desproporcionado para ~20.000 llamadas al mes. Mismo SDK en Cloud; pasar a self-hosted
después es cambiar una URL base. Lo único que sale a un tercero son prompts y completions:
datos de negocio y dossiers se quedan en Supabase.

### Modelo de coste esperado

| Concepto | Estimación |
|---|---|
| Research de una persona (una vez) | ~$0,30 |
| Scoring de un mensaje con dossier | fracción de céntimo |
| Miembros nuevos | ~50/mes × $0,30 ≈ $15/mes |
| Evaluación de mensajes | ~$25/mes |

El gasto se concentra en las primeras semanas, mientras se investiga por primera vez a la
gente activa del canal. Después la mayoría de mensajes vienen de personas ya investigadas,
que es la parte barata, y el coste cae y se estabiliza.

**La palanca de coste no es el volumen de mensajes: es cuánta gente distinta escribe.**

**Medido el 2026-09-10** contra seis empresas reales (Task 9): el coste medio real por
dossier fue de **~$0,022** (GPT-4.1, sobre 6 dossiers investigados). Es una **cota
inferior**, no una estimación: se midió sobre evidencia pobre —de 1 a 3 fuentes de empresa
por dossier y la búsqueda fallando en parte del lote—, y con evidencia completa el prompt
crece y el coste con él. Aun así queda muy por debajo de la estimación de ~$0,30 de research
por persona de la tabla de arriba, que sigue como techo de aceptación.

### Métricas

| Métrica | Para qué |
|---|---|
| Coste por persona investigada | ¿es rentable el research? |
| Coste por alerta entregada | ¿cuánto cuesta lo que Anthony realmente mira? |
| **Coste por deal cerrado** | el número que justifica el agente entero |

### Frenos

- **Tope por ejecución** — superado el límite de llamadas o de céntimos, el research se
  corta y la persona queda marcada como *incompleta*. No sigue.
- **Tope mensual duro** — al alcanzarlo el pipeline deja de investigar gente nueva y sigue
  puntuando a los ya conocidos, avisando por Slack. Nunca muere en silencio.
- **Throttling del research** — límite de N personas/hora. Investigar en ráfaga tumba
  SearXNG y provoca bloqueos en JobSpy. La cola drena despacio, no hay prisa.
- **Nunca se re-investiga a un descartado.**
- **Kill switch** — fila en `config` que detiene todo sin redeploy.

---

## 9. Cerebro de detección

### Sin prefiltro

**Todo el que escribe se investiga, una vez, escriba lo que escriba.** No hay lista de
keywords decidiendo quién merece research.

La razón de eliminarlo: un prefiltro por inclusión es el único componente capaz de perder
dinero en silencio. Lo que descarta no lo ve el LLM, no lo ve Anthony y no aparece en
ningún log. Y falla justo donde más duele — *"Hiring a VA"* la caza cualquier lista, pero
*"I'm the bottleneck in every process"* no contiene una sola palabra de la lista y es
exactamente el founder que necesita staffing.

Se descarta **después y por persona**, con criterio humano en el panel, no antes y por regex.

### Dos cosas separadas

- **La persona** dispara research por el hecho de escribir. Una vez, y ya la tienes.
- **El mensaje** se puntúa aparte, con el dossier delante, y eso decide si hay alerta.

Un *"congrats!"* provoca research de quien lo escribió —una sola vez— y ninguna alerta.

### Taxonomía de señales

- **Explícitas** — hiring, VA, overseas/offshore, LATAM, Colombia, "anyone know a good
  recruiter", "looking for someone to handle X".
- **Blandas** — escalar equipo, quitarse carga personal, "I'm the bottleneck", "spending my
  whole day on ops", ronda levantada, "our support queue is out of control".
- **Contextuales** — vienen del dossier, no del mensaje: careers page con vacantes, ofertas
  en JobSpy, funding reciente. No disparan solas; **corroboran** y suben el score.

### El score lo calcula código, no el LLM

Pedirle al LLM un número del 0 al 100 da resultados inestables entre ejecuciones e
imposibles de ajustar sin reescribir el prompt. En su lugar el LLM **extrae evidencia
estructurada** y una fórmula determinista calcula el score:

```
intención      (explícita 3 / blanda 1 / ninguna 0)
fit            (¿roles deslocalizables a LATAM? 0-3)
timing         (frescura del mensaje, decae con las horas)
corroboración  (careers page / JobSpy confirman: +2)
histórico      (Anthony descartó similares: -2)
```

Reproducible, testeable con casos fijos y afinable cambiando pesos en config.

### Bandas de score y qué dispara cada una

Valores iniciales, viven en `config` y se recalibran en modo sombra:

| Score | Banda | Entrega |
|---|---|---|
| ≥ 8 | Alta | Slack inmediato + SMS (sujeto al tope de 3/día) |
| 5 – 7 | Media | Slack inmediato, sin SMS |
| 3 – 4 | Baja | Solo digest diario por email |
| < 3 | Descartada | Se archiva en `signals`, no se entrega |

Un miembro nuevo que aún no ha escrito no tiene mensaje que puntuar: entra al digest diario
con su dossier, nunca como alerta ni SMS.

### Caducidad de los dossiers

Un dossier de hace ocho meses miente: la empresa creció, cerró vacantes, cambió de rumbo.
Re-research a los **6 meses**, y solo de personas que no estén descartadas.

### El modo de fallo real es el ruido

Tres alertas malas seguidas y Anthony deja de abrirlas: el agente muere aunque funcione.
**El umbral arranca alto y se afloja con datos, nunca al revés.** Perder una oportunidad
cuesta menos que perder la atención de Anthony.

---

## 10. Estados, entrega y human-in-the-loop

### El estado vive en la persona

```
nuevo → investigado → ┬→ contactado → cliente
                      └→ descartado  (pegajoso)
```

**Descartado es por persona y permanente.** Sus mensajes se siguen guardando y puntuando
—el histórico sirve— pero **nunca generan alerta** y no se vuelve a gastar en research.
Reversible con un botón en el panel si Anthony cambia de opinión.

### La tarjeta de Slack es el producto

```
🔥 SEÑAL ALTA · score 9 · hace 40 min

Sarah Chen — CEO, Northwind Ops (~$24M ARR, 60 empleados)

Dijo en #general:
> "Honestly our support queue is eating my whole week.
>  Need to hire but US hires are brutal right now."

Por qué importa:
· Señal explícita de hiring + coste US como fricción → encaje directo
· 6 vacantes abiertas en su careers page, 4 son support/ops
· Levantó Series A hace 3 meses (TechCrunch)

Ángulo sugerido:
Mencionar el caso de [cliente similar], que pasó soporte a
Medellín y bajó coste 60% manteniendo cobertura US hours.

[ Ver mensaje original ]  [ Contactado ]  [ Descartar ]  [ Investigar más ]
```

**"Ver mensaje original"** es un permalink de Slack al hilo real. Todo el sistema existe
para producir ese clic.

### SMS y email

- **SMS:** solo banda Alta. Tope duro de **3 al día**, nada entre 21:00 y 07:00 en la zona
  horaria de Anthony. Sin dossier: una línea y el link a Slack.
- **Email:** digest diario — miembros nuevos investigados, señales de banda Baja y resumen
  de coste del día.

### El agente nunca escribe en el Founders Club

Ni mensajes, ni reacciones, ni DMs. Es lo acordado y es lo que mantiene el uso del token
dentro de lo defendible.

---

## 11. Panel web

Mínimo. **Login real con Supabase Auth** para Anthony, Jake y David — sustituye al link con
clave compartida del diseño inicial. Lee por PostgREST y se actualiza en vivo por Realtime,
sin capa de API propia que escribir.

**Lectura:** lista de personas con filtros por fecha, estado y banda de señal; detalle del
dossier con fuentes; historial de mensajes y scores de cada persona; pantalla de costes con
las métricas de la sección 8.

**Escritura — solo tres acciones:** cambiar estado (descartar / revivir / marcar
contactado), re-investigar, y ajustar umbrales de `config`.

Sin edición de dossiers, sin vista de pipeline, sin CRM.

---

## 12. Plan de despliegue por fases

0. **Base local** — `supabase start` en Docker, migraciones iniciales del esquema de la
   sección 6, y el toolbox MCP contra esa base. Nada de Slack todavía.
1. **Workspace de pruebas** — Slack propio con mensajes sintéticos que imitan los reales.
   Todo el desarrollo ocurre aquí. **No bloqueado por nada: empieza ya.**
2. **Modo sombra** — token de Anthony, lectura del histórico real del Founders Club, cero
   entregas. Se investiga a quien escribió en ese histórico, se validan los 6 deals
   cerrados y se mide la tasa de ruido. Aquí se ve el coste real del research inicial.
3. **Producción con freno** — entregas reales solo a David y Jake durante una semana.
4. **Anthony en vivo.**

---

## 13. Criterios de éxito

- **Bloqueante para desplegar:** en modo sombra, el agente detecta los 6 deals que ya
  cerraron. Si no los detecta, los umbrales están mal y no se despliega.
- Tasa de ruido medida sobre el histórico antes de que Anthony la sufra.
- Coste por deal cerrado visible desde el primer día.
- Anthony usa los botones. Si no los toca, el bucle de aprendizaje no existe y la tarjeta
  no está funcionando.

---

## 14. Riesgos

| Riesgo | Mitigación |
|---|---|
| Las reglas del Founders Club prohíben esto | Confirmación de Anthony antes de la Fase 2. El diseño no cambia |
| El token de Anthony se revoca o caduca | Fallo de auth → alerta a Slack de Handoff, nunca silencio |
| LinkedIn público da datos pobres | El dossier se sostiene en web + JobSpy; LinkedIn es extra, no cimiento |
| Ruido erosiona la confianza de Anthony | Umbral alto de inicio, tope de 3 SMS/día, modo sombra previo |
| Investigar a todo el que escribe dispara el coste inicial | Throttling, topes mensuales, y el hecho de que el gasto se satura solo |
| Ráfagas de research bloquean SearXNG/JobSpy | Cola con límite por hora, backoff y reintentos |
| Slack rate limits con user token | Backoff exponencial y ventana de polling ajustable |
| La migración local → Supabase Cloud se tuerce | Todo el esquema como migraciones desde el día uno; nada creado a mano en Studio |

---

## 15. Dependencias

**No bloqueantes para empezar** — el desarrollo arranca contra el workspace de pruebas.

- **Anthony:** token de Slack (OAuth de usuario), datos históricos de los 6 deals cerrados
  con sus conversaciones, número para Twilio, confirmación sobre las reglas de la comunidad.
  Necesario para la **Fase 2**.
- **Leidy:** acuerdo firmado y NDA.
- **Hasmin/Leidy:** Google Workspace, Slack y Claude Teams.
- **Jake:** alineación de alcance antes de tocar producción.

---

## 16. Después de la V1

- Agente de engagement de comunidad (bienvenidas, comentarios en intros, LinkedIn).
- Sustituir el enriquecimiento OSS por proveedor de pago tras `EnrichmentProvider`.
- CEO Command Center (Prioridad 2), reutilizando el toolbox MCP.
- AI Council of Advisors (Prioridad 3).

---

## Historial de cambios

**Rev. 3 (2026-09-09)** — La capa de datos pasa a Supabase, en Docker local vía CLI y con
migración a Supabase Cloud después. El panel gana login real con Supabase Auth y lectura por
PostgREST/Realtime en lugar de clave compartida. Langfuse pasa de self-hosted a Cloud: el
ledger de costes se queda en Supabase porque la métrica de coste por deal exige el JOIN con
tablas de negocio, y Langfuse queda para trazas, prompts y evals. Se añade la Fase 0.

**Rev. 2 (2026-09-09)** — Se elimina el prefiltro de keywords. La unidad de trabajo pasa de
mensaje a persona: todo el que escribe se investiga una vez, y el descarte ocurre después,
por persona y a criterio humano en el panel. El research se mueve *antes* del scoring, de
modo que cada mensaje se puntúa con el dossier de su autor ya disponible. Se descarta
explícitamente el backfill del padrón completo.

**Rev. 1 (2026-09-09)** — Diseño inicial validado por secciones.
