-- Acceso del panel web (Plan 4).
--
-- El panel habla con la base a través de la API de Supabase, con la sesión del
-- usuario (rol authenticated). Supabase deja registrarse a cualquiera por
-- defecto, así que "haber iniciado sesión" no basta: solo los correos de
-- panel_users ven algo. Los correos no viven en esta migración porque el
-- repositorio es público; se añaden con un insert a mano.
--
-- El worker entra como postgres y no pasa por nada de esto.

create table panel_users (
    email     text primary key check (email = lower(email)),
    added_at  timestamptz not null default now()
);
alter table panel_users enable row level security;

-- El correo sale del token que PostgREST deja en la sesión. Es lo que hace
-- auth.jwt() por dentro, pero sin depender del esquema auth, que la base de
-- los tests no tiene.
create function public.panel_email() returns text
    language sql stable
    set search_path = ''
as $$
    select lower(nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'email')
$$;

-- security definer: tiene que poder mirar panel_users aunque el usuario no pueda.
create function public.is_panel_user() returns boolean
    language sql stable security definer
    set search_path = ''
as $$
    select exists (select 1 from public.panel_users where email = public.panel_email())
$$;

revoke all on function public.panel_email() from public;
revoke all on function public.is_panel_user() from public;
grant execute on function public.panel_email() to authenticated;
grant execute on function public.is_panel_user() to authenticated;

-- Permisos explícitos y mínimos. De escritura, solo las tres acciones del
-- spec §11, y columna a columna: cambiar el estado, volver a investigar y
-- ajustar umbrales. Nada de borrar, nada de tocar dossiers.
revoke all on prospects, dossiers, slack_messages, member_snapshots, research_jobs,
    deliveries, agent_actions, llm_calls, cost_events, config, panel_users
    from anon, authenticated;

grant usage on schema public to authenticated;
grant select on prospects, dossiers, slack_messages, research_jobs, deliveries,
    agent_actions, llm_calls, cost_events, config to authenticated;
grant update (state, updated_at) on prospects to authenticated;
grant insert (slack_user_id, reason) on research_jobs to authenticated;
grant update (value, updated_at) on config to authenticated;

create policy panel_lee_prospects on prospects for select to authenticated using (public.is_panel_user());
create policy panel_lee_dossiers on dossiers for select to authenticated using (public.is_panel_user());
create policy panel_lee_mensajes on slack_messages for select to authenticated using (public.is_panel_user());
create policy panel_lee_cola on research_jobs for select to authenticated using (public.is_panel_user());
create policy panel_lee_entregas on deliveries for select to authenticated using (public.is_panel_user());
create policy panel_lee_acciones on agent_actions for select to authenticated using (public.is_panel_user());
create policy panel_lee_llm on llm_calls for select to authenticated using (public.is_panel_user());
create policy panel_lee_costes on cost_events for select to authenticated using (public.is_panel_user());
create policy panel_lee_config on config for select to authenticated using (public.is_panel_user());

-- Acción 1: cambiar el estado. 'incompleto' lo decide el research, no una persona.
create policy panel_cambia_estado on prospects for update to authenticated
    using (public.is_panel_user())
    with check (
        public.is_panel_user()
        and state in ('nuevo', 'investigado', 'contactado', 'cliente', 'descartado')
    );

-- Acción 2: volver a investigar. Solo tareas manuales; la cola ya impide dos
-- abiertas para la misma persona.
create policy panel_reinvestiga on research_jobs for insert to authenticated
    with check (public.is_panel_user() and reason = 'manual' and status = 'pendiente');

-- Acción 3: ajustar umbrales. El kill switch y los topes de dinero se tocan a
-- mano, nunca desde una pantalla.
create policy panel_ajusta_umbrales on config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('banda_alta_min', 'banda_media_min', 'banda_baja_min', 'research_por_hora',
                    'sms_por_dia', 'sms_hora_inicio', 'sms_hora_fin')
    )
    with check (
        public.is_panel_user()
        and key in ('banda_alta_min', 'banda_media_min', 'banda_baja_min', 'research_por_hora',
                    'sms_por_dia', 'sms_hora_inicio', 'sms_hora_fin')
    );

-- Gasto por día ya sumado. security_invoker: sin él, la vista correría con los
-- permisos de su dueño y se saltaría las políticas de arriba.
create view panel_costes_diarios with (security_invoker = true) as
select dia, sum(usd) as usd, sum(llamadas) as llamadas
from (
    select date(created_at) as dia, sum(cost_usd) as usd, count(*) as llamadas
    from llm_calls group by 1
    union all
    select date(created_at), sum(cost_usd), count(*) from cost_events group by 1
) t
group by dia
order by dia desc;

grant select on panel_costes_diarios to authenticated;
