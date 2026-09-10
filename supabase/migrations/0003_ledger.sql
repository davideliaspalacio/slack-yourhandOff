-- Toda llamada a un LLM, sin excepción.
create table llm_calls (
    id             uuid primary key default gen_random_uuid(),
    prospect_id    uuid references prospects (id) on delete set null,
    stage          text not null,
    model          text not null,
    input_tokens   integer not null default 0,
    cached_tokens  integer not null default 0,
    output_tokens  integer not null default 0,
    cost_usd       numeric(12, 6) not null default 0,
    latency_ms     integer,
    trace_id       text,
    created_at     timestamptz not null default now()
);

create index llm_calls_created_idx on llm_calls (created_at desc);
create index llm_calls_prospect_idx on llm_calls (prospect_id);

-- Todo coste que no es LLM: Twilio, proxies, compute, APIs de pago futuras.
create table cost_events (
    id           uuid primary key default gen_random_uuid(),
    prospect_id  uuid references prospects (id) on delete set null,
    source       text not null,
    description  text,
    cost_usd     numeric(12, 6) not null,
    created_at   timestamptz not null default now()
);

create index cost_events_created_idx on cost_events (created_at desc);

-- Log append-only de lo que hace el agente.
create table agent_actions (
    id           uuid primary key default gen_random_uuid(),
    prospect_id  uuid references prospects (id) on delete set null,
    action       text not null,
    payload      jsonb not null default '{}'::jsonb,
    result       jsonb,
    created_at   timestamptz not null default now()
);

create index agent_actions_prospect_idx on agent_actions (prospect_id, created_at desc);

-- Umbrales, topes y kill switch. Editable sin redeploy.
create table config (
    key         text primary key,
    value       jsonb not null,
    updated_at  timestamptz not null default now()
);

insert into config (key, value) values
    ('kill_switch', 'false'::jsonb),
    ('monthly_budget_usd', '150'::jsonb),
    ('run_budget_usd', '1.0'::jsonb);

alter table llm_calls     enable row level security;
alter table cost_events   enable row level security;
alter table agent_actions enable row level security;
alter table config        enable row level security;
