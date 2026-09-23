# Radar de cuentas objetivo — diseño

Fecha: 2026-09-22. Extiende el Founders Club Sales Agent
([spec](2026-09-09-founders-club-sales-agent-design.md)); no abre un proyecto nuevo.

## 1. Objetivo

Handoff tiene una lista de **empresas prospecto** (cuentas objetivo), estén o no en
HubSpot. El agente las vigila a diario, detecta cuándo abren vacantes y, por cada vacante
que vale la pena, encuentra a quien decide esa contratación, lo investiga y deja listo el
contacto: correo, borrador y tarea en HubSpot. **El agente recomienda; Anthony actúa.**

## 2. Fases

| Fase | Alcance | Depende de |
|---|---|---|
| 1. Radar | Cuentas (alta manual y CSV), escaneo diario de vacantes, señales con score, secciones Accounts y Signals del panel | JobSpy, Unipile (solo para resolver la empresa) |
| 2. Decisor | Rol → cargos que deciden (GPT-4.1), búsqueda en Sales Navigator vía Unipile, candidatos, research con el worker existente | Unipile |
| 3. Contacto | Correo (HubSpot → patrón + MX → proveedor), borrador GPT-4.1, contacto + tarea en HubSpot, sync de cuentas desde HubSpot | Token de app privada de HubSpot |

Este documento fija las tres; la implementación de hoy cubre las fases 1 y 2.

## 3. Cambio sobre el spec original: LinkedIn

El spec del 2026-09-09 decía "en V1 no se scrapea LinkedIn". Se cambia **de forma acotada**:
se usa la cuenta de Sales Navigator de Anthony vía Unipile, solo para lo que nadie más
puede hacer (resolver el id de empresa y buscar decisores). Las vacantes siguen saliendo
sin cuenta (JobSpy, superficie pública).

Reglas duras, todas en código:

- **Topes diarios en `config`** muy por debajo de lo que recomienda Unipile (~100 perfiles/día,
  100/día por ruta): `unipile_busquedas_por_dia` = 25, `unipile_perfiles_por_dia` = 40.
- **Horario humano**: solo de `unipile_hora_inicio` a `unipile_hora_fin` (8–19, hora de
  `unipile_zona`, `America/New_York`), de lunes a viernes, con pausa aleatoria de 4–15 s
  entre llamadas.
- **Solo lectura**: el cliente no tiene métodos para invitar, escribir ni enviar InMail.
- **Freno**: si Unipile responde que la cuenta no está `OK` (desconectada, checkpoint,
  credenciales), se escribe `unipile_pausado = true` en `config`, se avisa por el webhook
  de alertas y nada más toca LinkedIn hasta que una persona lo reactive.
- Cada llamada queda en `agent_actions` (`unipile_*`), que es de donde sale el conteo diario.

## 4. Modelo de datos (migración 0012)

`target_accounts`: `id`, `name`, `domain` (único si no es nulo), `linkedin_company_id`,
`linkedin_name`, `careers_url`, `hubspot_company_id`, `source` (`manual`|`csv`|`hubspot`),
`status` (`watching`|`paused`), `last_scan_at`, `last_scan_error`, timestamps.

`hiring_signals`: una fila por vacante y cuenta.
`account_id`, `title`, `title_key` (título normalizado; único con `account_id`),
`location`, `sources` (`text[]`: linkedin, indeed, careers), `urls` (`text[]`),
`first_seen_at`, `last_seen_at`, `closed_at`, `reposted` (bool), `score` (0–10),
`status` (`new`|`pursued`|`dismissed`|`snoozed`|`researching`|`ready`|`task_created`),
`snoozed_until`, `department`, timestamps.

`decision_candidates`: candidatos a decisor por señal. `signal_id`, `linkedin_id`,
`full_name`, `headline`, `profile_url`, `location`, `rank`, `reason`, `chosen` (bool),
`prospect_id` (cuando se investiga; FK a `prospects`).

El decisor elegido entra en `prospects` con `slack_user_id = 'li:<linkedin_id>'` y pasa
por la cola `research_jobs` como cualquier otra persona: mismo dossier, mismos topes.

Panel: lectura por RLS `is_panel_user()`; escritura solo por funciones `security definer`
(`panel_agregar_cuenta`, `panel_estado_cuenta`, `panel_accion_senal`), como en 0007/0008/0010.

## 5. Radar (fase 1)

`handoff radar` recorre las cuentas `watching` cuyo último escaneo tiene más de
`radar_horas_entre_escaneos` (20 h):

