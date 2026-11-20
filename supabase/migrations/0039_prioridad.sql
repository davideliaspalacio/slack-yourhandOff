-- Qué atender primero: la prioridad de los avisos y el resumen del día.
--
-- AG lo pidió (punto 7): "priorizar notificaciones para organizarme mejor". Todo cae en
-- un mismo canal -- tarjetas de research, "they asked", menciones, presentaciones,
-- miembros nuevos, recordatorios -- y nada decía qué corría más. Dos cosas:
--
-- 1. **Un nivel de prioridad en cada aviso** ("act now" / "today" / "FYI"). No hay
--    nada que guardar: el nivel sale de reglas sobre datos que ya existen (la banda del
--    dossier, la confianza de la señal, quién espera), y las reglas viven en un solo
--    sitio (`delivery/prioridad.py`). Aquí solo está su interruptor.
-- 2. **El resumen del día** (`delivery/resumen_prioridad.py`): un mensaje a una hora
--    fija con lo que sigue pendiente, agrupado por nivel. Absorbe el resumen diario de
--    seguimiento ("FOLLOW-UPS", 0027): mientras esté encendido, aquel no se publica y
--    sus casos salen aquí dentro. Así AG sigue recibiendo UN resumen al día, no dos.
--
-- Esto es orden y presentación. Ningún tope cambia, y ningún aviso sale más ni menos
-- que antes.
--
-- 0001 a 0038 ya están aplicadas: nunca se tocan. Esto solo añade.

-- Un resumen por día, y la reserva que impide publicarlo dos veces. `dia` es la fecha
-- en `zona_horaria`, la misma con la que el SMS y las alertas cuentan su día. La fila
-- se escribe ANTES de publicar: dos workers, o un reintento, chocan con la clave
-- primaria y solo uno publica.
create table public.resumenes_prioridad (
    dia            date primary key,
    -- `reservado`: se está publicando (o se cayó a medias: ese día ya no sale, que es
    -- mejor que salir dos veces). `publicado`: salió. `vacio`: a la hora no había nada
    -- pendiente, y el día queda cerrado sin mensaje. `fallido`: Slack falló; se
    -- reintenta en la pasada siguiente hasta agotar los intentos.
    estado         text not null
                   check (estado in ('reservado', 'publicado', 'vacio', 'fallido')),
    intentos       integer not null default 1,
    -- Cuántas líneas salieron de cada nivel. Solo números: de quién era cada línea
    -- queda en el mensaje de Slack, no aquí.
    ahora          integer not null default 0,
    hoy            integer not null default 0,
    fyi            integer not null default 0,
    channel_id     text,
    message_ts     text,
    created_at     timestamptz not null default now(),
    actualizado_at timestamptz not null default now()
);
alter table public.resumenes_prioridad enable row level security;
-- Contabilidad interna del worker, como `seguimiento_canal` (0038): ni el panel ni la
-- API la leen.
revoke all on public.resumenes_prioridad from anon, authenticated;

insert into public.config (key, value) values
    -- La marca de prioridad en la cabecera y en la notificación de cada aviso. Apagada,
    -- los avisos salen exactamente como antes de esta migración.
    ('prioridad_activa', 'true'::jsonb),
    -- El resumen del día. Apagado, vuelve el resumen diario de seguimiento de siempre.
    ('resumen_prioridad_activo', 'true'::jsonb),
    -- A partir de qué hora sale, de 0 a 23, en `zona_horaria`. Las 8 de la mañana:
    -- antes de que empiece el día, no a medianoche.
    ('resumen_prioridad_hora', '8'::jsonb),
    -- Cuántas líneas como mucho por nivel. Lo que no cabe se cuenta ("+3 more") y no
    -- se pinta: un resumen que hay que desplazar no se lee.
    ('resumen_prioridad_max_por_nivel', '5'::jsonb),
    -- Cuántos días sigue saliendo una tarjeta o un "they asked" que nadie ha marcado
    -- como contactado ni descartado. Pasados, deja de recordarse: sigue en el panel.
    ('resumen_prioridad_dias', '7'::jsonb)
on conflict (key) do nothing;

-- Política propia, como cada grupo de claves desde 0012: booleanos los dos
-- interruptores, enteros acotados los tres numéricos, y el mismo cast protegido de 0023.
create policy panel_ajusta_prioridad on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('prioridad_activa', 'resumen_prioridad_activo', 'resumen_prioridad_hora',
                    'resumen_prioridad_max_por_nivel', 'resumen_prioridad_dias')
    )
    with check (
        public.is_panel_user()
        and (
            (key in ('prioridad_activa', 'resumen_prioridad_activo')
                and jsonb_typeof(value) = 'boolean')
            or (key in ('resumen_prioridad_hora', 'resumen_prioridad_max_por_nivel',
                        'resumen_prioridad_dias')
                and jsonb_typeof(value) = 'number'
                -- El cast solo se evalúa si el tipo es número (ver 0023). Enteros: una
                -- hora "8.5" o un tope "2.5" no significan nada para el worker.
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric >= 0
                              and (value #>> '{}')::numeric = trunc((value #>> '{}')::numeric)
                              and (value #>> '{}')::numeric <= case key
                                  when 'resumen_prioridad_hora' then 23
                                  when 'resumen_prioridad_max_por_nivel' then 20
                                  else 60
                              end
                         else false end)
        )
    );

-- Las tres numéricas NO entran en `mcp_ajustar_umbral`, igual que las de 0037: añadirlas
-- obliga a reescribir la función entera, con las claves de todas las migraciones
-- anteriores dentro. Se cambian desde Settings (un test deja escrita la decisión).
