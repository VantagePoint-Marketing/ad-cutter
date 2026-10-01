-- Video agent: what the link-only web page needs (W1, simplified on 2026-10-01: no accounts, one shared link).
-- A job may have several clips; `sources` lists them in order. A job sits in 'uploading' while the browser sends
-- its clips straight to the bucket, and becomes 'queued' once every clip has arrived in full.
-- Applied by worker/migrate.py as the worker's pre-deploy step. The web app never runs migrations.

alter table jobs add column if not exists sources jsonb not null default '[]'::jsonb;   -- [{key, name, bytes}, ...]

alter table jobs drop constraint if exists jobs_status_check;
alter table jobs add constraint jobs_status_check
    check (status in ('uploading', 'queued', 'working', 'ready', 'failed', 'approved', 'cancelled'));

create index if not exists jobs_recent_idx on jobs (created_at desc);
