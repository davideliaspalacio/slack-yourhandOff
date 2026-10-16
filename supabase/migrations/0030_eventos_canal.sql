-- Dos avisos que no necesitan modelo: cuando mencionan a AG y cuando alguien se presenta.
--
-- Las señales de 0021 preguntan al modelo si un mensaje expresa una necesidad, y eso
-- cuesta una llamada por mensaje. Estos dos no hacen falta preguntarlos:
--
-- - **Mención**: el texto del mensaje lleva el id de la cuenta lectora. Es una búsqueda
--   de cadena. Nada que interpretar.
-- - **Presentación**: un mensaje nuevo en el canal de presentaciones. El club tiene uno
--   (`0-welcome-introductions`, el segundo más activo con 156 mensajes al mes) y lo que
--   se publica ahí es, por definición, alguien presentándose.
--
-- Así que son gratis e inmediatos, y es la razón de que vivan aparte de `message_signals`
-- en vez de dentro: ahí cada fila es el resultado de una llamada al modelo, con su coste
-- y su confianza. Aquí no hay nada de eso.
--
-- Una fila por (canal, mensaje, tipo): el mismo mensaje puede ser las dos cosas a la vez
-- -- alguien que se presenta y menciona a AG en la misma línea -- y cada aviso lleva su
-- propia cuenta de si se publicó.
--
-- El texto del mensaje NO se copia aquí. Ya está en `slack_messages`, que es de donde
-- sale; esta tabla solo apunta qué pasó y si se avisó.
--
-- 0001 a 0029 ya están aplicadas: nunca se tocan. Esto solo añade.

create table public.eventos_canal (
    id          uuid primary key default gen_random_uuid(),
    channel_id  text not null,
    ts          text not null,
    tipo        text not null check (tipo in ('mencion', 'presentacion')),
    -- Quién escribió el mensaje. Puede no ser un prospect todavía (alguien que se
    -- presenta es, justamente, gente nueva), así que `prospect_id` es opcional.
    slack_user_id text,
    prospect_id uuid references public.prospects(id) on delete set null,
    estado      text not null default 'pendiente'
                check (estado in ('pendiente', 'publicada', 'omitida')),
    motivo      text,
    aviso_ts    text,
    created_at  timestamptz not null default now(),
    unique (channel_id, ts, tipo)
);

create index eventos_canal_pendientes_idx on public.eventos_canal (created_at)
    where estado = 'pendiente';

alter table public.eventos_canal enable row level security;

-- La lee el panel y nadie más, como message_signals (0021) y canales (0025).
grant select on public.eventos_canal to authenticated;
create policy panel_lee_eventos on public.eventos_canal for select to authenticated
    using (public.is_panel_user());

insert into public.config (key, value) values
    -- El canal de presentaciones. Vacío = no se avisa de presentaciones. Se pone desde
    -- Settings; no se adivina por el nombre, porque un club puede llamarlo de cualquier
    -- forma y adivinarlo mal significaría avisar de todo lo que se escribe en un canal.
    ('canal_presentaciones', '""'::jsonb),
    -- Topes diarios, por separado: una mención es casi siempre relevante y una
    -- presentación es rutina, así que no deben competir por el mismo cupo.
    ('menciones_por_dia', '50'::jsonb),
    ('presentaciones_por_dia', '20'::jsonb)
on conflict (key) do nothing;

create policy panel_ajusta_eventos on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('canal_presentaciones', 'menciones_por_dia', 'presentaciones_por_dia')
    )
    with check (
        public.is_panel_user()
        and (
            (key = 'canal_presentaciones' and jsonb_typeof(value) = 'string')
            or (key in ('menciones_por_dia', 'presentaciones_por_dia')
                and jsonb_typeof(value) = 'number'
                -- El cast solo se evalúa si el tipo es número (ver 0023).
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric >= 0 else false end)
        )
    );

-- Los dos topes nuevos entran también en la lista que el MCP puede tocar: son topes
-- como `alertas_por_dia`, y el sentido de las herramientas de escritura es que AG
-- pueda bajarlos desde su Claude sin entrar al panel. Mismo cuerpo que en 0029 más
-- los dos máximos; un test comprueba que las dos listas no se separen.
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
