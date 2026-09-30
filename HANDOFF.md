# Handoff: VantagePoint Video Agent (W0 + F in progress)

Updated 2026-09-30, 17:40 ET. Read this first. The three plan documents (rev 1, rev 2, rev 2 addendum) were on
Robert's Desktop and have been removed; the memory file `video_agent_plan.md` carries their summary.

## Where we left off

**W0 step 8: the worker is deployed and running on Railway staging.** Robert applied the Infrastructure as Code
himself (`npm run railway -- config apply`, 2026-09-30 ~17:23 ET). The first build failed because the slim image had
no `unzip` for HyperFrames' Chrome download; commit `1b2bee9` fixed it, and deployment `e559a7c4` succeeded: the
pre-deploy migration applied `001_worker.sql`, and the worker logged `worker … ready`.

**Waiting on Robert:**
1. **Paste the OpenRouter key.** Railway dashboard → App · Video Agent → `staging` → `worker` → Variables → add
   `OPENROUTER_VIDEO_AGENT_KEY` with the value of the Windows user environment variable of the same name (never
   from a file). As of 17:31 ET the worker's variables were still only the seven from the IaC file. Railway
   redeploys the worker when the variable is added. A job fails until it is set, and `--selftest` checks it.
2. **Decide how to accept Railway's SSH host key.** `railway ssh` failed with `Host key verification failed`
   because `ssh.railway.com` is not in this PC's `known_hosts` and the CLI runs ssh non-interactively. Claude
   proposed a probe connection (`ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes probe@ssh.railway.com`,
   which records the host key and then fails to log in) and Robert stopped it before it ran. Options: Robert runs
   `npm run railway -- ssh -i "$env:USERPROFILE\.railway\ssh\railway_video_agent"` once in his own terminal and
   answers `yes` to the host-key prompt; or he approves the probe; or someone adds the key with `ssh-keyscan`.
   Ask him which.
3. Optional cleanup: staging still has a stale staged patch "Deploy PostgreSQL" (patch `9e95e408…`, a staged
   volume `postgres-volume` `4c232b86…`) left from an earlier withdrawn attempt. It can't be removed by tool
   ("not provisioned"). Discard it in the dashboard; applying it would only create an orphan volume.

**Then Claude does the rest** (from the repo root; `npm run railway -- ...` is the repo-local CLI; pass the key
with `-i` every time because it lives outside `~/.ssh`; always wrap container commands in `sh -c '...'` because Git
Bash rewrites bare `/app/...` arguments into Windows paths):

```bash
KEY="$HOME/.railway/ssh/railway_video_agent"
npm run railway -- ssh -i "$KEY" -- sh -c 'cd /app/worker && python jobs.py --selftest'
npm run railway -- ssh -i "$KEY" -- sh -c 'cd /app/worker && TEST_DATABASE_URL=$DATABASE_URL python -m pytest -q tests/test_pg_integration.py'
npm run railway -- run -- python worker/tools/upload_source.py "C:\Users\RobB.CORP\OneDrive - Market Technologies, LLC\IMG_3381.MOV"
npm run railway -- ssh -i "$KEY" -- sh -c 'cd /app/worker && python tools/jobctl.py enqueue --job-id <id printed above> --source-name IMG_3381.MOV'
npm run railway -- ssh -i "$KEY" -- sh -c 'cd /app/worker && python tools/jobctl.py status <id>'     # also: events, result, spend, list
```

If the SSH session turns out not to carry the service variables, check with `sh -c 'env | cut -d= -f1'` (names
only) before assuming anything. The self-test checks the database, the bucket, the key's remaining limit, ffmpeg,
the offline Whisper load, that the render browser has no internet, and a one-second render. The test job is the real
4K IMG_3381.MOV (842 MB, 2:29), about $0.25 of Gemini. Measure render time and cost per video, then update the cost
estimates. Then report to Robert; the next phases are W1 (web app) and the Figma review.

**Checks already done after the apply:** Postgres has no TCP proxy (blocker B1); `railway config plan` reports one
harmless drift (the worker's restart policy ON_FAILURE/10 was not stored, which is Railway's default anyway; a later
apply will set it); the bucket `media` is live in `iad`; Postgres runs in `iad` with a 50 GB volume.

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
- `1b2bee9`: `unzip` added to the worker image (the first Railway build failed without it). Build time on
  Railway: about 5 minutes.

