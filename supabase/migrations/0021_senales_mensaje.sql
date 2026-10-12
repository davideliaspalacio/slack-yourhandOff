-- Señales en los mensajes: el camino barato que lee cada mensaje de Slack y
-- detecta una necesidad actual de la persona o su empresa, sin repetir el
-- research (que solo se hace a personas nuevas).
--
-- Una fila por mensaje clasificado, detectado o no: es lo que impide
-- clasificar -- y pagar -- dos veces el mismo mensaje. prospect_id es nulo
-- cuando quien escribe todavía no tiene fila en prospects.
--
-- 0001 a 0020 ya están en el repositorio: nunca se tocan. Esto solo añade.
-- (Se numera 0021 porque 0020_exigir_evidencia.sql ya existe.)

create table public.message_signals (
    id          uuid primary key default gen_random_uuid(),
    prospect_id uuid references public.prospects (id) on delete cascade,
    channel_id  text not null,
    ts          text not null,
    detectada   boolean not null,
    tipo        text check (tipo is null or tipo in (
                    'contratacion', 'soporte', 'operaciones', 'asistente', 'back_office',
                    'ventas', 'reclutamiento', 'contabilidad', 'desarrollo')),
    cita        text,
    confianza   text check (confianza is null or confianza in ('alta', 'media', 'baja')),
    created_at  timestamptz not null default now(),
    unique (channel_id, ts),
    -- Una señal detectada siempre dice de qué tipo es y de dónde sale.
    constraint message_signals_detectada_completa
        check (not detectada or (tipo is not null and cita is not null and confianza is not null))
);

-- Para los topes por persona del paso siguiente (cuántas alertas lleva y desde cuándo).
create index message_signals_prospect_idx
    on public.message_signals (prospect_id, created_at desc) where detectada;

alter table public.message_signals enable row level security;

-- El panel las mostrará más adelante: solo lectura, como el resto.
grant select on public.message_signals to authenticated;
create policy panel_lee_senales_mensaje on public.message_signals for select to authenticated
    using (public.is_panel_user());

insert into public.config (key, value) values
    ('senales_activas', 'true'::jsonb),
    -- Los dos topes siguientes los usa la publicación de alertas, no la detección.
    ('alertas_por_dia', '5'::jsonb),
    ('alertas_dias_misma_persona', '7'::jsonb)
on conflict (key) do nothing;

-- Editables desde Settings. Política nueva que se suma a panel_ajusta_umbrales
-- (0006), panel_ajusta_radar (0012) y panel_ajusta_evidencia (0020): las
-- permisivas se combinan con OR, así que la lista blanca solo crece en estas
-- tres claves. El with check fija el tipo: un interruptor solo admite true o
-- false y los topes solo números.
create policy panel_ajusta_senales on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('senales_activas', 'alertas_por_dia', 'alertas_dias_misma_persona')
    )
    with check (
        public.is_panel_user()
        and (
            (key = 'senales_activas' and jsonb_typeof(value) = 'boolean')
            or (key in ('alertas_por_dia', 'alertas_dias_misma_persona')
                and jsonb_typeof(value) = 'number')
        )
    );
