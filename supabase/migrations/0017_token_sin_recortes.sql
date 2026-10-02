-- Un valor copiado de la vista recortada de Chrome trae "…" (U+2026) en medio.
-- Con eso el worker reventaba al montar la cabecera Cookie (latin-1 no puede
-- codificarlo) y se quedaba fallando en bucle. Se rechaza al guardar, que es
-- donde se puede explicar qué hacer. 0001 a 0016 ya están en producción.
create or replace function public.panel_guardar_token_slack(p_token text, p_cookie text default null)
    returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_token  text := btrim(coalesce(p_token, ''));
    v_cookie text := btrim(coalesce(p_cookie, ''));
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    if v_token = '' then
        raise exception 'invalid token: empty';
    end if;

    -- Solo ASCII imprimible: una credencial de Slack no lleva otra cosa.
    if v_token ~ '[^\x20-\x7E]' or v_cookie ~ '[^\x20-\x7E]' then
        raise exception
            'the value looks truncated (it contains … or a space): in DevTools use '
            'right click on the cookie row and Copy value, never the shortened text';
    end if;

    if v_token like 'xoxp-%' then
        if v_cookie <> '' then
            raise exception 'an app token (xoxp-) does not take a cookie';
        end if;
    elsif v_token like 'xoxc-%' then
        if v_cookie not like 'xoxd-%' then
            raise exception 'a session token (xoxc-) needs the d cookie (xoxd-...)';
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
        delete from public.secretos where key = 'slack_d_cookie';
    end if;
end;
$$;

revoke all on function public.panel_guardar_token_slack(text, text) from public, anon;
grant execute on function public.panel_guardar_token_slack(text, text) to authenticated;
