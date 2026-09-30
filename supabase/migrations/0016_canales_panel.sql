-- Los canales que vigila el agente se editan desde el panel, sin redeploy.
--
-- Un ID de canal no es secreto, así que vive en `config` (jsonb) y no en
-- `secretos`: el panel ya puede leerla (política panel_lee_config, 0006), de
-- modo que no hace falta una función de lectura. La escritura NO se abre con
-- la política panel_ajusta_umbrales (solo numéricos, por lista blanca de
-- claves): pasa por la función de abajo, que valida y normaliza.
--
-- Una lista vacía significa "sin valor en el panel": el worker usa entonces
-- SLACK_CHANNEL_IDS del entorno, si existe.
--
-- 0001 a 0015 ya están en producción: nunca se tocan. Esto solo añade.

insert into public.config (key, value)
values ('slack_channel_ids', '[]'::jsonb)
on conflict (key) do nothing;

create function public.panel_guardar_canales(p_canales text[]) returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_entrada text;
    v_canal   text;
    v_lista   text[] := '{}';
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    foreach v_entrada in array coalesce(p_canales, '{}'::text[]) loop
        v_canal := upper(trim(coalesce(v_entrada, '')));
        if v_canal = '' then
            continue;
        end if;
        if v_canal !~ '^[CG][A-Z0-9]{5,}$' then
            raise exception 'invalid channel id: %', trim(v_entrada);
        end if;
        -- Sin duplicados, conservando el orden en que llegaron.
        if not (v_canal = any(v_lista)) then
            v_lista := v_lista || v_canal;
        end if;
    end loop;

    if cardinality(v_lista) > 20 then
        raise exception 'too many channels (max 20)';
    end if;

    insert into public.config (key, value, updated_at)
    values ('slack_channel_ids', to_jsonb(v_lista), now())
    on conflict (key) do update
        set value = excluded.value,
            updated_at = excluded.updated_at;
end;
$$;

revoke all on function public.panel_guardar_canales(text[]) from public, anon;
grant execute on function public.panel_guardar_canales(text[]) to authenticated;
