-- Corregir la web desde el panel (Plan de verificación de dominio, parte B).
--
-- research/gather.py adivina el dominio cuando no hay correo de trabajo, y con
-- nombres comunes adivina mal ("Handoff" -> handoff.ai en vez de
-- yourhandoff.com). company_domain_override deja que una persona del panel
-- fije la web correcta a mano; el research vuelve a correr con ese dominio ya
-- confiado (no adivinado), sin pasar por la verificación de 0007's compañera
-- en Python (research/gather.py `_confirm_domain`).
--
-- 0001 a 0006 ya están en producción: nunca se tocan. Esto solo añade.

alter table prospects add column company_domain_override text
    check (
        company_domain_override is null
        or company_domain_override ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
    );

-- 0006 dio "grant select on prospects ... to authenticated" a nivel de tabla
-- (no columna a columna): una columna nueva ya queda visible con ese permiso,
-- no hace falta repetirlo aquí.

-- Única vía de escritura de la columna nueva: no se concede update de
-- company_domain_override a authenticated (la política panel_cambia_estado
-- de 0006 ya limita qué columnas y qué valores puede tocar el panel
-- directamente, y ensancharla no es el objetivo de esta migración).
create or replace function public.panel_corregir_web(p_prospect uuid, p_dominio text)
    returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_dominio       text;
    v_slack_user_id text;
    v_state         public.prospect_state;
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    -- Normaliza: minúsculas, sin espacios sobrantes, sin esquema, sin
    -- "www." inicial, y sin ruta, query ni puerto -- de
    -- "https://www.Acme.com/about?x=1" a "acme.com".
    v_dominio := lower(trim(p_dominio));
    v_dominio := regexp_replace(v_dominio, '^https?://', '');
    v_dominio := regexp_replace(v_dominio, '^www\.', '');
    v_dominio := split_part(v_dominio, '/', 1);
    v_dominio := split_part(v_dominio, '?', 1);
    v_dominio := split_part(v_dominio, ':', 1);

    if v_dominio !~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' then
        raise exception 'invalid domain: %', p_dominio;
    end if;

    select slack_user_id, state into v_slack_user_id, v_state
        from public.prospects where id = p_prospect;

    if not found then
        raise exception 'person % not found', p_prospect;
    end if;

    update public.prospects
        set company_domain_override = v_dominio, updated_at = now()
        where id = p_prospect;

    -- Igual que el botón "Volver a investigar": una persona descartada no se
    -- reencola. La cola ya impide dos tareas abiertas para la misma persona
    -- (índice parcial de 0004), así que esto nunca duplica una en curso.
    if v_state <> 'descartado' then
        insert into public.research_jobs (slack_user_id, reason)
            values (v_slack_user_id, 'manual')
        on conflict (slack_user_id) where status in ('pendiente', 'en_curso') do nothing;
    end if;
end;
$$;

revoke all on function public.panel_corregir_web(uuid, text) from public, anon;
grant execute on function public.panel_corregir_web(uuid, text) to authenticated;
