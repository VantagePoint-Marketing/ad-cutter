# Handoff: VantagePoint Video Agent (W0 + F in progress)

Updated 2026-09-30, evening. Read this first. The three plan documents (rev 1, rev 2, rev 2 addendum) were on
Robert's Desktop and have been removed; the memory file `video_agent_plan.md` carries their summary.

## Where we left off

**W0 step 8: deploying the worker to Railway staging.** Everything is prepared in the repo. Two things still need
Robert, because the Claude Code permission classifier in this session blocks Railway infrastructure writes and
secret writes (and refuses retries):

1. **Apply the staging infrastructure.** From the repo root on this PC, where the Railway CLI is already signed in
   as VantagePoint AI and the folder is linked to project "App · Video Agent", environment `staging`:

   ```bash
   npm run railway -- config apply
   ```

   It prints the plan and asks for a yes. The plan on 2026-09-30 was: **2 to add** (database Postgres, bucket media),
   **10 to change** (worker source = GitHub `VantagePoint-Marketing/ad-cutter` branch `w0-worker-cloud`, Dockerfile
   `worker/Dockerfile`, pre-deploy `python migrate.py`, start `python jobs.py`, restart on failure, and the
   variables DATABASE_URL, BUCKET, ENDPOINT, REGION, ACCESS_KEY_ID, SECRET_ACCESS_KEY, MONTHLY_BUDGET_USD=90),
   **0 to destroy**. Connecting the source starts the first build (10 to 20 minutes: Whisper model and Chrome are
   baked into the image).

2. **Paste the OpenRouter key.** Railway dashboard → App · Video Agent → `staging` → `worker` → Variables → add
   `OPENROUTER_VIDEO_AGENT_KEY` with the value of the Windows user environment variable of the same name (never
   from a file). Railway redeploys the worker. The worker starts without the key, but a job would fail until it is
   set, and `--selftest` checks it.

Alternatively, switch this session's permission mode from Auto to the one that asks, then say "continue": Claude
runs both steps and Robert approves each prompt.

The session of 2026-09-30 ended here: Robert was given these two steps (also saved on his Desktop as
`Video Agent - W0 staging handoff (2026-09-30).md`) and has not answered yet. Commit `053f487` on
`w0-worker-cloud` holds everything described below and is pushed.

**Right after the apply, check:** `npm run railway -- config plan` reports no changes (if it proposes to move
the database or the worker to another region, stop and ask); the MCP `list-tcp-proxies` on the Postgres service
returns none (blocker B1); the first build succeeds (`list-deployments`, `get-logs` with `types: ["build"]`).

**Then Claude does the rest** (all from the repo root; `npm run railway -- ...` is the repo-local CLI). Always
wrap container commands in `sh -c '...'`: Git Bash rewrites bare `/app/...` arguments into Windows paths. The first
`railway ssh` registers an SSH key on the Railway account (it asks; say yes).

```bash
npm run railway -- ssh -- sh -c 'cd /app/worker && python jobs.py --selftest'
npm run railway -- ssh -- sh -c 'cd /app/worker && TEST_DATABASE_URL=$DATABASE_URL python -m pytest -q tests/test_pg_integration.py'
npm run railway -- run -- python worker/tools/upload_source.py "C:\Users\RobB.CORP\OneDrive - Market Technologies, LLC\IMG_3381.MOV"
npm run railway -- ssh -- sh -c 'cd /app/worker && python tools/jobctl.py enqueue --job-id <id printed above> --source-name IMG_3381.MOV'
npm run railway -- ssh -- sh -c 'cd /app/worker && python tools/jobctl.py status <id>'     # also: events, result, spend, list
```

If the SSH session turns out not to carry the service variables, check with `sh -c 'env | cut -d= -f1'` (names
only) before assuming anything. The self-test checks the database, the bucket, the key's remaining limit, ffmpeg,
the offline Whisper load, that the render browser has no internet, and a one-second render. The test job is the real
4K IMG_3381.MOV (842 MB, 2:29), about $0.25 of Gemini. Measure render time and cost per video, then update the cost
estimates. Then report to Robert; the next phases are W1 (web app) and the Figma review.

## What is built

**Branch `w0-worker-cloud`** (pushed; main untouched):
- `0810049`: pipeline moved to `worker/`. Fonts and GSAP vendored in `worker/template/vendor/` (SHA-256s in
  `SOURCES.md`), so renders are offline.
- `e8c0649`: `safe_media.py`.
  - Magic-byte sniff, forced demuxer, `-protocol_whitelist file`, `-enable_drefs 0`.
  - Video+audio required, 1 s to 10 min, ≤4K on every track.
  - `clean_env()` gives all subprocesses an environment with no keys.
- `9d4d768`: `llm.py`, `budget.py`, `net.py`.
  - Key only from env `OPENROUTER_VIDEO_AGENT_KEY`; on Windows the HKCU registry is the fallback. Never from a file.
  - OpenRouter key limit checked first; a key with **no limit is refused**.
  - Reserve estimate×attempts; settle in `finally`; 5xx, timeout and cut-off replies count at worst case.
  - https allowlist; YouTube media hosts always blocked; redirects never followed.
  - `ad_cutter.run_pipeline()`.
- `179d5ee`: the cloud worker.
  - `jobs.py`: Postgres queue with SKIP LOCKED, heartbeats, stale requeue, 3 attempts, `Stop(BaseException)` on
    SIGTERM, owner-guarded finish, prctl dumpable=0, `--selftest`, `--once`.
  - `pg_budget.py`: row-locked ledger; `release_stale` runs hourly.
  - `storage.py`, `migrate.py`, `db/migrations/001_worker.sql`.
  - `Dockerfile`: digest-pinned python:3.12-slim-bookworm + node:22-bookworm-slim; pinned hyperframes 0.8.92 with
    its Chrome behind `docker/chrome-offline.sh`; Whisper baked in and read-only; non-root; `PYTHONNOUSERSITE=1`.
