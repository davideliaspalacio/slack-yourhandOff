-- Los canales, con nombre y gestionables desde Claude.
--
-- Dos cosas, una tabla y tres funciones:
--
-- 1. `canales` guarda el nombre que Slack le da a cada canal. `C08MRLP46G6` no le dice
--    nada a nadie; `#founders-general` sí. Lo rellena el ciclo de lectura del worker
--    (ingest/watcher.py) con conversations.info, un método de SOLO LECTURA: el agente
--    sigue sin poder escribir en el Founders Club. El nombre puede faltar (la llamada
--    falla, el canal es privado) y entonces se muestra el código, como hasta ahora.
--
-- 2. Las funciones `mcp_canal_agregar` / `mcp_canal_quitar` dejan al equipo cambiar los
--    canales vigilados desde su Claude, lo mismo que hace el panel en Settings. La
--    validación es la de `panel_guardar_canales` (0016), que ahora vive una sola vez en
--    `_normalizar_canales`: formato, mayúsculas, sin duplicados y máximo 20. Quien pueda
--    ejecutarlas elegiría el correo que quisiera, así que se les quita el execute igual
--    que a las de 0024 (ver su cabecera).
--
--    Dos cosas que la lista de 0016 no tenía que mirar y estas sí:
--    - El canal donde el bot publica las tarjetas NUNCA puede vigilarse (lo impide
--      también delivery/slack_writer.py). Vive en el entorno del worker, no en la base,
--      así que llega como argumento: `p_tarjetas`.
--    - `secretos.slack_channel_ids()` usa SLACK_CHANNEL_IDS del entorno cuando la lista
--      de la base está vacía. Añadir un canal sobre una lista vacía dejaría de vigilar
--      los del entorno sin decirlo; por eso el respaldo llega también como argumento
--      (`p_respaldo`) y se materializa en la base antes de añadir o quitar.
--
-- 0001 a 0024 ya están en producción: nunca se tocan. Esto solo añade (más un
-- `create or replace` de panel_guardar_canales, mismo contrato, para compartir lógica).

create table public.canales (
    channel_id     text primary key,
    nombre         text,
    actualizado_at timestamptz not null default now()
);

alter table public.canales enable row level security;

-- Un id de canal y su nombre no son secretos, pero la tabla la lee el panel y nadie más:
-- mismas reglas que message_signals (0021).
grant select on public.canales to authenticated;
create policy panel_lee_canales on public.canales for select to authenticated
    using (public.is_panel_user());

-- ---------------------------------------------------------------------------------
-- Validación y normalización, compartida por el panel y el MCP. El cuerpo es el del
-- bucle de panel_guardar_canales (0016) sin cambios de comportamiento.
-- ---------------------------------------------------------------------------------
create function public._normalizar_canales(p_canales text[]) returns text[]
    language plpgsql
    immutable
    set search_path = ''
as $$
declare
    v_entrada text;
    v_canal   text;
    v_lista   text[] := '{}';
begin
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

    return v_lista;
end;
$$;

create function public._canales_escribir(p_lista text[]) returns void
    language sql
    set search_path = ''
as $$
    insert into public.config (key, value, updated_at)
    values ('slack_channel_ids', to_jsonb(coalesce(p_lista, '{}'::text[])), now())
    on conflict (key) do update
        set value = excluded.value,
            updated_at = excluded.updated_at;
$$;

-- La lista efectiva, con la fila de config bloqueada para el resto de la transacción:
-- la de la base si tiene algo, el respaldo del entorno si está vacía (la misma regla
-- que secretos.slack_channel_ids()).
create function public._canales_actuales(p_respaldo text[]) returns text[]
    language plpgsql
    set search_path = ''
as $$
declare
    v_valor jsonb;
    v_lista text[];
begin
    select value into v_valor from public.config where key = 'slack_channel_ids' for update;
    if v_valor is not null and jsonb_typeof(v_valor) = 'array' then
        -- Con el orden del array: es el orden en que el equipo los escribió.
        select array_agg(x order by n) into v_lista
          from jsonb_array_elements_text(v_valor) with ordinality as t(x, n)
         where trim(x) <> '';
    end if;
    v_lista := public._normalizar_canales(v_lista);
    if cardinality(v_lista) = 0 then
        v_lista := public._normalizar_canales(p_respaldo);
    end if;
    return v_lista;
