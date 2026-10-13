-- El rastro de los mensajes directos del dueño de la credencial, para
-- recordarle las conversaciones que se enfrían.
--
-- Anthony escribió a un lead por DM, el lead contestó, y se le pasó responder.
-- Esta tabla es lo que hace posible avisarle, y a propósito NO guarda nada de
-- lo que se dijeron. Una fila por conversación uno a uno, con lo mínimo para
-- decir "Sunny replied 2 days ago" y dar el enlace para abrirla:
--
--   dm_id          el canal D… de la conversación (la clave)
--   team_id        el workspace, solo para montar el enlace de Slack
--   slack_user_id  la otra persona
--   prospect_id    quién es, si ya la conocemos
--   ultimo_ts      cuándo fue el último mensaje
--   ultimo_de      quién lo escribió: él ('dueno') o la otra persona ('otro')
--   avisado_ts     el ultimo_ts del último aviso, para no repetirlo
--
-- Eso es todo el rastro. No hay columna de texto, ni de asunto, ni de
-- adjuntos, y ninguna consulta del repositorio pide el cuerpo de un DM: lo
-- único que se lee de Slack es la marca de tiempo y el autor del último
-- mensaje (ingest/dms.py). `tests/test_dms.py` lo vigila contra el esquema.
--
-- Solo conversaciones uno a uno: el CHECK de `dm_id` exige el prefijo D, que
-- es el que Slack da a los `im`. Un grupo (`mpim`, G…) o un canal (C…) no
-- cabe en esta tabla aunque el código se equivocara.
--
-- 0001 a 0026 ya están aplicadas en producción: nunca se tocan. Esto solo
-- añade.

create table public.dm_rastro (
    dm_id          text primary key check (dm_id ~ '^D[A-Z0-9]+$'),
    team_id        text check (team_id is null or team_id ~ '^T[A-Z0-9]+$'),
    slack_user_id  text not null,
    prospect_id    uuid references public.prospects (id) on delete set null,
    ultimo_ts      text not null,
    ultimo_de      text not null check (ultimo_de in ('dueno', 'otro')),
    -- El aviso ya dado para este estado de la conversación: mientras
    -- avisado_ts sea igual a ultimo_ts, el caso no vuelve a salir en el
    -- resumen. Un mensaje nuevo cambia ultimo_ts y lo reinicia.
    avisado_ts     text,
    actualizado_at timestamptz not null default now()
);

-- La ficha de la persona en el panel busca por su id; el rastro se enlaza por
-- slack_user_id cuando todavía no hay prospect.
create index dm_rastro_prospect_idx on public.dm_rastro (prospect_id);
create index dm_rastro_persona_idx on public.dm_rastro (slack_user_id);
-- Los casos del resumen diario: quién escribió el último mensaje y cuándo.
create index dm_rastro_pendientes_idx on public.dm_rastro (ultimo_de, ultimo_ts);

alter table public.dm_rastro enable row level security;

-- Lectura para el panel y nada más, igual que `canales` (0025) y
-- `message_signals` (0021). El worker entra como postgres y no pasa por aquí.
revoke all on public.dm_rastro from anon, authenticated;
grant select on public.dm_rastro to authenticated;
create policy panel_lee_dm_rastro on public.dm_rastro for select to authenticated
    using (public.is_panel_user());

insert into public.config (key, value) values
    -- El interruptor de todo esto: apagado, no se lee ningún DM ni se avisa.
    ('seguimiento_activo', 'true'::jsonb),
    -- "They replied, you haven't": horas desde su mensaje sin que él conteste.
    ('seguimiento_horas_para_responder', '24'::jsonb),
    -- "No answer yet": días desde el mensaje de él sin respuesta.
    ('seguimiento_dias_sin_respuesta', '3'::jsonb),
    -- Como mucho estas líneas en el resumen diario, las más urgentes primero.
    ('seguimiento_max_por_resumen', '10'::jsonb),
    -- El rastro de una conversación con el último mensaje más viejo que esto
    -- se borra solo (ingest/dms.py, limpiar_retencion).
    ('seguimiento_retencion_dias', '90'::jsonb)
on conflict (key) do nothing;

-- Editables desde Settings. Política nueva, que se suma a las de 0006, 0012,
-- 0020, 0022 y 0023: las permisivas se combinan con OR, así que la lista
-- blanca solo crece con estas claves. El interruptor es booleano y los
-- umbrales números no negativos; nada más entra.
create policy panel_ajusta_seguimiento on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('seguimiento_activo', 'seguimiento_horas_para_responder',
                    'seguimiento_dias_sin_respuesta', 'seguimiento_max_por_resumen',
                    'seguimiento_retencion_dias')
    )
    with check (
        public.is_panel_user()
        and (
            (key = 'seguimiento_activo' and jsonb_typeof(value) = 'boolean')
            or (key in ('seguimiento_horas_para_responder', 'seguimiento_dias_sin_respuesta',
                        'seguimiento_max_por_resumen', 'seguimiento_retencion_dias')
                and jsonb_typeof(value) = 'number'
                -- El cast solo se evalúa si el tipo es número (ver 0023).
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric >= 0 else false end)
        )
    );

-- ---------------------------------------------------------------------------------
-- Los cuatro umbrales nuevos, también desde Claude.
--
-- La regla del MCP de escritura es "lo mismo que el panel, y nada más"
-- (mcp_escritura.py, migración 0024), y un test la impone comparando su lista
-- con las claves numéricas que la RLS del panel deja tocar. Abrir las de
-- arriba en Settings y no aquí rompería esa regla, así que se añaden al CASE de
-- `mcp_ajustar_umbral` con su máximo. Mismo contrato y mismo cuerpo que en
-- 0024: solo crecen los casos (igual que 0025 reemplazó panel_guardar_canales).
--
-- El máximo de la retención es un año a propósito: es un tope de privacidad,
-- no un umbral cualquiera -- nadie debería poder dejar el rastro para siempre
-- desde una pantalla.
-- ---------------------------------------------------------------------------------
create or replace function public.mcp_ajustar_umbral(p_email text, p_clave text, p_valor numeric)
    returns jsonb
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

-- Como en 0024: nadie del lado de la API la ejecuta. `create or replace` conserva los
-- permisos, pero se repite para que no dependa de eso.
revoke all on function public.mcp_ajustar_umbral(text, text, numeric)
    from public, anon, authenticated;
