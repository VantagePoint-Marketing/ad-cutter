-- Video agent: tables the processing worker needs (W0).
-- Auth tables and the least-privilege web/worker roles arrive with the web app (W1).
-- Applied by worker/migrate.py as a Railway pre-deploy step, never from the web app.

create extension if not exists pgcrypto;

create table if not exists jobs (
    id            uuid primary key default gen_random_uuid(),
    created_at    timestamptz not null default now(),
    created_by    text not null,                                   -- user id from the web app ('cli' for tests)
    kind          text not null default 'edit' check (kind in ('edit', 'replan', 'rebuild')),
    parent_job    uuid references jobs(id),
    source_key    text not null,                                   -- bucket key of the raw upload
    source_name   text not null,                                   -- original file name, for display only
    options       jsonb not null default '{}'::jsonb,              -- ad_count, note, only
    status        text not null default 'queued'
                  check (status in ('queued', 'working', 'ready', 'failed', 'approved', 'cancelled')),
    stage         text,                                            -- preparing/transcribing/planning/rendering/uploading
    stage_detail  text,
    attempts      int not null default 0,
    max_attempts  int not null default 3,                          -- first try + 2 retries, then it stays failed
    locked_by     text,
    heartbeat_at  timestamptz,
    started_at    timestamptz,
    finished_at   timestamptz,
    error         text,                                            -- plain-English reason when failed
    result        jsonb,                                           -- plan, per-ad report, file keys, notes
    cost_usd      numeric(10, 4) not null default 0
);
create index if not exists jobs_queue_idx on jobs (status, created_at);
create index if not exists jobs_owner_idx on jobs (created_by, created_at desc);

create table if not exists job_events (
    id       bigserial primary key,
    job_id   uuid not null references jobs(id) on delete cascade,
    at       timestamptz not null default now(),
    message  text not null
);
create index if not exists job_events_job_idx on job_events (job_id, at);

-- One row per OpenRouter key per month. Rows are locked (SELECT ... FOR UPDATE) while reserving, so parallel
-- workers can never overshoot the cap together.
create table if not exists budget_months (
    key_name  text not null,
    month     text not null,                                       -- 'YYYY-MM' (UTC)
    cap_usd   numeric(10, 2) not null,
    spent     numeric(12, 6) not null default 0,
    reserved  numeric(12, 6) not null default 0,
    primary key (key_name, month)
);

create table if not exists spend_ledger (
    id          uuid primary key,
    key_name    text not null,
    month       text not null,
    job_id      uuid references jobs(id) on delete set null,
    label       text not null default '',
    model       text not null default '',
    reserved    numeric(12, 6) not null,
    cost        numeric(12, 6),
    status      text not null default 'reserved' check (status in ('reserved', 'settled')),
    note        text not null default '',
    created_at  timestamptz not null default now(),
    settled_at  timestamptz
);
create index if not exists spend_ledger_open_idx on spend_ledger (status, created_at);
