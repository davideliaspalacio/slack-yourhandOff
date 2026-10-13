-- Escrituras del MCP remoto (Fase 2, parte 1): estados, notas y umbrales desde el Claude
-- del equipo, con el mismo control que tiene el panel y nada más.
--
-- Por qué hacen falta funciones nuevas: las del panel (panel_*) y las políticas de RLS
-- exigen is_panel_user(), que mira el JWT de Supabase (request.jwt.claims). El MCP no
-- tiene ese JWT: la puerta (web/mcp_auth.py) identifica a la persona por el token y
-- sabe su correo. Estas funciones reciben ese correo como primer argumento y comprueban
-- ELLAS que está en panel_users: es el equivalente de is_panel_user() para un llamante
-- identificado por correo.
--
-- Quién puede llamarlas. Esto es lo delicado: reciben el correo como argumento, así que
-- quien pudiera ejecutarlas escogería el correo que quisiera. Por eso `revoke all` de
-- public, anon Y authenticated (el panel, PostgREST): Supabase da execute a anon y
-- authenticated por defecto en funciones nuevas del esquema public, y cualquiera puede
-- registrarse como authenticated. Las ejecuta el worker/web, que conecta con DATABASE_URL
-- como dueño de la base (postgres): el dueño ejecuta sin necesitar ningún grant. A
-- service_role no se le quita nada (ya se salta RLS y es la llave maestra; quitárselo
-- no protege nada y rompería un despliegue que conectara con ese rol).
--
-- Qué NO hay aquí, a propósito: borrar, tocar kill_switch / monthly_budget_usd /
-- run_budget_usd, publicar en Slack. Y los dos topes del propio MCP
-- (mcp_llamadas_por_dia, mcp_escrituras_por_dia) tampoco se ajustan por esta vía: un
-- token que pudiera subirse su propio tope no tendría tope.
--
-- 0001 a 0023 ya están en producción: nunca se tocan. Esto solo añade (más un
-- `create or replace` de panel_ayudar_research, mismo contrato, para compartir lógica).

-- ---------------------------------------------------------------------------------
-- Comprobación de acceso, compartida por todas.
-- ---------------------------------------------------------------------------------
create function public._mcp_exigir_panel(p_email text) returns void
    language plpgsql
    stable
    security definer
    set search_path = ''
as $$
begin
    -- panel_users.email está siempre en minúsculas (check de 0006).
    if p_email is null
       or not exists (select 1 from public.panel_users where email = lower(p_email)) then
        raise exception 'not authorized';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------------
-- Ayudar al research: la lógica de panel_ayudar_research (0008) vive ahora aquí, una
-- sola vez. panel_ayudar_research (abajo) y mcp_ayudar_research la llaman, así que las
-- validaciones no pueden divergir. El cuerpo es el de 0008 sin cambios, salvo que
-- devuelve lo que quedó guardado (jsonb) en vez de void, para que el MCP confirme.
-- ---------------------------------------------------------------------------------
create function public._ayudar_research(
    p_prospect uuid,
    p_empresa  text,
    p_dominio  text,
    p_links    text[],
    p_notas    text
) returns jsonb
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_empresa       text;
    v_dominio       text;
    v_links         text[];
    v_link          text;
    v_notas         text;
    v_slack_user_id text;
    v_state         public.prospect_state;
    v_filas         integer := 0;
