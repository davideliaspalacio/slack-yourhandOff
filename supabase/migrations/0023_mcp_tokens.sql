-- Tokens por persona para el MCP remoto de solo lectura (Fase 1).
--
-- Cada persona del panel crea desde Settings uno o varios tokens para conectar su
-- Claude a `https://<receptor>/mcp`. En la base solo vive el hash sha256 del token y
-- sus últimos 4 caracteres: el token en claro se devuelve UNA vez, al crearlo, y no
-- se puede recuperar después.
--
-- `mcp_tokens` es opaca para el panel, como `secretos` en 0014: RLS activa, ninguna
-- política y sin grants para anon/authenticated, así que ni un select directo
-- devuelve nada. El worker (web/mcp_auth.py) conecta como dueño de la base y la
-- lee sin pasar por RLS. El panel solo habla con las tres funciones de abajo, que
-- comprueban is_panel_user() como el resto de panel_*.
--
-- Aleatoriedad: el token sale de gen_random_bytes (pgcrypto). Supabase instala
-- pgcrypto en el esquema `extensions`, pero 0010 ya avisó de que la extensión no está
-- garantizada en todos los entornos (la base de los tests, creada desde template1, no
-- la trae). Por eso esta migración la crea en `extensions` si falta, localiza el
-- esquema en el que de verdad está y construye una función interna que llama a
-- <esquema>.gen_random_bytes. Las funciones del panel solo conocen esa función
-- interna, así que no dependen del esquema.
--
-- 0001 a 0022 ya están en producción: nunca se tocan. Esto solo añade.

create table public.mcp_tokens (
    id            uuid primary key default gen_random_uuid(),
    -- Minúsculas, el mismo correo que panel_users.email (panel_email() ya lo baja).
    email         text not null check (email = lower(email)),
    -- Etiqueta que pone la persona, p. ej. "Claude de Anthony".
    nombre        text not null check (char_length(nombre) between 1 and 60),
    -- sha256 del token en hexadecimal. Nunca el token.
    token_hash    text not null unique,
    sufijo        text not null check (char_length(sufijo) = 4),
    creado_at     timestamptz not null default now(),
    ultimo_uso_at timestamptz,
    revocado_at   timestamptz,
    -- Llamadas a herramientas hechas en `dia_uso` (el día en la zona de config
    -- `zona_horaria`). Cuando cambia el día, el contador vuelve a empezar.
    usos_hoy      integer not null default 0 check (usos_hoy >= 0),
    dia_uso       date
);

create index mcp_tokens_email_idx on public.mcp_tokens (email) where revocado_at is null;

alter table public.mcp_tokens enable row level security;
revoke all on public.mcp_tokens from anon, authenticated;

-- Esquema de pgcrypto -> función interna que genera los 32 bytes aleatorios.
do $do$
declare
    v_schema text;
begin
    create schema if not exists extensions;
    create extension if not exists pgcrypto with schema extensions;

    select n.nspname
      into v_schema
      from pg_catalog.pg_extension e
      join pg_catalog.pg_namespace n on n.oid = e.extnamespace
     where e.extname = 'pgcrypto';

    execute format($f$
        create function public.mcp_token_aleatorio() returns text
            language sql
            volatile
            set search_path = ''
        as $b$
            select encode(%I.gen_random_bytes(32), 'hex')
        $b$
    $f$, v_schema);
end;
$do$;

-- Interna: solo la llaman las funciones security definer de abajo, que corren como
-- dueño. Ni anon ni authenticated (ni public) pueden ejecutarla directamente.
revoke all on function public.mcp_token_aleatorio() from public, anon, authenticated;

-- Crea un token y lo devuelve en claro, esta única vez. Máximo 5 activos por persona.
create function public.panel_crear_token_mcp(p_nombre text) returns text
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_email  text := public.panel_email();
    v_nombre text := trim(coalesce(p_nombre, ''));
    v_token  text;
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;
    if char_length(v_nombre) not between 1 and 60 then
        raise exception 'invalid name: use 1 to 60 characters';
    end if;

    -- Dos clics a la vez no deben pasarse del máximo.
    perform pg_advisory_xact_lock(hashtext('mcp_tokens:' || v_email));
    if (select count(*) from public.mcp_tokens
         where email = v_email and revocado_at is null) >= 5 then
        raise exception 'too many tokens: revoke one first';
    end if;

    v_token := public.mcp_token_aleatorio();
    insert into public.mcp_tokens (email, nombre, token_hash, sufijo)
    values (
        v_email,
        v_nombre,
        encode(sha256(convert_to(v_token, 'UTF8')), 'hex'),
        right(v_token, 4)
    );
    return v_token;
end;
$$;

-- Los tokens de quien llama, activos y revocados. Nunca el hash.
create function public.panel_tokens_mcp()
    returns table (
        id uuid, nombre text, sufijo text,
        creado_at timestamptz, ultimo_uso_at timestamptz, revocado_at timestamptz
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
        select t.id, t.nombre, t.sufijo, t.creado_at, t.ultimo_uso_at, t.revocado_at
          from public.mcp_tokens t
         where t.email = public.panel_email()
         order by t.creado_at desc;
end;
$$;

-- Solo revoca un token propio. Surte efecto al instante: el MCP mira revocado_at en
-- cada petición. Revocar uno ya revocado no es un error.
create function public.panel_revocar_token_mcp(p_id uuid) returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_email text := public.panel_email();
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;
    -- El mismo mensaje para "no existe" y "es de otra persona": no se confirma que exista.
    if not exists (select 1 from public.mcp_tokens where id = p_id and email = v_email) then
        raise exception 'token not found';
    end if;

    update public.mcp_tokens
       set revocado_at = now()
     where id = p_id and email = v_email and revocado_at is null;
end;
$$;

revoke all on function public.panel_crear_token_mcp(text) from public, anon;
revoke all on function public.panel_tokens_mcp() from public, anon;
revoke all on function public.panel_revocar_token_mcp(uuid) from public, anon;
grant execute on function public.panel_crear_token_mcp(text) to authenticated;
grant execute on function public.panel_tokens_mcp() to authenticated;
grant execute on function public.panel_revocar_token_mcp(uuid) to authenticated;

-- Tope diario de llamadas por token. Editable desde Settings: política nueva que se
-- suma a las de 0006, 0012, 0020 y 0022 (las permisivas se combinan con OR, así que
-- la lista blanca solo crece en esta clave). Solo un número, no negativo: 0 apaga el
-- MCP remoto para todos.
insert into public.config (key, value) values
    ('mcp_llamadas_por_dia', '500'::jsonb)
on conflict (key) do nothing;

create policy panel_ajusta_mcp on public.config for update to authenticated
    using (public.is_panel_user() and key = 'mcp_llamadas_por_dia')
    with check (
        public.is_panel_user()
        and key = 'mcp_llamadas_por_dia'
        and jsonb_typeof(value) = 'number'
        -- El cast solo se evalúa si el tipo es número: un CASE sí garantiza el orden.
        and case when jsonb_typeof(value) = 'number' then (value #>> '{}')::numeric >= 0
                 else false end
    );
