create type prospect_state as enum (
    'nuevo', 'investigado', 'contactado', 'cliente', 'descartado', 'incompleto'
);

create table prospects (
    id             uuid primary key default gen_random_uuid(),
    slack_user_id  text unique not null,
    full_name      text,
    company_name   text,
    company_domain text,
    state          prospect_state not null default 'nuevo',
    first_seen_at  timestamptz not null default now(),
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

create index prospects_state_idx on prospects (state);

-- El descarte es pegajoso: se consulta en cada mensaje entrante, tiene que ser barato.
create index prospects_discarded_idx on prospects (slack_user_id) where state = 'descartado';

alter table prospects enable row level security;