1. Si falta `linkedin_company_id`, se resuelve una vez vía Unipile
   (`/linkedin/search/parameters?type=COMPANY`), eligiendo el resultado cuyo nombre
   normalizado coincide; si ninguno coincide, se queda nulo y se sigue por nombre.
2. JobSpy: LinkedIn filtrado por `linkedin_company_ids` cuando hay id (preciso, sin cuenta)
   o por nombre con el filtro de empleador existente; e Indeed por nombre.
3. Upsert por `(account_id, title_key)`: nueva → `first_seen_at`; vista → `last_seen_at`;
   una cerrada que reaparece → `reposted = true`, `closed_at = null`.
4. Las no vistas en `radar_dias_para_cerrar` (3) días se marcan `closed_at`.
5. Score calculado por código:
   - vacante nueva: 3
   - abierta ≥ 30 días: +2
   - re-publicada: +2
   - ≥ 3 vacantes abiertas en la cuenta en 30 días: +2
   - vista en ≥ 2 fuentes: +1

   Tope 10.

Roles ignorados (`radar_roles_ignorados` en config: intern, internship, práctica, becario…)
no generan señal.

Un fallo de JobSpy o Unipile en una cuenta se anota en `last_scan_error` y el radar sigue
con la siguiente. `handoff worker` ejecuta el radar una vez por ciclo cuando toca.

## 6. Decisor (fase 2)

Una señal pasa a `pursued` por **Pursue** en el panel o sola si `score ≥ radar_auto_min`
(7). El bucle del worker procesa las `pursued`:

1. GPT-4.1 (salida estructurada) propone 2–4 cargos que deciden esa contratación, en el
   idioma probable de la empresa (ej. Mining Engineer → Gerente de Operaciones, Superintendente
   de Mina, VP Operations). Coste al ledger (`llm_calls`, stage `decisor_cargos`).
2. Una búsqueda de Sales Navigator por cargo, filtrada por `linkedin_company_id`, 5
   resultados cada una, respetando los topes.
3. Se deduplican y ordenan por código (coincidencia de cargo en el headline, seniority);
   se guardan como `decision_candidates`; el primero queda `chosen`.
4. El elegido entra en `prospects` + `research_jobs` (motivo `radar`); la señal pasa a
   `researching` y a `ready` cuando su dossier existe.

Sin `linkedin_company_id` o con Unipile pausado, la señal se queda `pursued` y el panel lo dice.

## 7. Contacto (fase 3, pendiente de token de HubSpot)

- Correo: contacto existente en HubSpot → patrón del dominio + validación MX → proveedor de
  pago detrás de una interfaz (Hunter/Prospeo), siempre con su origen y confianza.
- Borrador GPT-4.1 con la vacante, el dossier y el enlace al PDF de propuesta (`config`).
- HubSpot: crear/actualizar contacto, asociarlo a la empresa y crear una tarea para Anthony
  con el borrador. Nunca se envía nada. Unipile tiene conectado el Gmail de anthony@: el
  borrador podría dejarse también como draft en su bandeja (a decidir).
- Sync de cuentas: empresas de HubSpot con la propiedad `handoff_target = true`.

## 8. Panel

Menú: **Accounts · Signals** · People · Costs · Settings · Test mode.

- **Accounts**: lista (Company, Domain, Source, Open roles, Top signal, Last scan), filtros,
  **Add account**, **Pause/Resume**.
- **Account detail**: cabecera con enlaces y **Scan now**, tabla de vacantes con estado y
  **Pursue/Dismiss**, candidatos a decisor con enlace al dossier.
- **Signals**: bandeja de vacantes abiertas por score, con **Pursue / Dismiss / Snooze**.
- **Settings**: umbrales del radar y topes de Unipile, con el uso de hoy.

Todo lo visible, en inglés.

## 9. Pruebas

Tests contra la base de pruebas existente (migraciones desde cero) con JobSpy, Unipile y
OpenAI sustituidos (respx/monkeypatch): upsert y cierre de señales, re-publicación, score,
topes y horario de Unipile, freno por cuenta caída, decisor de extremo a extremo con
LLM falso, y RLS del panel para las tablas y funciones nuevas.

## 10. Riesgos

- Restricción de la cuenta de LinkedIn de Anthony (mitigado en §3; no es cero).
- La clave de Unipile se compartió por chat y da acceso también a buzones de Gmail del
  equipo: hay que rotarla.
- JobSpy depende de superficies públicas que se bloquean a menudo: el radar se degrada,
  no se cae, y el panel muestra el último error por cuenta.
- Coincidencias de nombre: una empresa con nombre genérico puede resolverse a otra en
  LinkedIn. El id resuelto se ve y se corrige en el panel.
