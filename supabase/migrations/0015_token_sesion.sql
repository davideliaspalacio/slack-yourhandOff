-- El panel acepta también credenciales de sesión del navegador: un token xoxc-
-- más el valor de la cookie `d` (xoxd-). Es el respaldo para cuando el workspace
-- del Founders Club no deja instalar una app (xoxp). Dura pocos días.
--
-- 0014 ya está en producción: no se toca. Se reemplazan las tres funciones; las
-- dos que cambian de firma se borran primero y se vuelven a conceder igual.
-- La cookie vive en `secretos` bajo la clave slack_d_cookie, con las mismas
-- reglas que el token: solo escritura desde el panel, nunca se devuelve.

drop function public.panel_guardar_token_slack(text);
drop function public.panel_estado_token_slack();

create function public.panel_guardar_token_slack(p_token text, p_cookie text default null)
    returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_token  text := trim(coalesce(p_token, ''));
    v_cookie text := trim(coalesce(p_cookie, ''));
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    if v_token like 'xoxp-%' then
        if v_cookie <> '' then
            raise exception 'an app token (xoxp-) does not take a cookie';
        end if;
    elsif v_token like 'xoxc-%' then
        if v_cookie not like 'xoxd-%' then
            raise exception 'a session token (xoxc-) needs the d cookie (xoxd-…)';
        end if;
    else
        raise exception 'invalid token: must start with xoxp- or xoxc-';
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

    if v_token like 'xoxc-%' then
        insert into public.secretos (key, value, updated_at, updated_by)
        values ('slack_d_cookie', v_cookie, now(), public.panel_email())
        on conflict (key) do update
            set value = excluded.value,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by;
    else
        -- Un token de app sustituye a una sesión anterior: su cookie sobra.
        delete from public.secretos where key = 'slack_d_cookie';
    end if;
end;
$$;

-- `tipo` es 'app' (xoxp), 'session' (xoxc) o null sin token. Del token solo
-- salen los últimos 4 caracteres; de la cookie, nada.
create function public.panel_estado_token_slack()
    returns table (
        configurado boolean, tipo text, sufijo text, actualizado timestamptz, por text
    )
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    return query
        select (s.key is not null),
               case
                   when s.value like 'xoxp-%' then 'app'
                   when s.value like 'xoxc-%' then 'session'
               end,
               right(s.value, 4), s.updated_at, s.updated_by
        from (select 1) as uno
        left join public.secretos s on s.key = 'slack_user_token';
end;
$$;

create or replace function public.panel_borrar_token_slack() returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    delete from public.secretos where key in ('slack_user_token', 'slack_d_cookie');
end;
$$;

revoke all on function public.panel_guardar_token_slack(text, text) from public, anon;
revoke all on function public.panel_estado_token_slack() from public, anon;
revoke all on function public.panel_borrar_token_slack() from public, anon;
grant execute on function public.panel_guardar_token_slack(text, text) to authenticated;
grant execute on function public.panel_estado_token_slack() to authenticated;
grant execute on function public.panel_borrar_token_slack() to authenticated;
