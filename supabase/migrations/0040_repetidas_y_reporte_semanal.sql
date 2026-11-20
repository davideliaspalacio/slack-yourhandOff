-- Una tarjeta por persona, y las empresas que solo contratan, una vez a la semana.
--
-- Dos cosas que AG señaló el 2026-11-20:
--
-- 1. **Tarjetas repetidas** (un fallo). Desde el 7 de noviembre salieron 71 tarjetas
--    para 17 personas; una recibió 12 en tres días. La cadena: el diff de padrones
--    encolaba `miembro_nuevo` una vez por canal donde aparecía la persona; un dossier
--    degradado se rehacía entero cada vez; y cada dossier nuevo es una versión nueva,
--    que `deliveries_one_card` (una tarjeta por persona y VERSIÓN) no frena. Se arregla
--    en las tres capas (`ingest/queue.py`, `research/reintentos.py`,
--    `delivery/deliver.py`); aquí están los tres plazos que se dejan ajustar.
--
-- 2. **"Tiene vacantes abiertas" no es una señal alta** (una petición). Cuando la
--    única prueba del encaje son vacantes publicadas, la persona no pidió nada y AG no
--    puede hacer nada con el aviso. Esas fichas dejan de salir como tarjeta
--    (`delivery/bands.py`) y salen juntas en un reporte semanal
--    (`delivery/reporte_contratando.py`).
--
-- Nada de esto vacía un atraso al aplicarse: no se reencola a nadie, no se republica
-- ninguna tarjeta, y el primer reporte solo mira su ventana de días.
--
-- 0001 a 0039 ya están aplicadas: nunca se tocan. Esto solo añade.

-- `enqueue_miembro_nuevo` pregunta por las tareas de una persona (también las ya
-- terminadas, que el índice parcial de 0004 no cubre), y lo mismo hace el aviso de
-- miembro nuevo (0037). La tabla solo crece.
create index if not exists research_jobs_user_idx on public.research_jobs (slack_user_id);

-- Un reporte por semana, y la reserva que impide publicarlo dos veces. `semana` es el
-- lunes de la semana en `zona_horaria`. La fila se escribe ANTES de publicar, igual que
-- en `resumenes_prioridad` (0039): dos workers, o un reintento, chocan con la clave
-- primaria y solo uno publica.
create table public.reportes_contratando (
    semana         date primary key,
    -- `reservado`: se está publicando (o se cayó a medias: esa semana ya no sale, que
    -- es mejor que salir dos veces). `publicado`: salió. `vacio`: a la hora no había
    -- ninguna empresa que listar, y la semana queda cerrada sin mensaje. `fallido`:
    -- Slack falló; se reintenta en la pasada siguiente hasta agotar los intentos.
    estado         text not null
                   check (estado in ('reservado', 'publicado', 'vacio', 'fallido')),
    intentos       integer not null default 1,
    -- Cuántas empresas se listaron y cuántas había en total.
    lineas         integer not null default 0,
    total          integer not null default 0,
    channel_id     text,
    message_ts     text,
    created_at     timestamptz not null default now(),
    actualizado_at timestamptz not null default now()
);
alter table public.reportes_contratando enable row level security;
-- Contabilidad interna del worker, como `resumenes_prioridad` (0039): ni el panel ni
-- la API la leen.
revoke all on public.reportes_contratando from anon, authenticated;

-- Qué ficha salió en qué reporte. Una empresa "vista contratando" se anuncia una vez:
-- quien ya salió no vuelve a salir en un mes (`reporte_contratando.NO_REPETIR_DIAS`),
-- aunque la ventana de días sea más larga que una semana o se le rehaga el dossier.
-- La versión del dossier se guarda para saber qué se leyó al listarla.
create table public.reportes_contratando_lineas (
    prospect_id     uuid not null references public.prospects (id) on delete cascade,
    semana          date not null references public.reportes_contratando (semana)
                    on delete cascade,
    dossier_version integer not null,
    created_at      timestamptz not null default now(),
    primary key (prospect_id, semana)
);
alter table public.reportes_contratando_lineas enable row level security;
revoke all on public.reportes_contratando_lineas from anon, authenticated;

