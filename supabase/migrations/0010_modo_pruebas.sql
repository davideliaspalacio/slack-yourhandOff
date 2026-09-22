-- Modo de pruebas del panel (Plan 5): crear una persona simulada y seguirla
-- sin pasar por la terminal ni por `scripts/simular.py`.
--
-- El panel nunca ejecuta Python: solo escribe las filas que el worker de
-- Railway (que ya sabe drenar `research_jobs`) recoge solo. Estas dos
-- funciones son la única vía de escritura -- igual que panel_corregir_web
-- (0007) y panel_ayudar_research (0008) -- para que RLS siga siendo la
-- única puerta.
--
-- 0001 a 0009 ya están en producción: nunca se tocan. Esto solo añade.
--
-- 0006 ya dio "grant select on ... to authenticated" a nivel de tabla, con
-- políticas is_panel_user(), sobre prospects, dossiers, slack_messages,
-- research_jobs, deliveries, agent_actions, llm_calls, cost_events y config:
-- la página de pruebas no necesita ningún grant ni política nueva para leer.

-- El id de Slack falso: mismo nombre y empresa siempre caen en el mismo
-- slack_user_id, así repetir el formulario reproduce el camino de "ya hay
-- dossier vigente" en vez de crear a alguien nuevo cada vez -- igual que
-- `fake_user_id` de scripts/simular.py. Un sha1 de verdad no está disponible
-- sin la extensión pgcrypto (no garantizada en todos los entornos), así que
-- la regla común usa md5 (siempre disponible en Postgres) en vez de
-- reproducir sha1 en SQL; scripts/simular.py se cambió para usar esta misma
-- regla (ver ese archivo) y así los dos lados caen siempre en el mismo id
-- para la misma persona. "|" como separador evita que ("AB", "C") y
-- ("A", "BC") caigan en el mismo hash.
create or replace function public.panel_simular_persona(
    p_nombre  text,
    p_empresa text,
    p_web     text,
    p_mensaje text,
    p_links   text[],
    p_notas   text
) returns uuid
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_user_id     text;
    v_web         text;
    v_links       text[];
    v_link        text;
    v_notas       text;
    v_mensaje     text;
    v_prospect_id uuid;
    v_state       public.prospect_state;
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    if nullif(trim(coalesce(p_nombre, '')), '') is null
        and nullif(trim(coalesce(p_empresa, '')), '') is null then
        raise exception 'name or company is required';
    end if;

    v_user_id := 'USIM' || upper(substring(
        md5(lower(trim(coalesce(p_nombre, ''))) || '|' || lower(trim(coalesce(p_empresa, ''))))
        for 8
    ));

    -- Web: misma normalización y validación que panel_corregir_web (0007) y
    -- panel_ayudar_research (0008) -- minúsculas, sin esquema, sin "www."
    -- inicial, sin ruta, query ni puerto -- copiada aquí por la misma razón
    -- que 0008 copió la de 0007: un CHECK o una función no pueden compartir
    -- el cuerpo de otra sin volverse un tercer objeto que mantener.
    v_web := nullif(trim(p_web), '');
    if v_web is not null then
        v_web := lower(v_web);
        v_web := regexp_replace(v_web, '^https?://', '');
        v_web := regexp_replace(v_web, '^www\.', '');
        v_web := split_part(v_web, '/', 1);
        v_web := split_part(v_web, '?', 1);
        v_web := split_part(v_web, ':', 1);
        if v_web !~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' then
            raise exception 'invalid domain: %', p_web;
        end if;
    end if;

    -- Enlaces: misma regla que panel_ayudar_research (0008) -- recorta,
    -- descarta vacíos, quita duplicados exactos conservando el orden, tope
    -- de 10, cada uno http/https y de 500 caracteres o menos.
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

    -- Upsert manual (no "on conflict do update") porque hace falta decidir
    -- si se rechaza antes de tocar la fila: una persona descartada no se
    -- resucita solo por rellenar el formulario otra vez.
    select id, state into v_prospect_id, v_state
        from public.prospects where slack_user_id = v_user_id
        for update;

    if found then
        if v_state = 'descartado' then
            raise exception 'person is discarded; revive it first';
        end if;
        update public.prospects
            set full_name    = coalesce(nullif(trim(p_nombre), ''), full_name),
                company_name = coalesce(nullif(trim(p_empresa), ''), company_name),
                state        = 'nuevo',
                updated_at   = now()
            where id = v_prospect_id;
    else
        insert into public.prospects (slack_user_id, full_name, company_name, state)
            values (v_user_id, nullif(trim(p_nombre), ''), nullif(trim(p_empresa), ''), 'nuevo')
            returning id into v_prospect_id;
    end if;

    -- Blank/NULL borra -- igual que panel_ayudar_research (0008) con sus
    -- propias columnas: el formulario de pruebas siempre manda su estado
    -- completo, no hace falta un tercer valor para "no lo toques".
    update public.prospects
        set company_domain_override = v_web,
            research_links          = v_links,
            research_notes          = v_notas,
            updated_at              = now()
        where id = v_prospect_id;

    -- Mensaje: exactamente como lo dejaría el vigilante de Slack (ver
    -- ingest/watcher, y ingest/resolver.py). El estado se deja directamente
    -- en 'pendiente_scoring' -- el mismo en el que resolve_pending() deja un
    -- mensaje de una persona ya conocida (no 'nuevo') -- para que:
    --   1. delivery/deliver.py::_last_message (que filtra status <> 'ignorado')
    --      pueda citarlo en la tarjeta.
    --   2. ingest.resolver.resolve_pending() (que solo mira status = 'nuevo')
    --      nunca lo vuelva a procesar ni encole un segundo job por su cuenta:
    --      el job manual de aquí abajo es el único que se encola.
    v_mensaje := nullif(trim(p_mensaje), '');
    if v_mensaje is not null then
        insert into public.slack_messages (channel_id, ts, user_id, text, status)
            values (
                'CSIMULADO',
                to_char(extract(epoch from clock_timestamp()), 'FM9999999999.000000'),
                v_user_id,
                v_mensaje,
                'pendiente_scoring'
            );
    end if;

    -- Igual que panel_corregir_web y panel_ayudar_research: la cola ya
    -- impide dos tareas abiertas para la misma persona (índice parcial de
    -- 0004), así que un segundo envío con los mismos datos no duplica nada.
    -- No hace falta comprobar el estado otra vez: si la persona ya estaba
    -- descartada, la función ya levantó la excepción más arriba.
    insert into public.research_jobs (slack_user_id, reason)
        values (v_user_id, 'manual')
    on conflict (slack_user_id) where status in ('pendiente', 'en_curso') do nothing;

    return v_prospect_id;