begin
    v_empresa := nullif(trim(p_empresa), '');
    if v_empresa is not null and char_length(v_empresa) > 200 then
        raise exception 'invalid company name: must be 200 characters or fewer';
    end if;

    -- Dominio: minúsculas, sin esquema, sin "www." inicial, sin ruta, query ni puerto
    -- (la misma normalización que panel_corregir_web, 0007).
    v_dominio := nullif(trim(p_dominio), '');
    if v_dominio is not null then
        v_dominio := lower(v_dominio);
        v_dominio := regexp_replace(v_dominio, '^https?://', '');
        v_dominio := regexp_replace(v_dominio, '^www\.', '');
        v_dominio := split_part(v_dominio, '/', 1);
        v_dominio := split_part(v_dominio, '?', 1);
        v_dominio := split_part(v_dominio, ':', 1);
        if v_dominio !~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' then
            raise exception 'invalid domain: %', p_dominio;
        end if;
    end if;

    -- Enlaces: recorta cada uno, descarta los vacíos y quita duplicados exactos,
    -- conservando el orden en que el equipo los escribió.
    v_links := array[]::text[];
    if p_links is not null then
        foreach v_link in array p_links loop
            v_link := trim(v_link);
            if v_link <> '' and not (v_link = any(v_links)) then
                v_links := array_append(v_links, v_link);
            end if;
        end loop;
    end if;
    if array_length(v_links, 1) is null then
        v_links := null;
    end if;

    if v_links is not null then
        if array_length(v_links, 1) > 10 then
            raise exception 'invalid link: too many links (max 10)';
        end if;
        foreach v_link in array v_links loop
            if v_link !~ '^https?://' or char_length(v_link) > 500 then
                raise exception 'invalid link: %', v_link;
            end if;
        end loop;
    end if;

    v_notas := nullif(trim(p_notas), '');
    if v_notas is not null and char_length(v_notas) > 2000 then
        raise exception 'invalid notes: must be 2000 characters or fewer';
    end if;

    select slack_user_id, state into v_slack_user_id, v_state
        from public.prospects where id = p_prospect;

    if not found then
        raise exception 'person % not found', p_prospect;
    end if;

    update public.prospects
        set company_name_override = v_empresa,
            company_domain_override = v_dominio,
            research_links = v_links,
            research_notes = v_notas,
            updated_at = now()
        where id = p_prospect;

    -- Una persona descartada no se reencola. La cola ya impide dos tareas abiertas
    -- para la misma persona (índice parcial de 0004), así que no duplica una en curso.
    if v_state <> 'descartado' then
        insert into public.research_jobs (slack_user_id, reason)
            values (v_slack_user_id, 'manual')
        on conflict (slack_user_id) where status in ('pendiente', 'en_curso') do nothing;
        get diagnostics v_filas = row_count;
    end if;

    return jsonb_build_object(
        'empresa', v_empresa,
        'dominio', v_dominio,
        'enlaces', coalesce(to_jsonb(v_links), '[]'::jsonb),
        'notas', v_notas,
        'estado', v_state,
        'encolado', v_filas > 0
    );
end;
$$;

-- panel_ayudar_research: mismo contrato que en 0008 (firma, void, permisos); solo cambia
-- que el cuerpo es una llamada a la función común. `create or replace` conserva los
-- grants de 0008 (authenticated sí; public y anon no).
create or replace function public.panel_ayudar_research(
    p_prospect uuid,
    p_empresa  text,
    p_dominio  text,
    p_links    text[],
    p_notas    text
) returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;
    perform public._ayudar_research(p_prospect, p_empresa, p_dominio, p_links, p_notas);
end;
$$;

create function public.mcp_ayudar_research(
    p_email    text,
    p_prospect uuid,
    p_empresa  text,
    p_dominio  text,
    p_links    text[],
    p_notas    text
) returns jsonb
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    perform public._mcp_exigir_panel(p_email);
    return public._ayudar_research(p_prospect, p_empresa, p_dominio, p_links, p_notas);
end;
$$;

-- ---------------------------------------------------------------------------------
-- Cambiar el estado. El panel lo hace con un update directo bajo la política
-- panel_cambia_estado (0006); una política no se puede llamar, así que la lista de
-- estados permitidos se repite aquí. 'incompleto' lo decide el research, no una persona.
-- A diferencia del panel (que no hace nada si el id no existe), esta avisa.
-- ---------------------------------------------------------------------------------
create function public.mcp_cambiar_estado(p_email text, p_prospect uuid, p_estado text)
    returns jsonb
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_anterior public.prospect_state;
    v_nuevo    public.prospect_state;
begin
    perform public._mcp_exigir_panel(p_email);

    if p_estado is null
       or p_estado not in ('nuevo', 'investigado', 'contactado', 'cliente', 'descartado') then
        raise exception 'invalid state: %', p_estado;
    end if;

    select state into v_anterior from public.prospects where id = p_prospect for update;
    if not found then
        raise exception 'person % not found', p_prospect;
    end if;

    update public.prospects
        set state = p_estado::public.prospect_state, updated_at = now()
        where id = p_prospect
    returning state into v_nuevo;

    return jsonb_build_object('estado_anterior', v_anterior, 'estado', v_nuevo);
end;
$$;

