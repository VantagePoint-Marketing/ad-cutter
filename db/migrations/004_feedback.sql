-- Video agent: staff feedback on finished ads (step B of the library-and-loop plan approved by Robert on 2026-10-01).
-- One row per click on "Good" or "Not right"; the latest row for a job's ad wins. The worker reads the newest rows
-- as examples for the next plan. Applied by worker/migrate.py as the worker's pre-deploy step.

create table if not exists ad_feedback (
    id          bigserial primary key,
    job_id      uuid not null references jobs(id) on delete cascade,
    ad_k        int not null check (ad_k between 1 and 20),       -- the ad's number within the job
    verdict     text not null check (verdict in ('good', 'bad')),
    note        text not null default '' check (char_length(note) <= 1000),
    created_at  timestamptz not null default now()
);
create index if not exists ad_feedback_job_idx on ad_feedback (job_id, ad_k, created_at desc, id desc);
