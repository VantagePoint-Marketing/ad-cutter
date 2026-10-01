# Handoff: VantagePoint Video Agent (W0 + F in progress)

Updated 2026-10-01, 09:55 ET. Read this first. The three plan documents (rev 1, rev 2, rev 2 addendum) were on
Robert's Desktop and have been removed; the memory file `video_agent_plan.md` carries their summary.

## Where we left off

**W0 step 8 is done: on 2026-10-01 the first real job ran end to end on Railway staging and produced three ads.**
Job `d0cafd17-4d25-4a07-a430-ebb411fef6a7` (IMG_3381.MOV, 4K, 842 MB, 2:29). Its first attempt failed at the
render step (ffmpeg 5.1 channel-layout negotiation, fixed in `a647d5a`, see "What is built"); Robert requeued it
himself because the auto-mode permission classifier denies job starts (they spend Gemini money).

Measured on the second run (worker started 13:34:09Z, ready 13:46:18Z, **12 min 9 s** in total):

| Stage | Time | Notes |
|---|---|---|
| download from the bucket | 4 s | |
| preparing (1080x1920 working copy, 16 kHz wav, Gemini proxy) | 2 min 0 s | 4K source |
| transcribing (Whisper medium.en on CPU) | 55 s | 424 words |
| planning (Gemini 3.1 Pro watches the proxy) | 1 min 21 s | $0.1025; worst-case reservation $0.87 |
| rendering 3 ads (cut, captions, HyperFrames render, verify) | 7 min 47 s | 2:40, 2:44 and 2:23 per ad |
| uploading | 2 s | |

**Cost per source video: about $0.10 of Gemini** (the failed first run also paid $0.0976 for its plan) plus a few
cents of Railway compute. Ledger: $0.20 spent in 2026-10 against the $90 cap. Output: three 1080x1920 ads of
33 s, 41.5 s and 31 s, 64 to 88 MB each (crf 16), caption/speech match 0.89 to 0.98, layout checks passed, two
claims flagged for compliance review. Files: bucket `results/<job id>/` and a copy on Robert's Desktop in
`Video Agent test ads 2026-10-01/` (3 MP4s + Review Notes.md, 217 MB).

**Next:** Robert watches the three ads and reads Review Notes.md. Nothing is waiting on Claude. Then W1 (web app,
after the Figma review). Staging test commands, from the repo root (`npm run railway -- ...` is the repo-local CLI;
pass the key with `-i` every time; wrap container commands in `sh -c '...'`):

```bash
KEY="$HOME/.railway/ssh/railway_video_agent"
npm run railway -- ssh -i "$KEY" -- sh -c 'cd /app/worker && python jobs.py --selftest'
npm run railway -- run -s worker -e staging -- python worker/tools/upload_source.py "<path to video>"   # prints the job id
npm run railway -- ssh -i "$KEY" -- sh -c 'cd /app/worker && python tools/jobctl.py enqueue --job-id <id> --source-name <file name>'   # job starts: Robert runs them
npm run railway -- ssh -i "$KEY" -- sh -c 'cd /app/worker && python tools/jobctl.py status <id>'   # also: events, result, requeue, spend, list
```

A job goes queued -> working (preparing, transcribing, planning, rendering, uploading) -> ready or failed.
In Robert's PowerShell, `npm` fails ("running scripts is disabled"); he must type `npm.cmd`.

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
    **ffmpeg comes from Debian bookworm: 5.1.9.** Keep filter graphs compatible with it (or pin a newer ffmpeg on
    purpose); the PC's 8.1.2 is more forgiving, so a PC test does not prove a chain works in the container.
