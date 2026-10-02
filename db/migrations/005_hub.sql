-- Video agent: the knowledge hub (Robert, 2026-10-02: "build the FULL hub").
-- One graph of notes the editing agent can search and follow: sources (videos, ads), lessons, techniques, recipes
-- (how to do a technique with ffmpeg / HyperFrames), skills, rules, and examples (reference ads and videos). Notes link
-- to each other; evidence ties techniques to how the ads that used them scored. The knowledge is in our own words with a
-- pointer back to where it came from; no video and no transcript is stored. Applied by worker/migrate.py before deploy.

create table if not exists kb_items (
    id          bigserial primary key,
    kind        text not null check (kind in ('source', 'lesson', 'technique', 'recipe', 'skill', 'rule', 'example')),
    slug        text not null,                                   -- stable, human-readable: 'yt-QR8LxximqWI-41', 'punch-in'
    title       text not null,
    body        text not null default '',                        -- markdown, our own words
    tags        text[] not null default '{}',                    -- hook, pacing, captions, motion, cut, sound, story, cta ...
    meta        jsonb not null default '{}'::jsonb,              -- url, platform, external id, seconds, at_s, licence ...
    origin      text not null default 'seed'
                check (origin in ('seed', 'library', 'youtube', 'foreplay', 'agent', 'human')),
    status      text not null default 'active' check (status in ('active', 'draft', 'retired')),
    confidence  real,                                            -- 0..1 where it is known (e.g. from evidence)
    search      tsvector,                                        -- title (A), tags (B), body (C); set by the code that writes
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    unique (kind, slug)
);
create index if not exists kb_items_search_idx on kb_items using gin (search);
create index if not exists kb_items_tags_idx on kb_items using gin (tags);
create index if not exists kb_items_kind_idx on kb_items (kind, status);

create table if not exists kb_links (
    from_id     bigint not null references kb_items(id) on delete cascade,
    to_id       bigint not null references kb_items(id) on delete cascade,
    rel         text not null check (rel in ('part_of', 'taught_by', 'implements', 'uses', 'derived_from', 'example_of',
                                              'related', 'supports', 'contradicts')),
    note        text not null default '',
    created_at  timestamptz not null default now(),
    primary key (from_id, to_id, rel),
    check (from_id <> to_id)
);
create index if not exists kb_links_to_idx on kb_links (to_id, rel);

-- What happened when a technique was used: which job and ad, how it scored, what staff said, how it compared.
create table if not exists kb_evidence (
    id          bigserial primary key,
    item_id     bigint not null references kb_items(id) on delete cascade,
    job_id      uuid references jobs(id) on delete set null,
    ad_k        int,
    kind        text not null check (kind in ('used', 'score', 'feedback', 'compare')),
    value       jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now()
);
create index if not exists kb_evidence_item_idx on kb_evidence (item_id, created_at desc);

-- Small bookkeeping for the code that keeps the hub filled (which seed file it has imported, how far it got).
create table if not exists kb_state (
    key         text primary key,
    value       jsonb not null default '{}'::jsonb,
    updated_at  timestamptz not null default now()
);

-- What the agent wants to learn next (it adds goals itself; staff can add them too) and what became of each.
create table if not exists kb_goals (
    id          bigserial primary key,
    goal        text not null,                                   -- 'kinetic caption styles that work for finance ads'
    kind        text not null default 'tutorial' check (kind in ('tutorial', 'reference', 'ads')),
    queries     text[] not null default '{}',                    -- search phrases built from a fixed tag list
    status      text not null default 'open' check (status in ('open', 'working', 'done', 'failed', 'skipped')),
    reason      text,
    asked_by    text not null default 'agent',                   -- 'agent', 'staff', 'seed'
    attempts    int not null default 0,
    result      jsonb not null default '{}'::jsonb,              -- what was searched, watched and added
    locked_by   text,
    heartbeat_at timestamptz,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create unique index if not exists kb_goals_goal_idx on kb_goals (kind, lower(goal));
create index if not exists kb_goals_queue_idx on kb_goals (status, id);
