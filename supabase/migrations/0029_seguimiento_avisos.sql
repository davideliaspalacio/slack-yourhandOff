-- El seguimiento avisa en cuanto una conversación cruza la línea, y vuelve a recordarlo.
--
-- Hasta aquí el seguimiento (0027) esperaba al resumen del día y cada conversación salía
-- una sola vez. Eso era tranquilo pero llegaba tarde: un lead que contesta a las nueve de
-- la mañana no aparecía hasta el resumen, y si se le pasaba no volvía a aparecer nunca.
--
-- Desde aquí hay dos avisos distintos, y es a propósito que sean distintos:
--
-- - El **aviso inmediato**: en la misma pasada del rastro de DMs (cada ~15 min) que ve
--   que una conversación ya pasó de su plazo, se publica. Y mientras siga esperando, se
--   repite cada `seguimiento_recordar_horas`.
-- - El **resumen diario** de 0027 se queda como estaba, de red de seguridad: si los
--   avisos se quedaron sin cupo o alguien los apagó, el resumen sigue contando lo que
--   nadie ha mirado.
--
-- El riesgo de esto es el ruido, no el gasto: con muchas conversaciones colgando y un
-- recordatorio cada pocas horas, el aviso se vuelve papel pintado y se deja de leer. Por
-- eso el cupo diario (`seguimiento_avisos_por_dia`) y el intervalo son dos claves de
-- config y no dos números en el código: se bajan desde Settings, en caliente, sin
-- desplegar, el día que molesten.
--
-- Nada de esto escribe en el Founders Club ni guarda el texto de un DM: el aviso lleva
-- el nombre, los días que lleva esperando y el enlace, igual que el resumen.
--
-- 0001 a 0028 ya están aplicadas: nunca se tocan. Esto solo añade.

-- Cuándo se avisó por última vez de esta conversación. `avisado_ts` (0027) sigue siendo
-- "por qué mensaje se avisó" y es lo que evita repetir un aviso que ya no toca; esta
-- columna es "cuándo", y es la que permite volver a recordarlo pasadas unas horas.
alter table public.dm_rastro add column avisado_at timestamptz;

-- El índice de la consulta del aviso: las conversaciones a las que ya les toca recordar.
create index dm_rastro_por_recordar_idx on public.dm_rastro (avisado_at nulls first);

insert into public.config (key, value) values
    -- Cada cuántas horas se vuelve a recordar una conversación que sigue esperando.
    ('seguimiento_recordar_horas', '4'::jsonb),
    -- Tope de avisos inmediatos al día, para que no puedan inundar el canal.
    ('seguimiento_avisos_por_dia', '20'::jsonb)
on conflict (key) do nothing;

-- Las dos claves nuevas entran en la política de Settings de 0027. `create or replace`
-- no existe para políticas: se borra y se vuelve a crear con la lista ampliada, con el
-- mismo contrato (número entero, no negativo) y el mismo cast protegido de 0023.
drop policy panel_ajusta_seguimiento on public.config;
create policy panel_ajusta_seguimiento on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('seguimiento_activo', 'seguimiento_horas_para_responder',
                    'seguimiento_dias_sin_respuesta', 'seguimiento_max_por_resumen',
                    'seguimiento_retencion_dias', 'seguimiento_recordar_horas',
                    'seguimiento_avisos_por_dia')
    )
    with check (
        public.is_panel_user()
        and (
            (key = 'seguimiento_activo' and jsonb_typeof(value) = 'boolean')
            or (key in ('seguimiento_horas_para_responder', 'seguimiento_dias_sin_respuesta',
                        'seguimiento_max_por_resumen', 'seguimiento_retencion_dias',
                        'seguimiento_recordar_horas', 'seguimiento_avisos_por_dia')
                and jsonb_typeof(value) = 'number'
                -- El cast solo se evalúa si el tipo es número (ver 0023).
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric >= 0 else false end)
        )
    );

-- Y en la lista de umbrales que el MCP puede tocar. Mismo cuerpo que en 0027 más los dos
-- máximos nuevos: 168 horas (una semana) para el recordatorio, 1000 avisos al día.
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
