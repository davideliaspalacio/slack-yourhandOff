-- Radar de cuentas objetivo (spec 2026-09-22, §4).
--
-- Tres tablas nuevas: las empresas que Handoff vigila (target_accounts), una
-- fila por vacante y cuenta (hiring_signals) y los candidatos a decisor de
-- cada vacante (decision_candidates, que usa la fase 2). Más los topes y
-- umbrales en config y las funciones con las que el panel escribe.
--
-- 0001 a 0011 ya están en producción: nunca se tocan. Esto solo añade.
--
-- Los estados son text con CHECK, igual que research_jobs y slack_messages
-- (0004): añadir un estado mañana es cambiar un CHECK, no pelearse con un enum.

create table target_accounts (
    id                   uuid primary key default gen_random_uuid(),
    name                 text not null check (char_length(trim(name)) between 1 and 200),
    -- Único si no es nulo: una empresa sin web conocida puede entrar igual.
    domain               text unique
                         check (
                             domain is null
                             or domain ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
                         ),
    -- El id numérico de la empresa en LinkedIn ("16300" para Codelco). Lo
    -- resuelve el radar vía Unipile o lo corrige una persona en el panel.
    linkedin_company_id  text check (linkedin_company_id is null or linkedin_company_id ~ '^[0-9]{1,20}$'),
    linkedin_name        text,
    careers_url          text check (careers_url is null or careers_url ~ '^https?://'),
    hubspot_company_id   text,
    source               text not null default 'manual'
                         check (source in ('manual', 'csv', 'hubspot')),
    status               text not null default 'watching'
                         check (status in ('watching', 'paused')),
    last_scan_at         timestamptz,
    last_scan_error      text,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

-- El radar busca las cuentas vigiladas que toca escanear.
create index target_accounts_scan_idx on target_accounts (status, last_scan_at nulls first);

create table hiring_signals (
    id             uuid primary key default gen_random_uuid(),
    account_id     uuid not null references target_accounts (id) on delete cascade,
    title          text not null,
    -- Título normalizado (minúsculas, sin acentos, sin "(m/f/d)" ni "- remote"):
    -- lo que decide si dos anuncios son la misma vacante.
    title_key      text not null,
    location       text,
    sources        text[] not null default '{}'
                   check (sources <@ array['linkedin', 'indeed', 'careers']::text[]),
    urls           text[] not null default '{}',
    first_seen_at  timestamptz not null default now(),
    last_seen_at   timestamptz not null default now(),
    closed_at      timestamptz,
    reposted       boolean not null default false,
    score          integer not null default 0 check (score between 0 and 10),
    status         text not null default 'new'
                   check (status in ('new', 'pursued', 'dismissed', 'snoozed',
                                     'researching', 'ready', 'task_created')),
    snoozed_until  timestamptz,
    department     text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (account_id, title_key)
);

-- La bandeja Signals del panel: abiertas por score.
create index hiring_signals_inbox_idx on hiring_signals (status, score desc) where closed_at is null;

create table decision_candidates (
    id           uuid primary key default gen_random_uuid(),
    signal_id    uuid not null references hiring_signals (id) on delete cascade,
    linkedin_id  text not null,
    full_name    text,
    headline     text,
    profile_url  text check (profile_url is null or profile_url ~ '^https://([a-z]+\.)?linkedin\.com/'),
    location     text,
    rank         integer,
    reason       text,
    chosen       boolean not null default false,
    -- Cuando el elegido se investiga entra en prospects como 'li:<linkedin_id>'.
    prospect_id  uuid references prospects (id) on delete set null,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    unique (signal_id, linkedin_id)
);

-- Un solo decisor elegido por vacante.
create unique index decision_candidates_one_chosen on decision_candidates (signal_id) where chosen;

alter table target_accounts     enable row level security;
alter table hiring_signals      enable row level security;
alter table decision_candidates enable row level security;

-- Topes y umbrales (spec §3 y §5). Los de Unipile, muy por debajo de lo que
-- recomienda Unipile (~100 perfiles/día): la cuenta de LinkedIn es de Anthony.
insert into config (key, value) values
    ('unipile_busquedas_por_dia',  '25'::jsonb),
    ('unipile_perfiles_por_dia',   '40'::jsonb),
    ('unipile_hora_inicio',        '8'::jsonb),
    ('unipile_hora_fin',           '19'::jsonb),
    ('unipile_zona',               '"America/New_York"'::jsonb),
    ('unipile_pausado',            'false'::jsonb),
    ('radar_horas_entre_escaneos', '20'::jsonb),
    ('radar_dias_para_cerrar',     '3'::jsonb),
    ('radar_auto_min',             '7'::jsonb),
    ('radar_roles_ignorados',
     '["intern", "internship", "práctica", "prácticas", "practicante", "becario", "becaria", "pasante", "pasantía", "trainee", "apprentice", "werkstudent", "working student"]'::jsonb)
on conflict (key) do nothing;

-- Lectura para el panel; escritura solo por las funciones de abajo, igual que
-- 0007, 0008 y 0010. Supabase concede todo a anon y authenticated en las
-- tablas nuevas por defecto: se revoca explícitamente.
revoke all on target_accounts, hiring_signals, decision_candidates from anon, authenticated;
grant select on target_accounts, hiring_signals, decision_candidates to authenticated;

create policy panel_lee_cuentas on target_accounts for select to authenticated
    using (public.is_panel_user());
create policy panel_lee_senales on hiring_signals for select to authenticated
    using (public.is_panel_user());
create policy panel_lee_candidatos on decision_candidates for select to authenticated
    using (public.is_panel_user());

-- Ajustes del radar desde Settings. Política nueva, que se suma a
-- panel_ajusta_umbrales (0006): las políticas permisivas se combinan con OR.
-- unipile_pausado está a propósito: reactivar Unipile tras una pausa es una
-- decisión humana, y Settings es donde se toma. La franja horaria, la zona y
-- los roles ignorados se tocan a mano.
create policy panel_ajusta_radar on config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('radar_auto_min', 'radar_horas_entre_escaneos', 'unipile_busquedas_por_dia',
                    'unipile_perfiles_por_dia', 'unipile_pausado')
    )
    with check (
        public.is_panel_user()
        and key in ('radar_auto_min', 'radar_horas_entre_escaneos', 'unipile_busquedas_por_dia',
                    'unipile_perfiles_por_dia', 'unipile_pausado')
    );

