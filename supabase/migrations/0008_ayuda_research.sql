-- Ayudar al research desde el panel: empresa, web, enlaces y notas.
--
-- 0007 dejó corregir la web adivinada (company_domain_override) desde el
-- panel. Esto añade el resto de lo que el equipo de Handoff ya sabe y el
-- agente no: el nombre real de la empresa cuando el título de Slack lo dice
-- mal, enlaces sueltos (noticias, blog, la página de equipo, una vacante,
-- incluso un perfil de LinkedIn que nunca se descarga pero sí se lista) y
-- notas en texto libre para orientar el encaje. research/gather.py (equipo)
-- y research/synthesize.py (notas) son quienes de verdad los usan; esto solo
-- valida y guarda.
--
-- 0001 a 0007 ya están en producción: nunca se tocan. Esto solo añade.

alter table prospects add column company_name_override text
    check (
        company_name_override is null
        or char_length(company_name_override) between 1 and 200
    );

alter table prospects add column research_notes text
    check (research_notes is null or char_length(research_notes) <= 2000);

-- Un CHECK no puede llevar una subconsulta directamente (ni siquiera un
-- EXISTS sobre unnest()); envolverla en una función inmutable sí funciona,
-- porque el CHECK solo ve una llamada a función, no la subconsulta de dentro.
create or replace function public.valid_research_links(links text[])
    returns boolean
    language sql
    immutable
    set search_path = ''
as $$
    select links is null
        or (
            coalesce(array_length(links, 1), 0) <= 10
            and not exists (
                select 1 from unnest(links) as link
                where link !~ '^https?://' or char_length(link) > 500
            )
        )
$$;

alter table prospects add column research_links text[]
    check (public.valid_research_links(research_links));

-- 0006 dio "grant select on prospects ... to authenticated" a nivel de tabla
-- (no columna a columna): las columnas nuevas ya quedan visibles con ese
-- permiso, no hace falta repetirlo aquí.
--
-- Única vía de escritura de las columnas nuevas (y, de paso, de
-- company_domain_override, que esta función también actualiza): no se
-- concede update de ninguna de las dos a authenticated -- igual que 0007 hizo
-- con company_domain_override.
--
-- NULL o cadena en blanco en p_empresa, p_dominio, p_links o p_notas
-- significa "bórralo" (deja la columna en NULL), no "no lo toques": el
-- formulario del panel siempre manda su estado completo, así que no hace
-- falta un tercer valor para "sin cambios".
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
declare
    v_empresa       text;
    v_dominio       text;
    v_links         text[];
    v_link          text;
    v_notas         text;
    v_slack_user_id text;
    v_state         public.prospect_state;
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    v_empresa := nullif(trim(p_empresa), '');
    if v_empresa is not null and char_length(v_empresa) > 200 then
        raise exception 'invalid company name: must be 200 characters or fewer';
    end if;

    -- Dominio: misma normalización y validación que panel_corregir_web
    -- (0007) -- minúsculas, sin esquema, sin "www." inicial, sin ruta, query
    -- ni puerto -- copiada aquí porque un CHECK o una función no pueden
    -- compartir el cuerpo de otra función sin volverse un tercer objeto que
    -- mantener; panel_corregir_web se queda tal cual, código de producción
    -- puede seguir llamándola durante el despliegue.
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

    -- Enlaces: recorta cada uno, descarta los vacíos y quita duplicados
    -- exactos, conservando el orden en que el equipo los escribió (un bucle
    -- en vez de "select distinct", que reordenaría alfabéticamente).
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

    -- Igual que panel_corregir_web: una persona descartada no se reencola.
    -- La cola ya impide dos tareas abiertas para la misma persona (índice
    -- parcial de 0004), así que esto nunca duplica una en curso.
    if v_state <> 'descartado' then
        insert into public.research_jobs (slack_user_id, reason)
            values (v_slack_user_id, 'manual')
        on conflict (slack_user_id) where status in ('pendiente', 'en_curso') do nothing;
    end if;
end;
$$;

revoke all on function public.panel_ayudar_research(uuid, text, text, text[], text)
    from public, anon;
grant execute on function public.panel_ayudar_research(uuid, text, text, text[], text)
    to authenticated;
