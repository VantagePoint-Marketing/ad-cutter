# Handoff: VantagePoint Video Agent (W1 simplified: link-only page, deployed to staging)

Updated 2026-10-01, late afternoon ET. Read this first. The memory file `video_agent_plan.md` carries the plan
summary.

## Where we left off

**Robert changed the W1 scope on 2026-10-01:** no sign-in, no accounts, no job management. One URL that only a few
people have. The only human input is uploading clips and describing what they want; Gemini decides everything else.
"It shouldn't be this complicated and high security." That is built, tested, reviewed, pushed on branch
`w0-worker-cloud` and **deployed to Railway staging** (Robert: "push it and apply the railway config"):

- Worker: commit `c407ced` live, migration `002_web.sql` applied (checked: `schema_migrations` has 001 and 002,
  `jobs_status_check` includes 'uploading', `sources` is jsonb default `[]`), `--selftest` 9 of 9 OK in the
  container, including the new `working copy chain` (two clips joined on ffmpeg 5.1).
- Web: service `web` `d347c459-3ebf-485e-993b-c07332b0dd79`, address **`https://web-staging-c524.up.railway.app`**
  (Railway picked the name). `/healthz` answers `{"ok": true, "link": ..., "uploads": "allowed from
  https://web-staging-c524.up.railway.app"}`; the bucket's CORS rule is set to that address.

**One step left, and it needs Robert** (Claude's attempt was blocked by the auto-mode rule against writing secrets):
Railway dashboard → App · Video Agent → staging → **web** → Variables → add `APP_LINK_TOKEN` = any 16+ letters,
digits, `-` or `_` (the one Claude generated is in the chat, or make a fresh one). Railway redeploys the web service
when the variable is saved; `/healthz` then shows `"link": "set"`. The team link is
`https://web-staging-c524.up.railway.app/<token>/`.

**Then the first real test:** open the link, drop one or more clips (10 minutes of footage per job at most, 4 GB
per clip), type what you want, click **Make ads**. Expect 10 to 20 minutes. If a job fails, the page shows the
plain-English reason; `jobctl.py events <id>` in the container has the steps:

```bash
KEY="$HOME/.railway/ssh/railway_video_agent"
npm run railway -- ssh -i "$KEY" -- sh -c 'cd /app/worker && python jobs.py --selftest'
npm run railway -- ssh -i "$KEY" -- sh -c 'cd /app/worker && python tools/jobctl.py list'     # also: status, events, result, requeue, spend
```

In Robert's PowerShell, `npm` fails ("running scripts is disabled"); he must type `npm.cmd`.

**Reviews of this change (2026-10-01):** the architect found one blocker, fixed in the follow-up commit: Gemini's ad
names with an apostrophe or ampersand ("Don't Trade Blind") passed `safe_name` but failed `Bucket.result_key` at
upload time, after all the renders, and the generic retry then paid for two more Gemini plans; `safe_name` now
keeps only the characters result keys accept. Also added from that review: each input is capped at its own checked
length (a lying header can't stretch a job past 10 minutes), a cap of 30 web jobs per rolling day (429), the prompt
says the request never overrides the rules, and upload links last 6 hours. The code-reviewer approved with no
blockers and six should-fixes, all applied in the third commit: the page's `[hidden]` attribute lost to
`.row {display:flex}` (the "Start another" button showed during uploads), two polling loops could run at once
(hashchange plus a direct call; now a generation counter), a 10 GB total cap per job on the page and in the
worker, a replan/rebuild may read its parent's clips, uvicorn's access log is off (every path carries the token)
and `/healthz` names only an error's type, and `validate_plan` notes a segment that runs across a clip join.
Also verified on staging after that: the page's exact SQL against the real Postgres (in a rolled-back
transaction), and the browser's CORS preflight to the bucket (200, the page's origin, PUT).

## What the page does (web/)

- `web/app.py` (FastAPI, uvicorn, same Postgres and bucket as the worker). Every route lives under
  `/<APP_LINK_TOKEN>/`; anything else is 404; `/healthz` is open. No cookies, no accounts.
- Flow: `POST api/jobs` (brief + clip names and sizes) creates a job in status `uploading` and returns one signed
  PUT link per clip (1 hour, one key each: `uploads/<job>/clip-<n>`). The browser PUTs each clip straight into the
  bucket (XHR, with progress). `POST api/jobs/<id>/start` checks every clip's size in the bucket equals the declared
  size, then flips the job to `queued`. The page polls `GET api/jobs/<id>` every 4 s; when ready it shows each ad
  (inline preview + download through signed GET links, 1 hour), the headline, callouts, primary text, Gemini's
  "how I read the request", claims to review, and the review notes file. `GET api/jobs` lists the last 30 jobs
  (Recent, clickable). Deep link: `#<job id>` in the URL.