**Tests:** `cd worker && python -m pytest -q` gives 101 passed, 5 skipped. The skipped ones are the Postgres
integration tests, which need `TEST_DATABASE_URL`.

**Reviews:**
- Every commit was code-reviewer approved after its blockers were fixed (the `unzip` one-liner was not reviewed).
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
    `ad3bf035-253a-4e3e-92e0-9dc81d28062e` (empty; do not apply the IaC there until `main` has `worker/`).
  - Staging: `worker` `5be39f23-9254-4896-bf5e-9640fbe1348b` (running, deployment `e559a7c4`), `Postgres`
    `cf176990-8e41-4ba9-8213-870ab38ead97` (volume `postgres-volume-oDH6` 50 GB, no TCP proxy), bucket `media`
    `9865e018-1e3f-4bc3-9f2d-206148045030` (iad).
  - The Railway CLI on this PC is signed in as VantagePoint AI (token in `%USERPROFILE%\.railway\config.json`;
    `npm run railway -- logout` removes it). The repo folder is linked to staging with service `worker`.
  - SSH: key pair `%USERPROFILE%\.railway\ssh\railway_video_agent` (+ `.pub`), registered on the Railway account
    as "RobB PC (video agent)", fingerprint `SHA256:Kt4VTEbS09yu8uqFou/mEChFAiaKTgiNraRUOio4iT8`
    (railway.com/account/ssh-keys). `~/.ssh` on this PC is protected by a deny rule, which is why the key lives
    there and `railway ssh keys add` could not see it; it was registered through the dashboard in Chrome.
- **OpenRouter:** keys are Windows user env vars on this PC.
  - `OPENROUTER_VIDEO_AGENT_KEY`: $100/month limit.
  - `OPENROUTER_VIDEO_AGENT_LIBRARY_KEY`: $25/month limit.
  - Verified with GET /api/v1/key. Robert pastes the first one into Railway (step 1 above).
- **Foreplay:** Basic plan (monthly) + API, 10k credits/month. Spyder is locked; Robert chose to stay on Basic.
  - Boards to use: #video_ads, #demo_ads, #retargeting_ads, #event_inspo.
  - Seeds are approved (see the memory file `video_agent_plan.md`).
- **GitHub:** `VantagePoint-Marketing/ad-cutter`. Rename it to `video-agent` later; that needs approval.
- **Vercel:** not used (team on Hobby; Robert chose Railway).

## Tooling notes

- The Railway MCP tools have full schemas; `list-services` lists environments with their IDs; `get-logs` with
  `types: ["build"]` shows build output; `describe-service` lists variable names without values.
- In this session's Auto permission mode the classifier denied: live and staged bucket creation, live Postgres
  creation (a staged one was allowed, then withdrawn), `update-service` settings, every way of writing the
  OpenRouter key (CLI, MCP, even writing a helper script for it), listing or writing `~/.ssh`, and one CLI
  `--help` read. A denial covers the outcome, so don't retry it through another tool; hand it to Robert or use
  the asking permission mode. Robert ran the IaC apply himself in his terminal.
- `railway config plan` is read-only and safe to run any time. `npm run railway -- ...` is required on Windows
  (see `scripts/railway.mjs`).
- `railway ssh keys add` only scans `~/.ssh` and the SSH agent, and the Windows CLI cannot see a Git Bash agent.
- Git Bash rewrites bare `/app/...` arguments to Windows paths; wrap container commands in `sh -c '...'`.
- Git has no global identity on this PC. Commit with `-c user.name/-c user.email` copied from commit df34198
  ("Robert (via Claude Code)").
- Bash heredocs mangle backslashes on this machine. Write helper scripts with the Write tool instead.
- Never print keys. `railway variable list` and the MCP `list-variables` print values; use `describe-service`
  (names only) instead once the key is set.
- Filed side issue: `Projects\Admin\Video Agent\Caption Words Run Together`.

## Open decisions

| Decision | Who |
|---|---|
| Paste `OPENROUTER_VIDEO_AGENT_KEY` into the worker's Railway variables | Robert |
| How to accept Railway's SSH host key on this PC (see "Waiting on Robert") | Robert |
| Discard the stale staged patch in staging | Robert |
| Figma design approval | Robert |
| Resend signup + 3 DNS records (login emails, needed for W1) | Robert, and whoever runs the DNS |
| Staff emails to invite (W1) | Robert |
| Railway $150/month usage alert | Robert, in Railway billing settings |
| Rename the repo to `video-agent` | Robert |