end;
$$;

revoke all on function public.panel_simular_persona(text, text, text, text, text[], text)
    from public, anon;
grant execute on function public.panel_simular_persona(text, text, text, text, text[], text)
    to authenticated;

-- Limpieza de todo lo simulado (script o panel): personas cuyo slack_user_id
-- empieza por "USIM" (esta función y scripts/simular.py) o "UPRUEBA"
-- (tarjeta_prueba.py), en cascada (dossiers, entregas -- ver 0002 y 0005),
-- más sus slack_messages -- que no son cascada de prospects (user_id no es
-- clave ajena, ver 0004) -- y, de paso, cualquier resto que haya quedado en
-- el canal 'CSIMULADO', que ninguna persona real usa nunca. No toca
-- research_jobs/llm_calls/cost_events/agent_actions: esas filas quedan con
-- su slack_user_id o prospect_id huérfano (el mismo comportamiento que ya
-- tenía scripts/simular.py::cmd_limpiar), no se borran ni se tocan aquí.
create or replace function public.panel_borrar_simulados()
    returns integer
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_borrados integer;
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    delete from public.slack_messages
        where user_id like 'USIM%' or user_id like 'UPRUEBA%' or channel_id = 'CSIMULADO';

    delete from public.prospects
        where slack_user_id like 'USIM%' or slack_user_id like 'UPRUEBA%';
    get diagnostics v_borrados = row_count;

    return v_borrados;
end;
$$;

revoke all on function public.panel_borrar_simulados() from public, anon;
grant execute on function public.panel_borrar_simulados() to authenticated;
