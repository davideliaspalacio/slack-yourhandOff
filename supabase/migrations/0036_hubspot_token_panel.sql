-- Arregla dos cosas de 0035 que impedían pegar la llave de HubSpot desde el panel.
--
-- 1. **Faltaban los permisos.** 0035 revocaba las tres funciones de `public` y `anon`,
--    copiando esa parte de 0014, pero no copió los `grant execute ... to authenticated`
--    que vienen justo después. `authenticated` solo tenía permiso a través de `PUBLIC`,
--    así que revocarlo de `PUBLIC` se lo quitaba: nadie podía guardar la llave desde
--    Settings, ni siquiera estando en `panel_users`.
--
-- 2. **`panel_estado_token_hubspot` no devolvía lo mismo que la de Slack**, a pesar de
--    que el comentario decía que seguía su contrato. La de Slack hace un LEFT JOIN
--    contra `(select 1)` y por eso devuelve SIEMPRE una fila: `(false, null, …)` cuando
--    no hay llave. La de HubSpot devolvía cero filas en ese caso, así que el panel,
--    escrito contra la forma de Slack, se habría roto al no encontrar ninguna.
--    Se le añade además `actualizado_por`, el rastro de quién la pegó, que la de Slack
--    guarda y esta perdía.
--
-- 0001 a 0035 ya están aplicadas: nunca se tocan. Esto solo corrige lo suyo.

-- `create or replace` no cambia la firma, pero esta sí cambia (una columna más), así que
-- hay que borrarla antes.
drop function if exists public.panel_estado_token_hubspot();

create function public.panel_estado_token_hubspot()
    returns table (
        configurado boolean,
        ultimos4 text,
        actualizado timestamptz,
        actualizado_por text
    )
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;
    -- Siempre una fila, como la de Slack: sin llave devuelve (false, null, null, null) y
    -- no una respuesta vacía que el panel tendría que distinguir aparte.
    return query
        select (s.key is not null), right(s.value, 4), s.updated_at, s.updated_by
          from (select 1) as uno
          left join public.secretos s on s.key = 'hubspot_token';
end;
$$;

-- Y el rastro de quién la pegó, que 0035 no guardaba.
create or replace function public.panel_guardar_token_hubspot(p_token text) returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;
    if p_token is null or btrim(p_token) = '' then
        raise exception 'empty token';
    end if;
    -- El valor recortado con "…" es el error más común al pegar desde una pantalla.
    if p_token like '%…%' or p_token like '%...%' then
        raise exception 'the value looks truncated';
    end if;
    insert into public.secretos (key, value, updated_at, updated_by)
    values ('hubspot_token', btrim(p_token), now(), public.panel_email())
    on conflict (key) do update
        set value = excluded.value,
            updated_at = excluded.updated_at,
            updated_by = excluded.updated_by;
end;
$$;

revoke all on function public.panel_guardar_token_hubspot(text) from public, anon;
revoke all on function public.panel_estado_token_hubspot() from public, anon;
revoke all on function public.panel_borrar_token_hubspot() from public, anon;
grant execute on function public.panel_guardar_token_hubspot(text) to authenticated;
grant execute on function public.panel_estado_token_hubspot() to authenticated;
grant execute on function public.panel_borrar_token_hubspot() to authenticated;