- `053f487`: Railway **Infrastructure as Code** and staging test tools.
  - `.railway/railway.ts` declares Postgres, the `media` bucket (region `iad`, immutable) and the worker service
    with its build, deploy and variable references; `OPENROUTER_VIDEO_AGENT_KEY` is `preserve()`.
    Railway's `railway.json` config-as-code is deprecated and **new services can't use it**, so
    `worker/railway.json` was removed.
  - `package.json` pins the CLI (`@railway/cli`) and the IaC SDK (`railway`) as dev tools; `scripts/railway.mjs`
    runs the CLI with its binary on the PATH (the SDK's version check needs that on Windows).
  - `worker/tools/upload_source.py` (PC side, through `railway run`) and `worker/tools/jobctl.py` (container side,
    through `railway ssh`): upload a video as `uploads/<job id>/source`, enqueue, requeue, and inspect jobs and spend.
- `1b2bee9`: `unzip` added to the worker image (the first Railway build failed without it). Build time on
  Railway: 3 to 5 minutes.
- `a647d5a`: the ffmpeg 5.1 render fix, the `ad body chain` self-test check, and `jobctl.py requeue` (see above).

**Tests:** `cd worker && python -m pytest -q` gives 101 passed, 5 skipped. The skipped ones are the Postgres
integration tests, which need `TEST_DATABASE_URL`.

**Reviews:**
- Every commit was code-reviewer approved after its blockers were fixed (the `unzip` one-liner was not reviewed).
- The architect reviewed keys, spend and security: 5 blockers fixed, spend control rated sound.
- W1 follow-ups:
  - `source_key` must equal `Bucket.upload_key(job_id)`; a rebuild's parent must have the same owner.
  - Least-privilege DB roles.
  - killpg on render timeout; rlimits. `render_body` has no timeout at all (a hung ffmpeg hangs `--selftest`).
  - Save raw_plan right after Gemini.
  - ETag IfMatch on download.
  - pip `--require-hashes`; pytest out of the image.
  - Call `node_modules/.bin/hyperframes`, not npx.
  - Quota caps on the YouTube/Foreplay keys.
  - `requeue`: check the source is still in the bucket (`head_object`, as `enqueue` does); add a Postgres
    integration test (failed -> queued with the event, miss on a queued job, unknown id).
  - Review Notes.md is titled "Ad cuts from source.bin": the worker downloads the source as `source.bin`, so the notes
    should use the job's `source_name` (IMG_3381.MOV) instead.
  - Output ads are 64 to 88 MB for 30 to 40 s (crf 16). Fine for Meta, but consider crf 18 to 20 for faster uploads.
  - Self-test could use 540x960 clips (4× less encode time; only the audio negotiation matters).
  - The worker logs to stderr, so Railway tags every line, even INFO, as severity "error". Log to stdout.
  - Whisper load prints "Ignoring corrupted tree cache file ... Permission denied" (`/app/hf-cache/.../trees/*.json`
    is unreadable for the non-root user). Harmless, but make the file readable or silence it.

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
  - Staging: `worker` `5be39f23-9254-4896-bf5e-9640fbe1348b` (running commit `a647d5a`, deployment `16cb75e8`),
    `Postgres` `cf176990-8e41-4ba9-8213-870ab38ead97` (volume `postgres-volume-oDH6` 50 GB, no TCP proxy), bucket
    `media` `9865e018-1e3f-4bc3-9f2d-206148045030` (iad).
  - **Orphan volume:** the stale staged patch "Deploy PostgreSQL" got applied on 2026-10-01 (probably when Robert
    clicked Deploy after adding the key). It created `postgres-volume` `4c232b86-e16d-4b38-9e0b-705cec3dde7f`,
    50 GB, attached to nothing. Postgres itself is fine on its own volume. Deleting the orphan is Robert's call
    (a few dollars a month otherwise).
  - The worker's variables: the seven from the IaC plus `OPENROUTER_VIDEO_AGENT_KEY`. **The key in Railway is not
    the PC's `OPENROUTER_VIDEO_AGENT_KEY`**: it is a key with a $150 total limit (not monthly; $3.23 already used
    before this project), expiring 2026-12-21. Robert confirmed it is the one he wants ("the railway key is right").
    The worker's own ledger cap is `MONTHLY_BUDGET_USD` = 90. To compare keys without printing them, hash them:
    PC side in PowerShell with `[Environment]::GetEnvironmentVariable(...)` + SHA-256; container side with
    `printf %s "$OPENROUTER_VIDEO_AGENT_KEY" | sha256sum | cut -c1-8`.
  - The Railway CLI on this PC is signed in as VantagePoint AI (token in `%USERPROFILE%\.railway\config.json`;
    `npm run railway -- logout` removes it). The repo folder is linked to staging with service `worker`.
  - SSH: key pair `%USERPROFILE%\.railway\ssh\railway_video_agent` (+ `.pub`), registered on the Railway account
    as "RobB PC (video agent)", fingerprint `SHA256:Kt4VTEbS09yu8uqFou/mEChFAiaKTgiNraRUOio4iT8`
    (railway.com/account/ssh-keys). `~/.ssh` on this PC is protected by a deny rule, which is why the key lives
    there. The host key of `ssh.railway.com` is now in `known_hosts` (Robert accepted it on 2026-10-01), so
    `railway ssh` works from Claude's Bash tool.