-- Alta de una cuenta desde el panel. Con dominio, upsert por dominio; sin él,
-- una cuenta sin dominio con el mismo nombre (sin distinguir mayúsculas) se
-- reutiliza en vez de duplicarse. Un linkedin_company_id vacío no borra el que
-- el radar ya resolvió.
create or replace function public.panel_agregar_cuenta(
    p_nombre              text,
    p_dominio             text,
    p_linkedin_company_id text
) returns uuid
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_nombre   text;
    v_dominio  text;
    v_linkedin text;
    v_id       uuid;
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    v_nombre := nullif(trim(p_nombre), '');
    if v_nombre is null then
        raise exception 'name is required';
    end if;
    if char_length(v_nombre) > 200 then
        raise exception 'invalid name: must be 200 characters or fewer';
    end if;

    -- Misma normalización que panel_corregir_web (0007): minúsculas, sin
    -- esquema, sin "www." inicial, sin ruta, query ni puerto.
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

    v_linkedin := nullif(trim(p_linkedin_company_id), '');
    if v_linkedin is not null and v_linkedin !~ '^[0-9]{1,20}$' then
        raise exception 'invalid LinkedIn company id: %', p_linkedin_company_id;
    end if;

    if v_dominio is not null then
        insert into public.target_accounts (name, domain, linkedin_company_id, source)
            values (v_nombre, v_dominio, v_linkedin, 'manual')
        on conflict (domain) do update
            set name                = excluded.name,
                linkedin_company_id = coalesce(excluded.linkedin_company_id,
                                               public.target_accounts.linkedin_company_id),
                updated_at          = now()
        returning id into v_id;
        return v_id;
    end if;

    select id into v_id from public.target_accounts
        where domain is null and lower(name) = lower(v_nombre)
        order by created_at
        limit 1
        for update;

    if found then
        update public.target_accounts
            set name                = v_nombre,
                linkedin_company_id = coalesce(v_linkedin, linkedin_company_id),
                updated_at          = now()
            where id = v_id;
    else
        insert into public.target_accounts (name, linkedin_company_id, source)
            values (v_nombre, v_linkedin, 'manual')
            returning id into v_id;
    end if;
    return v_id;
