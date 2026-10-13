-- OAuth 2.1 para el MCP remoto (Fase 3): conectar Claude sin copiar ninguna llave.
--
-- Hasta aquí, conectar Claude al MCP era: entrar al panel, crear un token (0023),
-- copiarlo y pegarlo en un JSON. Inviable para quien no programa, e imposible desde
-- Claude en el navegador o en el móvil. Con esto el flujo es: "Add custom connector",
-- pegar la URL, "Connect", entrar con el correo y la contraseña del panel.
--
-- Lo que implementa el servidor (src/handoff_agent/web/oauth.py) sobre estas tablas:
-- la especificación de autorización de MCP (revisión vigente 2026-07-28), que a su vez
-- apoya en OAuth 2.1 (draft-ietf-oauth-v2-1), RFC 9728 (metadatos del recurso),
-- RFC 8414 (metadatos del servidor), RFC 7591 (registro dinámico), RFC 7636 (PKCE),
-- RFC 8707 (parámetro `resource`), RFC 9207 (`iss`) y RFC 7009 (revocación).
--
-- POR QUÉ TABLAS HERMANAS Y NO COLUMNAS EN `mcp_tokens` (la pregunta del brief).
-- Un token estático de 0023 es una credencial que la persona crea, que no caduca, y
-- cuya fila ES la unidad de todo: el contador del tope diario, `ultimo_uso_at` y el
-- botón "Revoke" del panel. Un access token OAuth vive 8 horas y lo sustituye el
-- refresh: si fuera una fila de `mcp_tokens` con `expira_at`, entonces
--   1. el contador diario (`usos_hoy`) se reiniciaría en cada refresh, es decir cada
--      8 horas, y el tope dejaría de ser un tope;
--   2. el límite de 5 tokens activos por persona y el "Revoke" del panel pasarían a
--      referirse a credenciales que el cliente rota solo, sin que nadie las vea;
--   3. `mcp_tokens` ya está en producción y la lee la puerta en cada petición.
-- Así que la unidad equivalente al token estático no es el access token: es la SESIÓN
-- (`oauth_sesiones`), la autorización que una persona concedió a un cliente y que
-- sobrevive a los refrescos. Ahí viven el contador diario, el último uso y la
-- revocación, exactamente como en `mcp_tokens`; los tokens son filas efímeras que
-- apuntan a ella. `mcp_tokens` no se toca: los tokens estáticos siguen funcionando
-- igual, y la puerta acepta los dos tipos.
--
-- Opacidad: las cinco tablas son opacas para el panel, como `mcp_tokens` (0023) y
-- `secretos` (0014): RLS activa, ninguna política y sin grants para anon/authenticated,
-- así que ni un select directo devuelve nada. El servicio web conecta como dueño de la
-- base y las lee sin pasar por RLS. El panel solo habla con las dos funciones del final.
--
-- Nunca se guarda un valor en claro: de los códigos de autorización y de los access y
-- refresh tokens solo vive el sha256 (como en 0023). De la contraseña, nada en absoluto:
-- la verifica Supabase Auth por HTTP y no pasa de la petición.
--
-- 0001 a 0025 ya están en producción: nunca se tocan. Esto solo añade.

-- ---------------------------------------------------------------------------------
-- Clientes registrados (RFC 7591). Los crea `POST /oauth/register` sin intervención
-- humana: es lo que permite que Claude se conecte pegando solo una URL. Un cliente
-- registrado no da acceso a nada por sí mismo -- solo puede pedir que una persona
-- inicie sesión, y es esa persona (y su pertenencia a panel_users) la que decide.
--
-- Clientes públicos: no se guarda ningún client_secret porque no lo hay
-- (`token_endpoint_auth_method: "none"`, OAuth 2.1 para apps nativas). Lo que protege
-- el canje del código es PKCE, no un secreto que una app de escritorio no puede
-- guardar.
-- ---------------------------------------------------------------------------------
create table public.oauth_clients (
    -- Opaco, lo genera el servidor. No es un secreto (viaja en la URL de autorización).
    client_id     text primary key,
    -- Lo que el cliente dice llamarse; se muestra en la pantalla de login y en el panel.
    -- Texto de terceros: la plantilla lo escapa antes de pintarlo.
    client_name   text check (client_name is null or char_length(client_name) <= 200),
    -- Las únicas URLs a las que se puede devolver un código. Se comparan EXACTAS en
    -- /oauth/authorize: ni comodines ni prefijos.
    redirect_uris text[] not null check (
        array_length(redirect_uris, 1) between 1 and 5
        and array_position(redirect_uris, null) is null
    ),
    grant_types   text[] not null default array['authorization_code', 'refresh_token'],
    -- El resto de los metadatos del registro, tal como llegaron, para poder depurar
    -- qué pidió un cliente. Nunca lleva credenciales: no las hay.
    metadata      jsonb not null default '{}'::jsonb,
    creado_at     timestamptz not null default now(),
    ultimo_uso_at timestamptz
);