- At start-up the app sets the bucket's CORS rule (PUT from its own `RAILWAY_PUBLIC_DOMAIN`). Verified against
  the real staging bucket on 2026-10-01: CORS set, signed PUT 200, signed GET 200 with Content-Disposition.
- `web/static/index.html` is the whole front end (vanilla JS, Apple-ish minimal). The Figma design can replace it
  later; the API stays.
- Limits: 1 to 10 clips, 4 GB per clip, 2,000 characters of request, 10 minutes of footage per job (the worker
  enforces the last one; the page estimates it from the files' metadata and warns), 30 jobs per rolling day.
- Image: `web/Dockerfile` (python slim, same digest as the worker, non-root, read-only files, no video tools);
  it copies `worker/storage.py`, which both services share. Health check `/healthz`.
- Tests: `cd web && python -m pytest -q` (12 pass; fake DB and bucket).

## What changed in the worker (worker/)

- **Several clips per job.** `jobs.sources` (jsonb, migration 002) lists `{key, name, bytes}` in order; old rows with
  only `source_key` still work. The worker refuses any key `Bucket.owns(job_id, key)` rejects, so a job can only
  read its own uploads. `ad_cutter.prepare()` joins all clips in one ffmpeg pass (`working_copy_args`: per-input
  scale/crop/`setsar`/`fps` and `aresample`+`aformat` to 48 kHz stereo fltp, then `concat`), so everything after
  it (Whisper, Gemini, cutting, rendering) still sees one video. The prompt gets a clip map (name, time span,
  word indexes) and tells Gemini not to run a segment across a join.
- **The request drives the plan.** `options.brief` (web) or `--brief` (CLI); `options.note` from older CLI rows is
  used the same way. `prompts/plan_ads.md` was rewritten: the request decides how many ads (1 to `ad_count_max`
  = 6, default `ad_count` = 3 when it does not say), length, angle, tone and which parts to use; the format rules
  (sentence boundaries, no invented claims, callout limits, compliance list) stay. New plan field
  `response_to_request` goes to Review Notes ("How Gemini read the request") and the job result. Length sanity
  band in `validate_plan` is now 5 to 120 s (`AD_SECONDS_SANE`) instead of the old fixed 20 to 75 target.
- Review Notes are titled after the clips (`IMG_3381.MOV + 1 more`), not `source.bin`, and show the request.
- `--selftest` gained `working copy chain`; `jobctl.py enqueue --brief`, `requeue` checks the uploads still exist.
- Tests: `cd worker && python -m pytest -q` (116 pass, 5 skipped: the Postgres integration tests need
  `TEST_DATABASE_URL`). The ffmpeg-dependent tests run on the PC (ffmpeg 8); the container's ffmpeg 5.1 is only
  proven by `--selftest` and the first real job.

## What is built (history)

**Branch `w0-worker-cloud`** (main untouched):
- `0810049`: pipeline moved to `worker/`. Fonts and GSAP vendored in `worker/template/vendor/` (SHA-256s in
  `SOURCES.md`), so renders are offline.
- `e8c0649`: `safe_media.py`: magic-byte sniff, forced demuxer, `-protocol_whitelist file`, `-enable_drefs 0`;
  video+audio required, 1 s to 10 min, ≤4K on every track; `clean_env()` gives subprocesses no keys.
- `9d4d768`: `llm.py`, `budget.py`, `net.py`: key only from env `OPENROUTER_VIDEO_AGENT_KEY` (never a file); a key
  with no OpenRouter limit is refused; reserve estimate×attempts, settle in `finally`; https allowlist, YouTube
  media hosts blocked, redirects never followed.
- `179d5ee`: the cloud worker: `jobs.py` (Postgres queue with SKIP LOCKED, heartbeats, stale requeue, 3 attempts,
  `Stop(BaseException)` on SIGTERM, owner-guarded finish, prctl dumpable=0, `--selftest`, `--once`),
  `pg_budget.py` (row-locked ledger), `storage.py`, `migrate.py`, `db/migrations/001_worker.sql`, `Dockerfile`
  (digest-pinned images, pinned hyperframes 0.8.92 with its Chrome behind `docker/chrome-offline.sh`, Whisper baked
  in, non-root). **ffmpeg in the image is Debian bookworm's 5.1.9**; the PC's 8.1.2 is more forgiving.
- `053f487`: Railway Infrastructure as Code (`.railway/railway.ts`; `railway.json` is deprecated) and the staging
  tools `worker/tools/upload_source.py` (PC side) and `worker/tools/jobctl.py` (container side).
- `1b2bee9`: `unzip` in the worker image. `a647d5a`: the ffmpeg 5.1 render fix (`aformat` after `aresample`),
  the `ad body chain` self-test, `jobctl.py requeue`.
- `5263ffd`: W0 done. First real job on staging, 2026-10-01: IMG_3381.MOV (4K, 842 MB, 2:29) → three ads in
  12 min 9 s (prepare 2:00, transcribe 0:55, Gemini plan 1:21 at $0.1025, three renders 7:47). **About $0.10 of
  Gemini per source video** plus a few cents of compute. Copies in `Desktop/Video Agent test ads 2026-10-01/`.
- This commit: W1 simplified (see the two sections above), `web/`, migration 002, docs.

**Reviews:** every commit was code-reviewer approved after its blockers were fixed; the architect reviewed keys,
spend and security for W0 (5 blockers fixed) and the link-only exposure for W1 (verdict: the design holds; without
the token an outsider reaches only `/healthz`; with it a person cannot reach another job's footage or pass the
Gemini caps). Follow-ups still open, none blocking:
- Budget per environment: each database has its own ledger, so production needs its own OpenRouter key (or a
  lower `MONTHLY_BUDGET_USD` on staging) and its own `APP_LINK_TOKEN`.
- Retention: raw uploads and jobs abandoned in 'uploading' are never deleted; decide a policy before production.
- An outsider can POST a large body to a wrong-token URL (FastAPI reads it before the link check): the worst case
  is a web-service restart, no data and no spend. Not worth a body-size middleware now.
- Least-privilege DB roles (the web service uses the owner connection like the worker).
- killpg on render timeout; rlimits. `render_body` has no timeout (a hung ffmpeg hangs `--selftest`).
- Save raw_plan right after Gemini. ETag IfMatch on download. pip `--require-hashes`; pytest out of the worker
  image. Call `node_modules/.bin/hyperframes`, not npx. Quota caps on the YouTube/Foreplay keys.
- Output ads are 64 to 88 MB for 30 to 40 s (crf 16); consider crf 18 to 20. Self-test could use 540x960 clips.
- The worker logs to stderr, so Railway tags every line as "error"; log to stdout (the web service already does).
- Whisper prints "Ignoring corrupted tree cache file ... Permission denied" at load (harmless).
- Jobs abandoned in `uploading` (clips never sent) stay in the table; a daily sweep could delete them and their
  partial uploads.
- A Postgres integration test for `requeue` and for the web API's SQL (the web tests use a fake database that
  mirrors the exact statements; a real-Postgres run on staging would close that gap).