- `053f487`: Railway **Infrastructure as Code** and staging test tools.
  - `.railway/railway.ts` declares Postgres, the `media` bucket (region `iad`, immutable) and the worker service
    with its build, deploy and variable references; `OPENROUTER_VIDEO_AGENT_KEY` is `preserve()`.
    Railway's `railway.json` config-as-code is deprecated and **new services can't use it**, so
    `worker/railway.json` was removed.
  - `package.json` pins the CLI (`@railway/cli`) and the IaC SDK (`railway`) as dev tools; `scripts/railway.mjs`
    runs the CLI with its binary on the PATH (the SDK's version check needs that on Windows).
  - `worker/tools/upload_source.py` (PC side, through `railway run`) and `worker/tools/jobctl.py` (container side,
    through `railway ssh`): upload a video as `uploads/<job id>/source`, enqueue, and inspect jobs and spend.

**Tests:** `cd worker && python -m pytest -q` gives 101 passed, 5 skipped. The skipped ones are the Postgres
integration tests, which need `TEST_DATABASE_URL`.

**Reviews:**
- Every commit was code-reviewer approved after its blockers were fixed.
- The architect reviewed keys, spend and security: 5 blockers fixed, spend control rated sound.
- W1 follow-ups:
  - `source_key` must equal `Bucket.upload_key(job_id)`; a rebuild's parent must have the same owner.
  - Least-privilege DB roles.
  - killpg on render timeout; rlimits.
  - Save raw_plan right after Gemini.
  - ETag IfMatch on download.
  - pip `--require-hashes`; pytest out of the image.
  - Call `node_modules/.bin/hyperframes`, not npx.
  - Quota caps on the YouTube/Foreplay keys.

**Local CLI still works:** drag a video onto `Cut ads.cmd`. It uses the `video-agent` key and a local ledger at
`%LOCALAPPDATA%\ad-cutter\spend-ledger.jsonl`.

## F: Figma design (draft, awaiting Robert's review)

https://www.figma.com/design/3lDijX1zUtREW6VPKnhI9C, in Robert's **personal** team. Move it to a company Figma
account later (open item).
- Pages: Cover, Foundations, Components, Screens.
- Screens:
  - desktop 1440: sign in ×3, New video, Videos, Review, Playbook, References, Admin;
  - phone 390: sign in ×3, New video, Videos, Review.
- Robert should comment in Figma, then say "design approved" or "apply my Figma comments".

## Outside systems

- **Railway:** workspace "Marketing Department" (`ffa79be0-2c63-498a-8045-fd615dfa5ffc`), project
  **"App · Video Agent"** `d30013fd-1056-4a21-9a8d-730f81cd3390`.
  - Environments: `staging` `9c59f900-b58f-4bd6-89cc-1eb961834e09`, `production`
    `ad3bf035-253a-4e3e-92e0-9dc81d28062e` (empty).
  - Staging has one empty service, `worker` `5be39f23-9254-4896-bf5e-9640fbe1348b`, nothing deployed. The IaC
    apply fills in the rest.
  - The Railway CLI on this PC is signed in as VantagePoint AI (token in `%USERPROFILE%\.railway\config.json`;
    `npm run railway -- logout` removes it). The repo folder is linked to staging with service `worker`.
- **OpenRouter:** keys are Windows user env vars on this PC.
  - `OPENROUTER_VIDEO_AGENT_KEY`: $100/month limit.
  - `OPENROUTER_VIDEO_AGENT_LIBRARY_KEY`: $25/month limit.
  - Verified with GET /api/v1/key. Robert pastes the first one into Railway (step 2 above).
- **Foreplay:** Basic plan (monthly) + API, 10k credits/month. Spyder is locked; Robert chose to stay on Basic.
  - Boards to use: #video_ads, #demo_ads, #retargeting_ads, #event_inspo.
  - Seeds are approved (see the memory file `video_agent_plan.md`).
- **GitHub:** `VantagePoint-Marketing/ad-cutter`. Rename it to `video-agent` later; that needs approval.
- **Vercel:** not used (team on Hobby; Robert chose Railway).

## Tooling notes

- The Railway MCP tools now have full schemas; `list-services` lists environments with their IDs.
- In this session's Auto permission mode the classifier denied: live and staged bucket creation, live Postgres
  creation (a staged one was allowed, then withdrawn), `update-service` settings, and every way of writing the
  OpenRouter key (CLI, MCP, even writing a helper script for it). A denial covers the outcome, so don't retry it
  through another tool; hand it to Robert or use the asking permission mode.
- `railway config plan` is read-only and safe to run any time.
- Git has no global identity on this PC. Commit with `-c user.name/-c user.email` copied from commit df34198
  ("Robert (via Claude Code)").
- Bash heredocs mangle backslashes on this machine. Write helper scripts with the Write tool instead.
- Never print keys. `railway variable list` and the MCP `list-variables` print values; use `describe-service`
  (names only) instead once the key is set.
- Filed side issue: `Projects\Admin\Video Agent\Caption Words Run Together`.

## Open decisions

| Decision | Who |
|---|---|
| Run `npm run railway -- config apply` for staging (or switch the session to asking mode) | Robert |
| Paste `OPENROUTER_VIDEO_AGENT_KEY` into the worker's Railway variables | Robert |
| Figma design approval | Robert |
| Resend signup + 3 DNS records (login emails, needed for W1) | Robert, and whoever runs the DNS |
| Staff emails to invite (W1) | Robert |
| Railway $150/month usage alert | Robert, in Railway billing settings |
| Rename the repo to `video-agent` | Robert |
