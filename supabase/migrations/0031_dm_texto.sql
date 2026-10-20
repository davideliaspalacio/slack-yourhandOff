-- Leer el texto de los DMs de Anthony con sus leads, para sugerirle qué contestar.
--
-- Hasta aquí el rastro de DMs (0027) guardaba solo quién habló último y cuándo, y había
-- tests que impedían guardar una sola palabra. Eso era a propósito: leer las
-- conversaciones privadas de Anthony con terceros necesitaba su permiso explícito.
-- Anthony lo dio (comunicado por David el 2026-10-19). Esta migración abre la puerta, y
-- la abre lo justo:
--
-- - **Detrás de un interruptor apagado** (`dm_texto_activo`). Apagado no se lee ni se
--   guarda nada, y al apagarlo el worker borra todo lo guardado: texto y sugerencias.
-- - **Solo leads.** El worker solo trae el texto de conversaciones con gente que ya pasó
--   por el research (`dm_rastro.prospect_id` no nulo, sin descartar). Las conversaciones
--   personales de Anthony no se abren.
-- - **Solo lo reciente.** `dm_texto_retencion_dias` (10): lo más viejo se borra.
-- - **El texto en bruto no lo lee nadie más que el worker.** `dm_mensajes` no tiene
--   permisos para el panel ni para la API. Lo que se enseña es la sugerencia y una línea
--   de contexto (`dm_sugerencias`), que sí lee el panel.
--
-- Leer esto no le añade al lector del Founders Club ni un método de Slack: el historial
-- de un DM es `conversations.history` y lo no leído sale de `conversations.info`, los
-- dos ya en su lista blanca de solo lectura. `conversations.mark` sigue prohibido, así
-- que leer no le cambia a Anthony lo que tiene como no leído.
--
-- 0001 a 0030 ya están aplicadas: nunca se tocan. Esto solo añade.

insert into public.config (key, value) values
    ('dm_texto_activo', 'false'::jsonb),
    ('dm_texto_retencion_dias', '10'::jsonb),
    -- Cada sugerencia es una llamada al modelo con la conversación entera: su tope.
    ('dm_sugerencias_por_dia', '30'::jsonb)
on conflict (key) do nothing;

create policy panel_ajusta_dm_texto on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('dm_texto_activo', 'dm_texto_retencion_dias', 'dm_sugerencias_por_dia')
    )
    with check (
        public.is_panel_user()
        and (
            (key = 'dm_texto_activo' and jsonb_typeof(value) = 'boolean')
            or (key in ('dm_texto_retencion_dias', 'dm_sugerencias_por_dia')
                and jsonb_typeof(value) = 'number'
                -- El cast solo se evalúa si el tipo es número (ver 0023).
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric >= 0 else false end)
        )
    );

-- Lo no leído de cada conversación, tal como lo da `conversations.info`. Son marcas, no
-- contenido: cuántos mensajes tiene Anthony sin abrir y hasta dónde leyó.
alter table public.dm_rastro
    add column sin_leer    integer,
    add column leido_hasta text;

-- El texto. Una fila por mensaje. Se borra solo con la conversación, con la retención o
-- al apagar el interruptor.
create table public.dm_mensajes (
    dm_id      text not null references public.dm_rastro (dm_id) on delete cascade,
    ts         text not null,
    de         text not null check (de in ('dueno', 'otro')),
    texto      text not null,
    created_at timestamptz not null default now(),
    primary key (dm_id, ts)
);

create index dm_mensajes_por_fecha_idx on public.dm_mensajes (created_at);

alter table public.dm_mensajes enable row level security;
-- Sin política y sin permisos: ni el panel ni la API pueden leerlo. Solo el worker, que
-- conecta como dueño de la base. Supabase da permisos por omisión a las tablas nuevas,
-- así que se quitan de forma explícita.
revoke all on public.dm_mensajes from anon, authenticated;

-- La sugerencia y su línea de contexto, una por conversación. Se rehace solo cuando la
-- conversación cambia (`ultimo_ts`), para no pagar dos veces por lo mismo.
create table public.dm_sugerencias (
    dm_id      text primary key references public.dm_rastro (dm_id) on delete cascade,
    ultimo_ts  text not null,
    contexto   text not null,
    sugerencia text not null,
    created_at timestamptz not null default now()
);

alter table public.dm_sugerencias enable row level security;
grant select on public.dm_sugerencias to authenticated;
create policy panel_lee_sugerencias on public.dm_sugerencias for select to authenticated
    using (public.is_panel_user());

-- Los dos topes numéricos entran en la lista que el MCP puede tocar, igual que el resto.
-- Mismo cuerpo que en 0030 más los dos máximos; un test vigila que no se separen.
create or replace function public.mcp_ajustar_umbral(
    p_email text,
    p_clave text,
    p_valor numeric
) returns jsonb
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_maximo   numeric;
    v_anterior jsonb;
    v_nuevo    jsonb;
begin
    perform public._mcp_exigir_panel(p_email);

    v_maximo := case p_clave
        when 'banda_alta_min' then 4
        when 'banda_media_min' then 4
        when 'banda_baja_min' then 4
        when 'research_por_hora' then 1000
        when 'sms_por_dia' then 1000
        when 'sms_hora_inicio' then 24
        when 'sms_hora_fin' then 24
        when 'radar_auto_min' then 10
        when 'radar_horas_entre_escaneos' then 720
        when 'unipile_busquedas_por_dia' then 1000
        when 'unipile_perfiles_por_dia' then 1000
        when 'alertas_por_dia' then 1000
        when 'alertas_dias_misma_persona' then 365
        when 'alertas_edad_maxima_horas' then 720
        when 'aviso_token_horas' then 720
        when 'seguimiento_horas_para_responder' then 720
        when 'seguimiento_dias_sin_respuesta' then 365
        when 'seguimiento_max_por_resumen' then 100
        when 'seguimiento_retencion_dias' then 365
        when 'seguimiento_recordar_horas' then 168
        when 'seguimiento_avisos_por_dia' then 1000
        when 'menciones_por_dia' then 1000
        when 'presentaciones_por_dia' then 1000
        when 'dm_texto_retencion_dias' then 90
        when 'dm_sugerencias_por_dia' then 1000
        else null
    end;
    if v_maximo is null then
        raise exception 'setting % cannot be changed from here', p_clave;
    end if;

    if p_valor is null or p_valor <> trunc(p_valor) or p_valor < 0 or p_valor > v_maximo then
        raise exception 'invalid value for %: use a whole number from 0 to %', p_clave, v_maximo;
    end if;

    select value into v_anterior from public.config where key = p_clave for update;
    if not found then
        raise exception 'setting % not found', p_clave;
    end if;

    v_nuevo := to_jsonb(p_valor::bigint);
    update public.config set value = v_nuevo, updated_at = now() where key = p_clave;

    return jsonb_build_object('clave', p_clave, 'anterior', v_anterior, 'valor', v_nuevo);
end;
$$;

revoke all on function public.mcp_ajustar_umbral(text, text, numeric)
    from public, anon, authenticated;