end;
$$;

-- panel_guardar_canales: mismo contrato que en 0016 (firma, void, permisos y mensajes de
-- error); solo cambia que la validación es la función común. `create or replace` conserva
-- los grants de 0016 (authenticated sí; public y anon no).
create or replace function public.panel_guardar_canales(p_canales text[]) returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;
    perform public._canales_escribir(public._normalizar_canales(p_canales));
end;
$$;

-- ---------------------------------------------------------------------------------
-- Añadir un canal a los vigilados.
-- ---------------------------------------------------------------------------------
create function public.mcp_canal_agregar(
    p_email    text,
    p_canal    text,
    p_tarjetas text,
    p_respaldo text[]
) returns jsonb
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_canal    text;
    v_tarjetas text;
    v_antes    text[];
    v_lista    text[];
begin
    perform public._mcp_exigir_panel(p_email);

    v_lista := public._normalizar_canales(array[p_canal]);
    if cardinality(v_lista) <> 1 then
        raise exception 'invalid channel id: %', coalesce(p_canal, '');
    end if;
    v_canal := v_lista[1];

    v_tarjetas := upper(trim(coalesce(p_tarjetas, '')));
    if v_tarjetas <> '' and v_canal = v_tarjetas then
        raise exception
            'channel % is where the agent posts its cards: it can never be watched', v_canal;
    end if;

    v_antes := public._canales_actuales(p_respaldo);
    if v_canal = any(v_antes) then
        raise exception 'channel % is already watched', v_canal;
    end if;
    if cardinality(v_antes) >= 20 then
        raise exception 'too many channels (max 20): remove one before adding another';
    end if;

    v_lista := public._normalizar_canales(v_antes || v_canal);
    perform public._canales_escribir(v_lista);

    return jsonb_build_object(
        'canal', v_canal,
        'antes', to_jsonb(v_antes),
        'canales', to_jsonb(v_lista)
    );
end;
$$;

-- ---------------------------------------------------------------------------------
-- Quitar un canal de los vigilados. No borra nada más: los mensajes y las señales que
-- ese canal trajo siguen donde estaban.
-- ---------------------------------------------------------------------------------
create function public.mcp_canal_quitar(
    p_email    text,
    p_canal    text,
    p_respaldo text[]
) returns jsonb
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_canal text;
    v_antes text[];
    v_lista text[];
begin
    perform public._mcp_exigir_panel(p_email);

    v_lista := public._normalizar_canales(array[p_canal]);
    if cardinality(v_lista) <> 1 then
        raise exception 'invalid channel id: %', coalesce(p_canal, '');
    end if;
    v_canal := v_lista[1];

    v_antes := public._canales_actuales(p_respaldo);
    if not (v_canal = any(v_antes)) then
        raise exception 'channel % is not watched', v_canal;
    end if;

    select coalesce(array_agg(x order by n), '{}'::text[]) into v_lista
      from unnest(v_antes) with ordinality as t(x, n)
     where x <> v_canal;
    perform public._canales_escribir(v_lista);

    return jsonb_build_object(
        'canal', v_canal,
        'antes', to_jsonb(v_antes),
        'canales', to_jsonb(v_lista)
    );
end;
$$;

-- Nadie del lado de la API las ejecuta (misma razón que en 0024): reciben el correo como
-- argumento. Las llama el servicio web, que conecta como dueño de la base.
revoke all on function public._normalizar_canales(text[]) from public, anon, authenticated;
revoke all on function public._canales_escribir(text[]) from public, anon, authenticated;
revoke all on function public._canales_actuales(text[]) from public, anon, authenticated;
revoke all on function public.mcp_canal_agregar(text, text, text, text[])
    from public, anon, authenticated;
revoke all on function public.mcp_canal_quitar(text, text, text[])
    from public, anon, authenticated;
