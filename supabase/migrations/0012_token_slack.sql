-- El token del Founders Club (xoxp) se pega desde el panel, sin redeploy.
--
-- `secretos` es de solo escritura para el panel: RLS activa, ninguna política
-- y sin grants para anon/authenticated, así que ni un select directo devuelve
-- nada. El worker de Python conecta como dueño de la base (DATABASE_URL) y
-- la lee sin pasar por RLS. El panel solo habla con las tres funciones de
-- abajo, que comprueban is_panel_user() como el resto de panel_*.
--
-- 0001 a 0011 ya están en producción: nunca se tocan. Esto solo añade.

create table public.secretos (
    key        text primary key,
    value      text not null,
    updated_at timestamptz not null default now(),
    updated_by text
);

alter table public.secretos enable row level security;
revoke all on public.secretos from anon, authenticated;

create function public.panel_guardar_token_slack(p_token text) returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_token text := trim(coalesce(p_token, ''));
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;
    if v_token not like 'xoxp-%' then
        raise exception 'invalid token: must start with xoxp-';
    end if;
    if length(v_token) < 20 then
        raise exception 'invalid token: too short';
    end if;

    insert into public.secretos (key, value, updated_at, updated_by)
    values ('slack_user_token', v_token, now(), public.panel_email())
    on conflict (key) do update
        set value = excluded.value,
            updated_at = excluded.updated_at,
            updated_by = excluded.updated_by;
end;
$$;

-- Solo los últimos 4 caracteres: lo justo para reconocer cuál token es.
create function public.panel_estado_token_slack()
    returns table (configurado boolean, sufijo text, actualizado timestamptz, por text)
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    return query
        select (s.key is not null), right(s.value, 4), s.updated_at, s.updated_by
        from (select 1) as uno
        left join public.secretos s on s.key = 'slack_user_token';
end;
$$;

create function public.panel_borrar_token_slack() returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    delete from public.secretos where key = 'slack_user_token';
end;
$$;

revoke all on function public.panel_guardar_token_slack(text) from public, anon;
revoke all on function public.panel_estado_token_slack() from public, anon;
revoke all on function public.panel_borrar_token_slack() from public, anon;
grant execute on function public.panel_guardar_token_slack(text) to authenticated;
grant execute on function public.panel_estado_token_slack() to authenticated;
grant execute on function public.panel_borrar_token_slack() to authenticated;
