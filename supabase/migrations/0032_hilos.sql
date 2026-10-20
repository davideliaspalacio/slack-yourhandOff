-- Leer las respuestas de los hilos, con el hilo entero como contexto.
--
-- `conversations.history` solo devuelve los mensajes principales de un canal; las
-- respuestas de un hilo no llegan nunca. Y ahí está buena parte de la compra: "+1, we
-- need this too", "we're looking for the same". Esta migración es el contrato de las dos
-- partes que se construyen en paralelo (traer los hilos, y entenderlos para avisar).
--
-- Las reglas que fija, y por qué:
--
-- - **Una respuesta se reconoce sola**: en `slack_messages`, una fila con `thread_ts` no
--   nulo y distinto de su `ts` es una respuesta, y `thread_ts` es el `ts` del mensaje
--   principal. El principal también se guarda (Slack lo devuelve primero en
--   `conversations.replies`), aunque sea viejo: sin él no hay contexto.
-- - **Lo viejo es solo contexto**: las respuestas (y el principal) anteriores a la ventana
--   de lectura se guardan con `status = 'archivado'`. El resolver solo investiga `nuevo`
--   y el detector solo mira lo reciente, así que nunca disparan nada. Es la regla de "sin
--   histórico" que se aprendió por las malas.
-- - **El marcador de cada hilo** vive en `hilos`: la última respuesta ya traída. Así solo
--   se pide un hilo cuando su `latest_reply` es más nueva.
-- - **Las respuestas a mensajes viejos** se descubren con un barrido por canal de los
--   últimos `hilos_dias` días, como mucho cada media hora (`canales.hilos_barrido_at`).
-- - **`message_signals.thread_ts`**: el hilo del que sale una señal. Con eso los avisos
--   de un mismo hilo se agrupan en uno, en vez de un aviso por cada "+1".
--
-- Leer un hilo es `conversations.replies`, que solo lista las respuestas: no publica, no
-- reacciona y no marca nada como leído. Se añade a la lista blanca del lector en el mismo
-- commit que esta migración, con su test. La lista de lo prohibido no cambia.
--
-- Nace apagado (`leer_hilos`). 0001 a 0031 ya están aplicadas: nunca se tocan.

insert into public.config (key, value) values
    ('leer_hilos', 'false'::jsonb),
    -- Hasta dónde se mira hacia atrás para descubrir respuestas nuevas en hilos viejos.
    ('hilos_dias', '7'::jsonb),
    -- Tope de hilos que se piden en una vuelta, para no quedarse hablando con Slack.
    ('hilos_por_vuelta', '20'::jsonb)
on conflict (key) do nothing;

create policy panel_ajusta_hilos on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('leer_hilos', 'hilos_dias', 'hilos_por_vuelta')
    )
    with check (
        public.is_panel_user()
        and (
            (key = 'leer_hilos' and jsonb_typeof(value) = 'boolean')
            or (key in ('hilos_dias', 'hilos_por_vuelta')
                and jsonb_typeof(value) = 'number'
                -- El cast solo se evalúa si el tipo es número (ver 0023).
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric >= 0 else false end)
        )
    );

-- El marcador de cada hilo.
create table public.hilos (
    channel_id       text not null,
    thread_ts        text not null,
    -- `ts` de la última respuesta ya traída; null = aún no se ha traído ninguna.
    ultima_respuesta text,
    actualizado_at   timestamptz not null default now(),
    primary key (channel_id, thread_ts)
);
alter table public.hilos enable row level security;
-- Es contabilidad interna del worker: ni el panel ni la API la leen.
revoke all on public.hilos from anon, authenticated;

-- Cuándo se barrió cada canal buscando respuestas nuevas en hilos viejos.
alter table public.canales add column hilos_barrido_at timestamptz;

-- El hilo del que sale una señal (el `ts` del mensaje principal). Null en las señales de
-- mensajes principales, que son todas las anteriores a esta migración.
alter table public.message_signals add column thread_ts text;
create index message_signals_hilo_idx on public.message_signals (channel_id, thread_ts)
    where thread_ts is not null;

-- Los dos topes numéricos, en la lista que el MCP puede tocar (un test vigila la paridad).
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
        when 'hilos_dias' then 30
        when 'hilos_por_vuelta' then 200
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