**Local CLI still works:** drag one or more clips onto `Cut ads.cmd` (or `--brief "..."` on the command line). It
uses the `video-agent` key and a local ledger at `%LOCALAPPDATA%\ad-cutter\spend-ledger.jsonl`.

## F: Figma design (draft, superseded in scope)

https://www.figma.com/design/3lDijX1zUtREW6VPKnhI9C, in Robert's **personal** team. It has sign-in screens and
Playbook/References/Admin pages that are no longer in scope; its New video / Videos / Review screens are still the
reference look for the page once Robert wants more than the plain `index.html`. Not blocking anything now.

## Outside systems

- **Railway:** workspace "Marketing Department" (`ffa79be0-2c63-498a-8045-fd615dfa5ffc`), project
  **"App · Video Agent"** `d30013fd-1056-4a21-9a8d-730f81cd3390`.
  - Environments: `staging` `9c59f900-b58f-4bd6-89cc-1eb961834e09`, `production`
    `ad3bf035-253a-4e3e-92e0-9dc81d28062e` (empty; do not apply the IaC there until `main` has `worker/` and `web/`).
  - Staging: `worker` `5be39f23-9254-4896-bf5e-9640fbe1348b` (commit `c407ced`, deployment `bf9e37d0`),
    `web` `d347c459-3ebf-485e-993b-c07332b0dd79` (`https://web-staging-c524.up.railway.app`, domain id
    `0739cc7f-b576-42cf-84ed-031072653c62`), `Postgres` `cf176990-8e41-4ba9-8213-870ab38ead97` (volume
    `postgres-volume-oDH6` 50 GB, no TCP proxy), bucket `media` `9865e018-1e3f-4bc3-9f2d-206148045030` (iad,
    Tigris-backed; the web service sets its CORS rule at every start).
  - `railway config plan` keeps reporting `restartPolicyType/MaxRetries (null → ON_FAILURE/10)` for both services
    even right after an apply; Railway seems not to persist or report those two fields. Harmless; ignore it.
  - **Orphan volume:** `postgres-volume` `4c232b86-e16d-4b38-9e0b-705cec3dde7f`, 50 GB, attached to nothing
    (created by a stale staged patch on 2026-10-01). Deleting it is Robert's call (a few dollars a month otherwise).
  - The worker's variables: the seven from the IaC plus `OPENROUTER_VIDEO_AGENT_KEY`. **The key in Railway is not
    the PC's `OPENROUTER_VIDEO_AGENT_KEY`**: it has a $150 total limit (not monthly; $3.23 used before this
    project), expiring 2026-12-21; Robert confirmed it is the one he wants. The worker's own ledger cap is
    `MONTHLY_BUDGET_USD` = 90. To compare keys without printing them, hash them (PowerShell SHA-256 on the PC;
    `printf %s "$OPENROUTER_VIDEO_AGENT_KEY" | sha256sum | cut -c1-8` in the container).
  - The Railway CLI on this PC is signed in as VantagePoint AI (token in `%USERPROFILE%\.railway\config.json`;
    `npm run railway -- logout` removes it). The repo folder is linked to staging with service `worker`.
  - SSH: key pair `%USERPROFILE%\.railway\ssh\railway_video_agent` (+ `.pub`), registered on the Railway account as
    "RobB PC (video agent)", fingerprint `SHA256:Kt4VTEbS09yu8uqFou/mEChFAiaKTgiNraRUOio4iT8`. `~/.ssh` on this PC
    is protected by a deny rule, which is why the key lives there. `railway ssh` works from Claude's Bash tool.