- **OpenRouter:** keys are Windows user env vars on this PC.
  - `OPENROUTER_VIDEO_AGENT_KEY`: $100/month limit (not the one in Railway, see above).
  - `OPENROUTER_VIDEO_AGENT_LIBRARY_KEY`: $25/month limit.
  - Verified with GET /api/v1/key.
- **Foreplay:** Basic plan (monthly) + API, 10k credits/month. Spyder is locked; Robert chose to stay on Basic.
  - Boards to use: #video_ads, #demo_ads, #retargeting_ads, #event_inspo.
  - Seeds are approved (see the memory file `video_agent_plan.md`).
- **GitHub:** `VantagePoint-Marketing/ad-cutter`. Rename it to `video-agent` later; that needs approval.
- **Vercel:** not used (team on Hobby; Robert chose Railway).
- **Test video:** `IMG_3381.MOV` now lives at `OneDrive - Market Technologies, LLC\Obsidian\01_VantagePoint\
  90_Source_Library\Call_Recordings_and_Transcripts\Meeting_Transcripts\Team_Meeting_Audio\IMG_3381.MOV`
  (842,429,761 bytes, pinned locally). It is already in the bucket as `uploads/d0cafd17-.../source`.

## Tooling notes

- The Railway MCP tools have full schemas; `list-services` lists environments with their IDs; `get-logs` with
  `types: ["build"]` shows build output; `describe-service` lists variable names without values;
  `list-deployments` shows whether a push started a build.
- Auto permission mode (classifier) denials so far: live and staged bucket creation, live Postgres creation,
  `update-service` settings, every way of writing the OpenRouter key, listing or writing `~/.ssh`, one CLI `--help`
  read, and **requeueing a job** (spends Gemini money; the first `enqueue` was allowed). A denial covers the
  outcome, so don't retry it through another tool; hand it to Robert or use the asking permission mode.
- `railway config plan` is read-only and safe to run any time. `npm run railway -- ...` is required on Windows
  (see `scripts/railway.mjs`). In Robert's own PowerShell it must be `npm.cmd`.
- `railway ssh keys add` only scans `~/.ssh` and the SSH agent, and the Windows CLI cannot see a Git Bash agent.
- Git Bash rewrites bare `/app/...` arguments to Windows paths; wrap container commands in `sh -c '...'` and set
  `MSYS_NO_PATHCONV=1` when the script text itself contains `/tmp/...` paths. Piping a script into
  `railway ssh -- sh -s` (or `python -`) works and avoids quoting problems.
- `railway run -s worker -e staging -- python worker/tools/upload_source.py <path>` uploads at roughly 3 MB/s
  (842 MB took about 5 minutes).
- Git has no global identity on this PC. Commit with `-c user.name="Robert (via Claude Code)"
  -c user.email=marketing@vantagepointsoftware.com`.
- Bash heredocs mangle backslashes on this machine. Write files that contain backslashes with the Write tool.
- Never print keys. `railway variable list` and the MCP `list-variables` print values; use `describe-service`
  (names only) instead.
- Filed side issue: `Projects\Admin\Video Agent\Caption Words Run Together`.

## Open decisions

| Decision | Who |
|---|---|
| Delete the orphan volume `postgres-volume` (`4c232b86...`) in staging | Robert |
| Keep the $150 key in Railway or swap in the PC's $100/month key | Robert (he said keep it) |
| Figma design approval | Robert |
| Resend signup + 3 DNS records (login emails, needed for W1) | Robert, and whoever runs the DNS |
| Staff emails to invite (W1) | Robert |
| Railway $150/month usage alert | Robert, in Railway billing settings |
| Rename the repo to `video-agent` | Robert |
