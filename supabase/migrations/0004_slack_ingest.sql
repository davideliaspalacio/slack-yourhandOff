-- Mensajes leídos del Slack del Founders Club. Solo lectura: el agente nunca
-- escribe allí.
create table slack_messages (
    id          uuid primary key default gen_random_uuid(),
    channel_id  text not null,
    ts          text not null,
    user_id     text,
    text        text,
    subtype     text,
    thread_ts   text,
    status      text not null default 'nuevo'
                check (status in ('nuevo', 'ignorado', 'archivado', 'pendiente_scoring')),
    created_at  timestamptz not null default now(),
    unique (channel_id, ts)
);

create index slack_messages_status_idx on slack_messages (status);
create index slack_messages_user_idx on slack_messages (user_id);

-- Padrón de cada canal en cada lectura, para detectar miembros nuevos por diferencia.
create table member_snapshots (
    id          uuid primary key default gen_random_uuid(),
    channel_id  text not null,
    members     text[] not null,
    taken_at    timestamptz not null default now()
);

create index member_snapshots_channel_idx on member_snapshots (channel_id, taken_at desc);

-- Cola de research. Una sola tarea abierta por persona: dos disparadores a la
-- vez (escribió y además entró como miembro nuevo) no pagan dos investigaciones.
create table research_jobs (
    id             uuid primary key default gen_random_uuid(),
    slack_user_id  text not null,
    reason         text not null check (reason in ('mensaje', 'miembro_nuevo', 'manual')),
    status         text not null default 'pendiente'
                   check (status in ('pendiente', 'en_curso', 'hecho', 'fallido')),
    attempts       integer not null default 0,
    last_error     text,
    not_before     timestamptz,
    outcome        jsonb,
    created_at     timestamptz not null default now(),
    started_at     timestamptz,
    finished_at    timestamptz
);

create unique index research_jobs_one_open on research_jobs (slack_user_id)
    where status in ('pendiente', 'en_curso');

create index research_jobs_claim_idx on research_jobs (status, created_at);

-- Throttling del research (spec §8): investigar en ráfaga tumba la búsqueda.
insert into config (key, value) values ('research_por_hora', '20'::jsonb)
on conflict (key) do nothing;

alter table slack_messages   enable row level security;
alter table member_snapshots enable row level security;
alter table research_jobs    enable row level security;
