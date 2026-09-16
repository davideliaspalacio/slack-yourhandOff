-- Una fila por cada aviso enviado. Es lo que impide avisar dos veces de lo
-- mismo y lo que deja ver, por persona, qué se le mandó y cuándo.
create table deliveries (
    id            uuid primary key default gen_random_uuid(),
    prospect_id   uuid not null references prospects (id) on delete cascade,
    kind          text not null check (kind in ('slack', 'sms', 'email')),
    band          text not null check (band in ('alta', 'media', 'baja')),
    dossier_version integer,
    channel_id    text,
    message_ts    text,
    external_id   text,
    detail        jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now()
);

create index deliveries_prospect_idx on deliveries (prospect_id, created_at desc);
create index deliveries_kind_idx on deliveries (kind, created_at desc);

-- Una tarjeta por persona y versión de dossier: si el worker reintenta, no
-- se publica dos veces lo mismo.
create unique index deliveries_one_card
    on deliveries (prospect_id, dossier_version)
    where kind = 'slack';

alter table deliveries enable row level security;

insert into config (key, value) values
    ('banda_alta_min',  '3'::jsonb),
    ('banda_media_min', '2'::jsonb),
    ('banda_baja_min',  '1'::jsonb),
    ('sms_por_dia',     '3'::jsonb),
    ('sms_hora_inicio', '7'::jsonb),
    ('sms_hora_fin',    '21'::jsonb),
    ('zona_horaria',    '"America/New_York"'::jsonb)
on conflict (key) do nothing;
