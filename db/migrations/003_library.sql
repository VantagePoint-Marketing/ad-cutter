-- Video agent: the reference library (step A of the library-and-loop plan approved by Robert on 2026-10-01).
-- Tier 1 = Robert's list on the craft of editing (worker/library/foundation.txt); tier 2 = niche examples (later).
-- Only platform metadata and Gemini's notes are stored here. No video is ever downloaded or kept.
-- Applied by worker/migrate.py as the worker's pre-deploy step.

create table if not exists ref_sources (
    id            bigserial primary key,
    tier          smallint not null check (tier in (1, 2)),
    platform      text not null check (platform in ('youtube', 'foreplay')),
    external_id   text not null,                      -- YouTube video id, Foreplay ad id
    url           text not null,
    title         text not null default '',
    channel       text not null default '',
    seconds       int,
    metrics       jsonb not null default '{}'::jsonb, -- views, published_at, ...
    picked_by     text not null default '',           -- 'foundation', later 'channel:<name>', 'search:<term>', ...
    status        text not null default 'new' check (status in ('new', 'working', 'done', 'failed', 'skipped')),
    reason        text,                               -- plain English, for failed and skipped
    attempts      int not null default 0,             -- watches whose notes failed the checks
    locked_by     text,
    heartbeat_at  timestamptz,
    meta_at       timestamptz,                        -- platform metadata fetched (YouTube: refresh within 30 days)
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (platform, external_id)
);
create index if not exists ref_sources_queue_idx on ref_sources (status, tier, id);

-- One row per watched part of a video (long videos are watched in parts of about ten minutes).
create table if not exists ref_notes (
    id            bigserial primary key,
    source_id     bigint not null references ref_sources(id) on delete cascade,
    created_at    timestamptz not null default now(),
    model         text not null,
    window_start  int not null,                       -- seconds into the video
    window_end    int not null,
    note          jsonb not null,
    reference_ok  boolean not null,                   -- passed the "really watched" checks; only these are used
    problems      text not null default '',
    video_tokens  int,
    cost_usd      numeric(10, 4) not null default 0   -- 0 on Google's free tier
);
create index if not exists ref_notes_source_idx on ref_notes (source_id, window_start, created_at desc);

-- Daily usage of outside quotas, per Pacific day (when Google's free-tier and YouTube quotas reset).
create table if not exists api_quota (
    api     text not null,                            -- 'gemini-free-video-seconds', 'youtube-units'
    period  text not null,                            -- 'YYYY-MM-DD' in America/Los_Angeles
    used    numeric(14, 2) not null default 0,
    primary key (api, period)
);