-- ---------------------------------------------------------------------------------
-- Una fila por intento de autorización, con dos momentos:
--
-- 1. `GET /oauth/authorize` valida los parámetros (cliente, redirect_uri exacta, PKCE
--    S256, scope, resource) y los guarda aquí. La página de login solo lleva el `id`
--    de esta fila, así que lo que el formulario manda de vuelta no puede cambiar
--    ninguno de esos parámetros ya validados.
-- 2. Si el login es válido Y el correo está en panel_users, se anota el correo y el
--    hash del código, con caducidad corta (<= 60 s, OAuth 2.1 recomienda 10 minutos
--    como máximo; aquí es un minuto).
--
-- `usado_at` hace que el código sea de un solo uso: el canje es un UPDATE condicional
-- (... where usado_at is null), así que dos canjes a la vez no pueden ganar los dos.
-- ---------------------------------------------------------------------------------
create table public.oauth_solicitudes (
    id             uuid primary key default gen_random_uuid(),
    client_id      text not null references public.oauth_clients (client_id) on delete cascade,
    -- Una de las registradas, ya comprobada. Se vuelve a comparar en el canje.
    redirect_uri   text not null,
    -- PKCE (RFC 7636): el reto S256 que manda el cliente. Solo S256; `plain` se rechaza.
    code_challenge text not null,
    scope          text not null,
    -- RFC 8707: el recurso para el que se pide el token, si el cliente lo manda.
    recurso        text,
    -- Se devuelve tal cual en la redirección, sin interpretarlo.
    state          text,
    -- Quién inició sesión. Nulo mientras la solicitud está pendiente.
    email          text check (email is null or email = lower(email)),
    -- sha256 del código. Nulo mientras no se ha emitido ninguno.
    code_hash      text unique,
    creado_at      timestamptz not null default now(),
    expira_at      timestamptz not null,
    usado_at       timestamptz
);

-- Para barrer lo caducado sin recorrer la tabla entera.
create index oauth_solicitudes_expira_idx on public.oauth_solicitudes (expira_at);

-- ---------------------------------------------------------------------------------
-- La sesión: lo que una persona autorizó a un cliente. Es el equivalente de una fila de
-- `mcp_tokens` (ver la cabecera) y lo que el panel lista y revoca. Sobrevive a la
-- rotación de los tokens, así que el tope diario cuenta aquí: una conexión, un
-- contador, igual que hoy un token estático.
-- ---------------------------------------------------------------------------------
create table public.oauth_sesiones (
    id            uuid primary key default gen_random_uuid(),
    -- Minúsculas, el mismo correo que panel_users.email. Sin clave ajena, igual que
    -- mcp_tokens: quitar a alguien de panel_users le cierra el acceso (lo comprueban
    -- las herramientas de escritura), no borra el historial de sus sesiones.
    email         text not null check (email = lower(email)),
    client_id     text not null references public.oauth_clients (client_id) on delete cascade,
    scope         text not null,
    creado_at     timestamptz not null default now(),
    ultimo_uso_at timestamptz,
    revocado_at   timestamptz,
    -- Llamadas a herramientas hechas en `dia_uso` (el día en la zona de config
    -- `zona_horaria`), igual que en mcp_tokens.
    usos_hoy      integer not null default 0 check (usos_hoy >= 0),
    dia_uso       date
);

create index oauth_sesiones_email_idx on public.oauth_sesiones (email) where revocado_at is null;