- **OpenRouter:** keys are Windows user env vars on this PC: `OPENROUTER_VIDEO_AGENT_KEY` ($100/month, not the one
  in Railway) and `OPENROUTER_VIDEO_AGENT_LIBRARY_KEY` ($25/month). Verified with GET /api/v1/key.
- **Foreplay:** Basic plan (monthly) + API, 10k credits/month. Boards: #video_ads, #demo_ads, #retargeting_ads,
  #event_inspo. Seeds approved (memory file `video_agent_plan.md`). Not used by anything built yet.
- **GitHub:** `VantagePoint-Marketing/ad-cutter`. Rename to `video-agent` later; needs approval.
- **Resend / login emails:** no longer needed (no sign-in).
- **Test video:** `IMG_3381.MOV` at `OneDrive - Market Technologies, LLC\Obsidian\01_VantagePoint\
  90_Source_Library\Call_Recordings_and_Transcripts\Meeting_Transcripts\Team_Meeting_Audio\IMG_3381.MOV`
  (842,429,761 bytes). Also in the bucket as `uploads/d0cafd17-4d25-4a07-a430-ebb411fef6a7/source`.

## Tooling notes

- The Railway MCP tools: `list-services` lists environments with IDs; `get-logs` with `types: ["build"]` shows
  build output; `describe-service` lists variable names without values; `list-deployments` shows whether a push
  started a build; `search-docs`/`fetch-docs` read docs.railway.com (that is how the bucket CORS answer was found).
- Railway IaC: `domains: [...]` is refused for `*.up.railway.app` names ("Custom-domain registration is not
  supported"); `networking.serviceDomains` is accepted by `config plan`. `config plan` is read-only and safe.
- Auto permission mode (classifier) denials so far: creating buckets/Postgres live through the MCP tools,
  `update-service`, writing any secret or variable (`set-variables` for `APP_LINK_TOKEN` too), `~/.ssh`, and
  starting or requeueing jobs (they spend Gemini money). A denial covers the outcome; hand it to Robert or use the
  asking permission mode. Allowed: `railway config apply --yes` from Bash once Robert asked for it, `railway run
  -s worker -e staging -- python <script>` (bucket variables injected), and `railway ssh` commands.
- `npm run railway -- ...` is required on Windows (see `scripts/railway.mjs`); in Robert's PowerShell, `npm.cmd`.
- Git Bash rewrites bare `/app/...` arguments; wrap container commands in `sh -c '...'`. Piping a script into
  `railway ssh -- sh -s` works and avoids quoting problems.
- Git has no global identity on this PC. Commit with `-c user.name="Robert (via Claude Code)"
  -c user.email=marketing@vantagepointsoftware.com`.
- Bash heredocs mangle backslashes on this machine; write such files with the Write tool.
- Never print keys. `railway variable list` and the MCP `list-variables` print values; use `describe-service`.
- ruff on this PC runs with a strict rule set (the repo has no ruff config); the baseline has ~37 findings
  (`noqa: E402` comments flagged as unused, `datetime.today()`, etc.). Compare before/after, don't chase them.
- Filed side issue: `Projects\Admin\Video Agent\Caption Words Run Together`.

## Open decisions

| Decision | Who |
|---|---|
| Set `APP_LINK_TOKEN` on the web service (the one step left; see the top) | Robert |
| Delete the orphan volume `postgres-volume` (`4c232b86...`) in staging | Robert |
| Keep the $150 key in Railway or swap in the PC's $100/month key | Robert (he said keep it) |
| Railway $150/month usage alert | Robert, in Railway billing settings |
| Rename the repo to `video-agent`; move the Figma file to a company team | Robert |
| When to merge `w0-worker-cloud` into `main` and apply the IaC to `production` | Robert, after the staging test |