-- ---------------------------------------------------------------------------------
-- Volver a investigar: el botón del panel (insert en research_jobs bajo la política
-- panel_reinvestiga, 0006), con la regla de panel_corregir_web: una persona descartada
-- no se reencola. La cola impide dos tareas abiertas para la misma persona.
-- ---------------------------------------------------------------------------------
create function public.mcp_reinvestigar(p_email text, p_prospect uuid) returns jsonb
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_slack_user_id text;
    v_state         public.prospect_state;
    v_filas         integer := 0;
    v_resultado     text;
begin
    perform public._mcp_exigir_panel(p_email);

    select slack_user_id, state into v_slack_user_id, v_state
        from public.prospects where id = p_prospect;
    if not found then
        raise exception 'person % not found', p_prospect;
    end if;

    if v_state = 'descartado' then
        v_resultado := 'descartado';
    else
        insert into public.research_jobs (slack_user_id, reason)
            values (v_slack_user_id, 'manual')
        on conflict (slack_user_id) where status in ('pendiente', 'en_curso') do nothing;
        get diagnostics v_filas = row_count;
        v_resultado := case when v_filas > 0 then 'encolado' else 'ya_en_cola' end;
    end if;

    return jsonb_build_object('resultado', v_resultado, 'estado', v_state);
end;
$$;

-- ---------------------------------------------------------------------------------
-- Ajustar un umbral numérico. Solo las claves que el panel ya deja tocar (políticas
-- panel_ajusta_umbrales 0006, panel_ajusta_radar 0012, panel_ajusta_senales 0022), menos
-- las booleanas (exigir_evidencia, senales_activas, unipile_pausado) y menos los dos
-- topes del propio MCP. Las bandas llegan hasta 4 porque la puntuación máxima es 3 y,
-- según delivery/bands.py, subir banda_baja_min por encima de ella silencia. El panel solo
-- exige "número" (y su formulario, entero >= 0); aquí además hay un máximo por clave, más
-- estricto que el panel a propósito: Claude equivoca un cero con más facilidad que una
-- persona delante de un formulario.
-- ---------------------------------------------------------------------------------
create function public.mcp_ajustar_umbral(p_email text, p_clave text, p_valor numeric)
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

-- Nadie del lado de la API puede ejecutarlas (ver la cabecera). `revoke ... from public`
-- quita el execute que Postgres da a todos por defecto; anon y authenticated lo traen de
-- los privilegios por defecto de Supabase, y hay que quitárselo por nombre.
revoke all on function public._mcp_exigir_panel(text) from public, anon, authenticated;
revoke all on function public._ayudar_research(uuid, text, text, text[], text)
    from public, anon, authenticated;
revoke all on function public.mcp_ayudar_research(text, uuid, text, text, text[], text)
    from public, anon, authenticated;
revoke all on function public.mcp_cambiar_estado(text, uuid, text)
    from public, anon, authenticated;
revoke all on function public.mcp_reinvestigar(text, uuid) from public, anon, authenticated;
revoke all on function public.mcp_ajustar_umbral(text, text, numeric)
    from public, anon, authenticated;

-- ---------------------------------------------------------------------------------
-- Tope diario de escrituras por persona. Aparte de mcp_llamadas_por_dia (que cuenta toda
-- llamada a una herramienta, lea o escriba). Se cuenta sobre agent_actions: cada
-- escritura deja allí una fila 'mcp_escritura', así que el tope y la auditoría son la
-- misma fuente de verdad. Editable desde Settings: política nueva que se suma a las de
-- 0006, 0012, 0020, 0022 y 0023 (las permisivas se combinan con OR). Solo un número, no
-- negativo: 0 apaga las escrituras del MCP para todos.
-- ---------------------------------------------------------------------------------
insert into public.config (key, value) values
    ('mcp_escrituras_por_dia', '100'::jsonb)
on conflict (key) do nothing;

create policy panel_ajusta_mcp_escrituras on public.config for update to authenticated
    using (public.is_panel_user() and key = 'mcp_escrituras_por_dia')
    with check (
        public.is_panel_user()
        and key = 'mcp_escrituras_por_dia'
        and jsonb_typeof(value) = 'number'
        -- El cast solo se evalúa si el tipo es número: un CASE sí garantiza el orden.
        and case when jsonb_typeof(value) = 'number' then (value #>> '{}')::numeric >= 0
                 else false end
    );

-- Para contar las escrituras de hoy de una persona sin recorrer todo agent_actions.
create index agent_actions_mcp_escritura_idx
    on public.agent_actions ((payload ->> 'email'), created_at)
    where action = 'mcp_escritura';