-- ---------------------------------------------------------------------------------
-- Los tokens de una sesión. Solo el hash, nunca el valor: el access token y el refresh
-- token se devuelven una vez, en la respuesta de `/oauth/token`, y no se pueden
-- recuperar.
--
-- Rotación del refresh (OAuth 2.1 para clientes públicos): canjear un refresh lo marca
-- `usado_at` y emite uno nuevo. Si alguien presenta un refresh ya usado, es que lo
-- robaron o que se duplicó: se revoca la sesión entera.
-- ---------------------------------------------------------------------------------
create table public.oauth_tokens (
    id          uuid primary key default gen_random_uuid(),
    sesion_id   uuid not null references public.oauth_sesiones (id) on delete cascade,
    tipo        text not null check (tipo in ('access', 'refresh')),
    token_hash  text not null unique,
    -- Los 4 últimos caracteres, lo único que se puede enseñar o registrar.
    sufijo      text not null check (char_length(sufijo) = 4),
    creado_at   timestamptz not null default now(),
    expira_at   timestamptz not null,
    -- Solo para los refresh: cuándo se canjeó. Un refresh usado no vuelve a valer.
    usado_at    timestamptz,
    revocado_at timestamptz
);

create index oauth_tokens_sesion_idx on public.oauth_tokens (sesion_id);
create index oauth_tokens_expira_idx on public.oauth_tokens (expira_at);

-- La sesión que salió de ese código, para poder cortarla si alguien presenta el código
-- por segunda vez. Va en un `alter` porque apunta a una tabla que se crea después, y
-- porque el orden de arriba cuenta el flujo (cliente -> solicitud -> sesión -> tokens).
alter table public.oauth_solicitudes
    add column sesion_id uuid references public.oauth_sesiones (id) on delete set null;

-- ---------------------------------------------------------------------------------
-- Intentos de login y de registro, para el límite con espera creciente. Esto expone un
-- formulario de contraseña a internet: sin freno, probar contraseñas sale gratis.
--
-- Una fila por clave ("email:ana@...", "ip:1.2.3.4", "reg:1.2.3.4"): se limita por las
-- dos cosas a la vez, porque una botnet cambia de IP y un atacante con una IP prueba
-- muchos correos. Nunca lleva contraseñas ni tokens.
-- ---------------------------------------------------------------------------------
create table public.oauth_intentos (
    clave             text primary key,
    intentos          integer not null default 0 check (intentos >= 0),
    primer_intento_at timestamptz not null default now(),
    ultimo_intento_at timestamptz not null default now(),
    bloqueado_hasta   timestamptz
);

alter table public.oauth_clients enable row level security;
alter table public.oauth_solicitudes enable row level security;
alter table public.oauth_sesiones enable row level security;
alter table public.oauth_tokens enable row level security;
alter table public.oauth_intentos enable row level security;

revoke all on public.oauth_clients from anon, authenticated;
revoke all on public.oauth_solicitudes from anon, authenticated;
revoke all on public.oauth_sesiones from anon, authenticated;
revoke all on public.oauth_tokens from anon, authenticated;
revoke all on public.oauth_intentos from anon, authenticated;

-- ---------------------------------------------------------------------------------
-- Las dos funciones del panel, en la tarjeta "Connect your Claude" junto a los tokens
-- estáticos. Mismas reglas que las de 0023: is_panel_user(), solo lo propio, y nunca
-- un hash ni un token.
-- ---------------------------------------------------------------------------------

-- Las sesiones OAuth de quien llama, activas y revocadas, con el cliente que las pidió.
create function public.panel_sesiones_oauth()
    returns table (
        id uuid, cliente text, client_id text,
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
        select s.id, coalesce(c.client_name, s.client_id), s.client_id,
               s.creado_at, s.ultimo_uso_at, s.revocado_at
          from public.oauth_sesiones s
          join public.oauth_clients c on c.client_id = s.client_id
         where s.email = public.panel_email()
         order by s.creado_at desc;
end;
$$;

-- Revocar una sesión propia. Surte efecto al instante: la puerta mira `revocado_at` en
-- cada petición, igual que con los tokens estáticos, y marca también sus tokens para
-- que un refresh en curso no pueda resucitarla. Revocar una ya revocada no es un error.
create function public.panel_revocar_sesion_oauth(p_id uuid) returns void
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
    if not exists (
        select 1 from public.oauth_sesiones where id = p_id and email = v_email
    ) then
        raise exception 'session not found';
    end if;

    update public.oauth_sesiones
       set revocado_at = now()
     where id = p_id and email = v_email and revocado_at is null;

    update public.oauth_tokens
       set revocado_at = now()
     where sesion_id = p_id and revocado_at is null;
end;
$$;

revoke all on function public.panel_sesiones_oauth() from public, anon;
revoke all on function public.panel_revocar_sesion_oauth(uuid) from public, anon;
grant execute on function public.panel_sesiones_oauth() to authenticated;
grant execute on function public.panel_revocar_sesion_oauth(uuid) to authenticated;
