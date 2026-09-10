create table dossiers (
    id           uuid primary key default gen_random_uuid(),
    prospect_id  uuid not null references prospects (id) on delete cascade,
    version      integer not null,
    content      jsonb not null,
    sources      jsonb not null default '[]'::jsonb,
    created_at   timestamptz not null default now(),
    unique (prospect_id, version)
);

create index dossiers_prospect_version_idx on dossiers (prospect_id, version desc);

alter table dossiers enable row level security;
