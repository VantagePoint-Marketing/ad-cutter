# Handoff: VantagePoint Video Agent (W0 + F in progress)

Updated 2026-09-30. Read this first. Plans are on Robert's Desktop:
- `Video Editing Agent - Plan (2026-09-30).md` (rev 1: the learning/playbook design)
- `Video Editing Agent - Plan rev 2 (2026-09-30).md` (web app, security design §4)
- `Video Editing Agent - Plan rev 2 addendum (2026-09-30).md` (**everything on Railway, not Vercel**; Figma in Robert's personal team)

## Where we left off

**W0 step 8: deploying the worker to Railway staging.** Robert approved all four parts:
1. push the branch (**done**);
2. create the Railway project and staging setup (**in progress**);
3. Robert pastes the OpenRouter key into Railway;
4. one real test job with IMG_3381 (≈ $0.25 Gemini).

**Waiting on Robert:** the Railway **staging environment ID**. He created a `staging` environment in the project, but the Railway MCP tools can't list environments. `describe-environment` always returns production; there's no create/list-environments tool and no Railway CLI on this PC. We asked him to paste the browser address while viewing staging; it contains `environmentId=…`.

**Next steps once the ID arrives** (all in the staging environment):
1. Add Postgres (template). **Delete its public TCP proxy** (architect blocker B1).
2. Add a bucket named `media`.
3. Create the worker service from GitHub `VantagePoint-Marketing/ad-cutter`, branch `w0-worker-cloud`:
   - root directory `/`;
   - Dockerfile `worker/Dockerfile`;
   - config file path `worker/railway.json` (the pre-deploy migration runs from there).
4. Worker variables:
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}`, which must be the `.railway.internal` host;
   - the bucket reference variables `BUCKET`, `ACCESS_KEY_ID`, `SECRET_ACCESS_KEY`, `ENDPOINT`, `REGION`;
   - `MONTHLY_BUDGET_USD=90`;
   - **Robert pastes `OPENROUTER_VIDEO_AGENT_KEY` himself.** Never handle the key value.
5. Deploy. Then run `python jobs.py --selftest` in the container. It checks the DB, the bucket, the key limit, the Whisper offline load, a 1-second render, and that the browser is offline. Also run `pytest tests/test_pg_integration.py` with `TEST_DATABASE_URL` set to the staging DB.
6. The real test: upload IMG_3381 to `uploads/<job_id>/source`, insert a `jobs` row, then watch the logs, cost and output. Measure render time and cost per video, then update the cost estimates in the plan.
7. Report to Robert. The next phases are W1 (web app) and the Figma review.

## What is built

**Branch `w0-worker-cloud`** (pushed; main untouched):
- `0810049`: pipeline moved to `worker/`. Fonts and GSAP vendored in `worker/template/vendor/` (SHA-256s in `SOURCES.md`), so renders are offline.
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
  - `jobs.py`: Postgres queue with SKIP LOCKED, heartbeats, stale requeue, 3 attempts, `Stop(BaseException)` on SIGTERM, owner-guarded finish, prctl dumpable=0, `--selftest`, `--once`.
  - `pg_budget.py`: row-locked ledger; `release_stale` runs hourly.
  - `storage.py`, `migrate.py`, `db/migrations/001_worker.sql`.
  - `Dockerfile`:
    - digest-pinned python:3.12-slim-bookworm + node:22-bookworm-slim;
    - pinned hyperframes 0.8.92 with its Chrome behind `docker/chrome-offline.sh` (host-resolver block, dead proxy, no WebRTC UDP);
    - Whisper baked in and read-only; non-root; `PYTHONNOUSERSITE=1`.
  - `railway.json`.

**Tests:** `cd worker && python -m pytest -q` gives 101 passed, 5 skipped. The skipped ones are the Postgres integration tests, which need `TEST_DATABASE_URL`.

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

**Local CLI still works:** drag a video onto `Cut ads.cmd`. It now uses the `video-agent` key and a local ledger at `%LOCALAPPDATA%\ad-cutter\spend-ledger.jsonl`.

## F: Figma design (draft, awaiting Robert's review)

https://www.figma.com/design/3lDijX1zUtREW6VPKnhI9C, in Robert's **personal** team. Move it to a company Figma account later (open item).
- Pages: Cover, Foundations, Components, Screens.
- Screens:
  - desktop 1440: sign in ×3, New video, Videos, Review, Playbook, References, Admin;
  - phone 390: sign in ×3, New video, Videos, Review.
- Robert should comment in Figma, then say "design approved" or "apply my Figma comments".

## Outside systems

- **Railway:** workspace "Marketing Department" (`ffa79be0-2c63-498a-8045-fd615dfa5ffc`), project **"App · Video Agent"** `d30013fd-1056-4a21-9a8d-730f81cd3390`.
  - Environments: `production` `ad3bf035-253a-4e3e-92e0-9dc81d28062e` (empty) and `staging` (ID pending).
  - Nothing is deployed yet.
- **OpenRouter:** keys are Windows user env vars on this PC.
  - `OPENROUTER_VIDEO_AGENT_KEY`: $100/month limit.
  - `OPENROUTER_VIDEO_AGENT_LIBRARY_KEY`: $25/month limit.
  - Verified with GET /api/v1/key. They will be copied into Railway by Robert.
- **Foreplay:** Basic plan (monthly) + API, 10k credits/month. Spyder is locked; Robert chose to stay on Basic.
  - Boards to use: #video_ads, #demo_ads, #retargeting_ads, #event_inspo.
  - Seeds are approved (see the memory file `video_agent_plan.md`).
- **GitHub:** `VantagePoint-Marketing/ad-cutter`. Rename it to `video-agent` later; that needs approval.
- **Vercel:** not used (team on Hobby; Robert chose Railway).

## Tooling notes

- The Railway MCP tool schemas are empty; parameter names are found by trial.
  - `create-project` takes `name`, `workspaceId`, `description`.
  - `railway-agent` takes `message`, `projectId`, `environmentId`.
  - None can create or list environments.
- Git has no global identity on this PC. Commit with `-c user.name/-c user.email` copied from commit df34198 ("Robert (via Claude Code)").
- Bash heredocs mangle backslashes on this machine. Write helper scripts to the scratchpad instead, or edit with the Edit tool.
- Never print keys. The earlier plain-text key file on the Desktop was moved to the Recycle Bin; Robert needs to empty the Recycle Bin.
- Filed side issue: `Projects\Admin\Video Agent\Caption Words Run Together`.

## Open decisions

| Decision | Who |
|---|---|
| Figma design approval | Robert |
| Resend signup + 3 DNS records (login emails, needed for W1) | Robert, and whoever runs the DNS |
| Staff emails to invite (W1) | Robert |
| Railway $150/month usage alert | Robert, in Railway billing settings |
| Rename the repo to `video-agent` | Robert |