insert into public.config (key, value) values
    -- Días en los que una persona no recibe otra tarjeta (ni otro SMS) salvo que su
    -- banda suba o alguien pida el research a mano. Los mismos 7 que
    -- `alertas_dias_misma_persona`. Cero lo apaga: cada versión del dossier, su tarjeta.
    ('tarjeta_dias_misma_persona', '7'::jsonb),
    -- Horas mínimas entre dos research automáticos de alguien cuyo último intento salió
    -- degradado o incompleto. Un buscador vetado vuelve en horas; una empresa sin
    -- huella en la web no mejora por preguntar otra vez.
    ('research_reintento_horas', '24'::jsonb),
    -- Cuántos reintentos automáticos como mucho tras un research degradado o
    -- incompleto: con 2 son tres research pagados por persona. Después, solo a mano.
    ('research_reintentos_max', '2'::jsonb),
    -- La regla de "solo vacantes": encendida, una ficha cuya única evidencia son
    -- vacantes publicadas no tiene tarjeta ni SMS y va al reporte semanal. Apagada,
    -- todo sale como antes de esta migración.
    ('vacantes_solo_reporte', 'true'::jsonb),
    -- El reporte semanal de empresas contratando.
    ('reporte_contratando_activo', 'true'::jsonb),
    -- Qué día sale: 1 es lunes y 7 domingo (ISO), en `zona_horaria`.
    ('reporte_contratando_dia', '1'::jsonb),
    -- A partir de qué hora, de 0 a 23, en `zona_horaria`. Una hora después del resumen
    -- del día, para que no lleguen pegados.
    ('reporte_contratando_hora', '9'::jsonb),
    -- Cuántos días hacia atrás mira: las empresas con vacantes vistas en ese plazo. El
    -- primer reporte tras aplicar esto mira lo mismo, ni un día más.
    ('reporte_contratando_dias', '7'::jsonb),
    -- Cuántas empresas como mucho. AG pidió "the 15 companies hiring right now".
    ('reporte_contratando_max', '15'::jsonb)
on conflict (key) do nothing;

-- Política propia, como cada grupo de claves desde 0012: booleanos los dos
-- interruptores, enteros acotados los numéricos, y el mismo cast protegido de 0023.
create policy panel_ajusta_repetidas_y_reporte on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('tarjeta_dias_misma_persona', 'research_reintento_horas',
                    'research_reintentos_max', 'vacantes_solo_reporte',
                    'reporte_contratando_activo', 'reporte_contratando_dia',
                    'reporte_contratando_hora', 'reporte_contratando_dias',
                    'reporte_contratando_max')
    )
    with check (
        public.is_panel_user()
        and (
            (key in ('vacantes_solo_reporte', 'reporte_contratando_activo')
                and jsonb_typeof(value) = 'boolean')
            or (key in ('tarjeta_dias_misma_persona', 'research_reintento_horas',
                        'research_reintentos_max', 'reporte_contratando_dia',
                        'reporte_contratando_hora', 'reporte_contratando_dias',
                        'reporte_contratando_max')
                and jsonb_typeof(value) = 'number'
                -- El cast solo se evalúa si el tipo es número (ver 0023). Enteros: un
                -- día "1.5" o un tope "2.5" no significan nada para el worker.
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric = trunc((value #>> '{}')::numeric)
                              and (value #>> '{}')::numeric >= case key
                                  -- El día de la semana y la ventana no admiten cero.
                                  when 'reporte_contratando_dia' then 1
                                  when 'reporte_contratando_dias' then 1
                                  else 0
                              end
                              and (value #>> '{}')::numeric <= case key
                                  when 'tarjeta_dias_misma_persona' then 60
                                  when 'research_reintento_horas' then 720
                                  when 'research_reintentos_max' then 10
                                  when 'reporte_contratando_dia' then 7
                                  when 'reporte_contratando_hora' then 23
                                  when 'reporte_contratando_dias' then 60
                                  else 50
                              end
                         else false end)
        )
    );

-- Las numéricas NO entran en `mcp_ajustar_umbral`, igual que las de 0037 y 0039:
-- añadirlas obliga a reescribir la función entera, con las claves de todas las
-- migraciones anteriores dentro. Se cambian desde Settings.
