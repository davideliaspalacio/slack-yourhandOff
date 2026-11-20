-- Recordatorios de lo que hoy se escapa: hilos de canal, menciones y gente sin ficha.
--
-- AG lo pidió (punto 6): "monitorizar la actividad de las personas para darle
-- recordatorios de que responda". Hasta aquí el seguimiento (0027, 0029) solo cubría los
-- mensajes directos con leads que ya tienen ficha. Quedaban fuera tres cosas, y esta
-- migración es el contrato de las tres (`delivery/seguimiento_canal.py` y
-- `delivery/seguimiento.py`):
--
-- - **Hilos de canal**: alguien respondió en un hilo que abrió él -- o quien abrió el
--   hilo le contestó a él -- y no ha vuelto a escribir ahí.
-- - **Menciones**: lo mencionaron (0030 ya avisa una vez) y nadie comprobaba después si
--   contestó.
-- - **Mensajes directos de gente sin ficha**: el rastro (0027) ya los guarda -- sin
--   texto, como todos --, pero el recordatorio los saltaba porque no había nombre que
--   poner. Nace APAGADO: encenderlo enseña en el canal del equipo con quién habla AG
--   aunque no sea un lead, y eso lo decide él.
--
-- Aquí no se guarda nada de lo que nadie escribió. La tabla nueva apunta de qué hilo o
-- de qué mención se avisó, cuántas veces y hasta dónde se dio por contestado; el texto
-- sigue en `slack_messages` y de un mensaje directo sigue sin guardarse nada.
--
-- **Lo viejo no se anuncia.** Los interruptores de abajo hacen además de marca: solo
-- cuenta lo que llegó DESPUÉS de la última vez que se tocó el suyo (`config.updated_at`,
-- que al aplicar esta migración es ahora). Sin eso, el primer despliegue -- o volver a
-- encenderlo tras una semana apagado -- vaciaría en el canal todo lo que llevaba días
-- sin contestar, que es la inundación que ya pasó dos veces.
--
-- 0001 a 0037 ya están aplicadas: nunca se tocan. Esto solo añade.

-- De qué se avisó en los canales. Una fila por hilo, o por mención suelta si no está en
-- ningún hilo: `clave_ts` es el `ts` del principal del hilo, o el de la propia mención.
create table public.seguimiento_canal (
    channel_id     text not null,
    clave_ts       text not null,
    tipo           text not null check (tipo in ('hilo', 'mencion')),
    -- Lo que espera respuesta: el `ts` de la primera respuesta sin contestar, o el de la
    -- mención. Si cambia (él contestó y volvieron a escribirle), la cuenta empieza de cero.
    pendiente_ts   text not null,
    -- Cuándo salió el último aviso inmediato y cuántos van por este `pendiente_ts`.
    avisado_at     timestamptz,
    avisos         integer not null default 0,
    -- Hasta dónde se comprobó contra Slack que ya contestó (escribió después, o
    -- reaccionó). Hace falta porque una reacción nunca llega a `slack_messages`: sin
    -- esto, lo que contestó con un 👍 volvería a salir en cada pasada.
    resuelto_ts    text,
    actualizado_at timestamptz not null default now(),
    primary key (channel_id, clave_ts)
);
alter table public.seguimiento_canal enable row level security;
-- Contabilidad interna del worker, como `hilos` (0032): ni el panel ni la API la leen.
revoke all on public.seguimiento_canal from anon, authenticated;

-- La consulta de los hilos busca las respuestas de cada hilo donde él escribió: sin esto
-- recorrería el canal entero por cada uno.
create index slack_messages_hilo_idx on public.slack_messages (channel_id, thread_ts)
    where thread_ts is not null;

insert into public.config (key, value) values
    -- Los tres interruptores. Los de canal nacen encendidos; el de hilos no hace nada
    -- mientras `leer_hilos` (0032) siga apagado, porque sin leer hilos no hay respuestas.
    ('seguimiento_hilos_activo', 'true'::jsonb),
    ('seguimiento_menciones_activo', 'true'::jsonb),
    ('seguimiento_sin_ficha_activo', 'false'::jsonb),
    -- Horas que espera una respuesta en un hilo antes de recordarla: las mismas 24 que
    -- un mensaje directo.
    ('seguimiento_hilo_horas', '24'::jsonb),
    -- Una mención es alguien pidiéndole algo por su nombre: espera menos.
    ('seguimiento_mencion_horas', '4'::jsonb),
    -- Cuántas veces interrumpe un mismo hilo o una misma mención. Después solo queda en
    -- el resumen del día: un hilo que decidió no contestar no puede sonar cada 4 horas.
    ('seguimiento_canal_max_avisos', '3'::jsonb),
    -- Pasados estos días, un hilo, una mención o un mensaje directo de alguien sin ficha
    -- deja de recordarse del todo.
    ('seguimiento_caduca_dias', '7'::jsonb)
on conflict (key) do nothing;

-- Política propia, sin tocar `panel_ajusta_seguimiento` (0029): mismo contrato de
-- siempre (booleano o entero no negativo) y el mismo cast protegido de 0023.
create policy panel_ajusta_seguimiento_canal on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('seguimiento_hilos_activo', 'seguimiento_menciones_activo',
                    'seguimiento_sin_ficha_activo', 'seguimiento_hilo_horas',
                    'seguimiento_mencion_horas', 'seguimiento_canal_max_avisos',
                    'seguimiento_caduca_dias')
    )
    with check (
        public.is_panel_user()
        and (
            (key in ('seguimiento_hilos_activo', 'seguimiento_menciones_activo',
                     'seguimiento_sin_ficha_activo')
                and jsonb_typeof(value) = 'boolean')
            or (key in ('seguimiento_hilo_horas', 'seguimiento_mencion_horas',
                        'seguimiento_canal_max_avisos', 'seguimiento_caduca_dias')
                and jsonb_typeof(value) = 'number'
                -- El cast solo se evalúa si el tipo es número (ver 0023).
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric >= 0 else false end)
        )
    );

-- Los cuatro numéricos, en la lista que el MCP puede tocar (un test vigila la paridad).
-- Mismo cuerpo que en 0032 más los cuatro máximos. OJO al fusionar: si 0037 también
-- redefine esta función, sus claves hay que traerlas aquí, porque la última gana.
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
        when 'seguimiento_hilo_horas' then 720
        when 'seguimiento_mencion_horas' then 720
        when 'seguimiento_canal_max_avisos' then 100
        when 'seguimiento_caduca_dias' then 90
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