end;
$$;

-- Pause/Resume de una cuenta.
create or replace function public.panel_estado_cuenta(p_id uuid, p_estado text)
    returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;
    if p_estado is null or p_estado not in ('watching', 'paused') then
        raise exception 'invalid status: %', p_estado;
    end if;

    update public.target_accounts set status = p_estado, updated_at = now() where id = p_id;
    if not found then
        raise exception 'account % not found', p_id;
    end if;
end;
$$;

-- Scan now: olvidar el último escaneo basta para que el siguiente ciclo del
-- worker la escanee. El panel nunca ejecuta Python.
create or replace function public.panel_escanear_cuenta(p_id uuid)
    returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    update public.target_accounts set last_scan_at = null, updated_at = now() where id = p_id;
    if not found then
        raise exception 'account % not found', p_id;
    end if;
end;
$$;

-- Pursue / Dismiss / Snooze sobre una vacante. Solo mueve los estados del
-- principio del embudo: researching, ready y task_created los pone el worker,
-- y una persona no puede devolver a pursued una señal que ya se está
-- investigando.
create or replace function public.panel_accion_senal(p_id uuid, p_accion text)
    returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_status text;
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    select status into v_status from public.hiring_signals where id = p_id for update;
    if not found then
        raise exception 'signal % not found', p_id;
    end if;

    if p_accion = 'pursue' then
        if v_status not in ('new', 'snoozed', 'dismissed') then
            raise exception 'signal cannot be pursued from status %', v_status;
        end if;
        update public.hiring_signals
            set status = 'pursued', snoozed_until = null, updated_at = now()
            where id = p_id;
    elsif p_accion = 'dismiss' then
        if v_status not in ('new', 'snoozed', 'pursued') then
            raise exception 'signal cannot be dismissed from status %', v_status;
        end if;
        update public.hiring_signals
            set status = 'dismissed', snoozed_until = null, updated_at = now()
            where id = p_id;
    elsif p_accion = 'snooze' then
        if v_status not in ('new', 'snoozed', 'pursued', 'dismissed') then
            raise exception 'signal cannot be snoozed from status %', v_status;
        end if;
        update public.hiring_signals
            set status = 'snoozed', snoozed_until = now() + interval '7 days', updated_at = now()
            where id = p_id;
    else
        raise exception 'invalid action: %', p_accion;
    end if;
end;
$$;

-- Elegir otro decisor para la vacante. Dos sentencias, no una: el índice
-- único parcial se comprueba fila a fila, y un solo update que desmarca uno y
-- marca otro puede chocar a medio camino según el orden en que los recorra.
create or replace function public.panel_elegir_candidato(p_id uuid)
    returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
declare
    v_signal uuid;
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;

    select signal_id into v_signal from public.decision_candidates where id = p_id;
    if not found then
        raise exception 'candidate % not found', p_id;
    end if;

    update public.decision_candidates
        set chosen = false, updated_at = now()
        where signal_id = v_signal and chosen and id <> p_id;
    update public.decision_candidates
        set chosen = true, updated_at = now()
        where id = p_id;
end;
$$;

revoke all on function public.panel_agregar_cuenta(text, text, text) from public, anon;
revoke all on function public.panel_estado_cuenta(uuid, text) from public, anon;
revoke all on function public.panel_escanear_cuenta(uuid) from public, anon;
revoke all on function public.panel_accion_senal(uuid, text) from public, anon;
revoke all on function public.panel_elegir_candidato(uuid) from public, anon;
grant execute on function public.panel_agregar_cuenta(text, text, text) to authenticated;
grant execute on function public.panel_estado_cuenta(uuid, text) to authenticated;
grant execute on function public.panel_escanear_cuenta(uuid) to authenticated;
grant execute on function public.panel_accion_senal(uuid, text) to authenticated;
grant execute on function public.panel_elegir_candidato(uuid) to authenticated;
