# Handoff: VantagePoint Video Agent (link-only page live on staging; library and loop, step A)

Updated 2026-10-01, evening ET. Read this first. The memory file `video_agent_plan.md` carries the plan summary.
The approved plan for training is on Robert's Desktop: `Video Agent - Library and Loop plan (2026-10-01).md`.

## The agent's own storage (2026-10-02, committed locally, NOT pushed; needs tokens pasted into Railway)

Robert asked for a Supabase/Drive/OneDrive backend so the agent can keep skills, memory, references and assets; he chose Supabase, the existing
project "MyVantagePointAI | Marketing Department" (`wxoeiwkrannpwtdpgdsa`), with a NEW private bucket. (The architect advised a dedicated project for a
financial-software company, about $10/month; Robert chose the shared project, so the gate is hardened instead. Revisit before production relies on it.)
- **Bucket** `video-agent` (private, 50 MB per file, no storage policies, so only the service role can touch it) and one helper function
  `public.video_agent_bucket_bytes()` (security definer, service role only) for the 10 GB cap. Nothing else in that project was changed.
- **Gate** `supabase/functions/video-agent-store/index.ts` (Edge Function v2, `verify_jwt` off, custom auth). Each Railway environment has its OWN token
  (hashes in the source: staging, production). Rules: read anything; write only `<folder>/<own env>/...`; never replace an existing file except under
  `exports/` and `_selftest/`; delete only there; paths at most 6 parts; total cap 10 GB; signed links last 2 minutes. Rotate a token: add the new hash,
  redeploy (Supabase MCP `deploy_edge_function`, verify_jwt false), paste the new token into Railway, remove the old hash, redeploy. A leftover 3-byte probe
  file `memory/staging/probe/a.txt` cannot be deleted by any token (add-only); remove it by hand if wanted.
- **Client** `worker/agent_store.py` (`AgentStore.from_env()`, needs `AGENT_STORE_TOKEN` and `AGENT_STORE_ENV`) through `net.request_bytes`. **Sync**
  `worker/hub_sync.py`: adds `memory/<env>/hub/<UTC time>.jsonl.gz` (what the agent LEARNED) when it changed (at most every 6 h) and rewrites
  `exports/<env>/hub-vault.zip` (Obsidian vault). Restores are insert-only: `HUB_RESTORE_FROM=staging` on production takes in staging's newest snapshot once;
  a hub that lost its data takes its own newest snapshot back before it uploads anything. A restore never touches existing notes, never creates rules or
  skills, forces the origin to the agent's kinds, skips retired notes and refuses snapshots over 50 MB unpacked. The worker logs `sync: self-test: ...`.
- **Verified live**: the real client's selftest passes for both tokens (text, large file via signed link, replace, list, delete); the gate refuses a retired
  token, cross-environment writes, overwrites of history, deletes of memory, bad JSON and 7-part paths.
- **Not done**: assets (B-roll cache, generated clips) and reference files are not written yet; the API is ready for them. No token has been pasted into Railway
  yet, so nothing syncs until `AGENT_STORE_TOKEN` (staging's token on the staging worker, production's on the production worker) and `AGENT_STORE_ENV` are set
  and the code is pushed. Missing before production relies on it: an off-Supabase weekly copy of `memory/`, Supabase usage alerts, tests of the TypeScript gate itself.

## The design engine (2026-10-02, committed locally on `w0-worker-cloud`, NOT pushed; commits 33ce263, 0ec31b8)

Robert asked how to get better output from HyperFrames and said "yes to everything". The cause of the templated look was that Gemini
decided only the words; the look was hard-coded. Now `worker/design_kit.py` holds a closed kit and Gemini writes a per-ad design brief:
- **Kit**: 8 fonts (SIL OFL, files in `template/vendor`, licences recorded in `vendor/SOURCES.md`), 3 motions, 5 caption styles, 4 headline
  styles, 2 callout styles, 3 end screens, 5 card kinds (stat, quote, lower_third, compare, kinetic), a 3-colour palette (hex, checked for legibility).
  Cards are tied to spoken word ranges and every word on a card must have been said (plain characters only).
- **Brief**: each ad's `design` = observations of the footage, choices, and a reason; no observation or reason = the original look. Invalid
  single options fall back to the original value with a note. Ads in one batch that look alike get their caption style varied (`diversify`).
  The prompt (`plan_ads.md`) is built from the same tables the code checks against (`options_text`), plus the last 20 looks (`history_text`,
  read from `jobs.result->'designs'`, SQL `RECENT_DESIGNS_SQL`, proven by `jobs.py --selftest`) and design/reference hints from the hub.
- **Original look is byte-identical**: `tests/fixtures/classic_compose.html` is the baseline from before the change; `compose()` with no design
  must reproduce it. HyperFrames' static check needs inline `@font-face` for fonts it does not know, so non-Montserrat fonts get inline rules.
- **Self-check** now has an eighth score, `design` (a design of 2 or less with a named problem flags the ad; the old average still uses the
  original seven). The scorecard also shows "Next to ads that are working".
- **Verified live (2026-10-02, about $0.12)**: one real 149 s clip, brief asking for three different designed ads: three clearly different looks
  rendered and passed the HyperFrames check (copies and a contact sheet are in `Desktop/Ad Cutter design test ads 2026-10-02/`). Gemini kept the
  original look's caption style for ad 1 but with its own colours and cards; ad 2 used a calm clean look; ad 3 a hype Anton look with a band headline.
- **Not done yet** (from the roadmap): the loop (L0 plumbing, L1 repair rounds, "Change this ad"), planner tools, a measured golden set, subject-aware
  framing, ad-media watching, and Gemini Omni for generated shots (needs a Google AI Studio key; API cannot yet restyle a full clip).

## The knowledge hub and the self-teaching agent (2026-10-02, committed locally on `w0-worker-cloud`, NOT pushed)

What exists (commits 48f036f, f59429c, 386ceb3, 4a03e36, then "Hub part 5"):
- **Hub** (`worker/hub.py`, migration `db/migrations/005_hub.sql`): one Postgres graph of notes (source, lesson, technique, recipe, skill,
  rule, example) with links and evidence, plus the goal queue `kb_goals` (kinds tutorial, reference, ads). All hub SQL is in `hub.py`.
  `hub_import.Maintainer` seeds it (techniques, ~skill notes from `hub_seed/`, the library's lessons) a little at a time; `hub_export.py`
  makes the Obsidian vault zip.
- **Planner** is told what the hub knows (`hub_context.fetch`, bounded, hints only). **Self-check** compares each finished ad with a few
  reference examples (`hub_context.fetch_references`; the scorecard shows "Next to ads that are working"), and each comparison is
  written to `kb_evidence` (`jobs.record_comparisons`).
- **Learner** (`worker/learner.py`, only while no job waits, `LEARNING_ENABLED=1`): tutorial and reference goals search YouTube and watch
  public videos on Gemini's free tier; **ads goals** use Foreplay (`worker/foreplay.py`): one page of `longest_running`, live,
  English, 10 to 75 s video ads per step (1 credit per ad), a cheap OpenRouter model (Flash-Lite, zero retention, spend ledger) describes
  each ad from its words and metadata (`prompts/analyse_ads.md`, `hub_ads.py`), filed as `example` notes `ref-fp-<id>` with running days.
  Limits (config `learn`): 600 Foreplay credits a month, 100 always left in the account, 10 ads a call, 2 pages a goal. A paid page is saved
  to the goal before analysis, so a crash cannot lose or repay it. Ads goals pause alone (they never block video goals).
- **Page**: new Knowledge tab (counts, search, note view with links, Obsidian download, "Teach it something", what it is learning).
  Web endpoints: `/api/knowledge`, `/search`, `/note`, `/export`, `POST /goals`, `POST /goals/{id}/remove`. The web image now also
  copies `worker/hub.py` and `worker/hub_export.py` (Dockerfile + `watchPatterns`).
- **Unverified on real Postgres** (no local Postgres, `railway ssh` denied): the hub SQL and the learner's SQL are only run against stand-ins
  in tests. At worker boot, `Hub.selftest()` (via the maintainer's first step) and `Store.selftest()` (jobs.py, retried 3 times) run every
  statement on the real database and log `hub: self-test: ...` / `learner self-test: ...` (or FAILED; a failed learner self-test turns ads
  goals off). **After the next deploy, read the worker log for those two lines before trusting any of it.**
- **Needs Robert**: `FOREPLAY_API_KEY` in the worker's Railway variables (the IaC marks it `preserve()`); the ads goals only run when it and
  `OPENROUTER_VIDEO_AGENT_KEY` are set. Learning stays off in production (`LEARNING_ENABLED` is "0" there).
- **Not built yet**: playbook distillation (a short human-edited "what is working now" note), hub tools the loop can call mid-plan,
  watching Foreplay ads' media, a design-engine that uses the hub's references when building cards.

## More than one link (2026-10-02, committed locally, not pushed)

`APP_EXTRA_LINK_TOKENS` (web service variable, comma separated, set by hand in Railway, `preserve()` in the IaC) adds extra
links next to `APP_LINK_TOKEN`, e.g. one per person, so one person's link can be switched off alone (delete it from the
variable). An extra shorter than 16 characters or with odd characters is ignored. `/healthz` shows how many links work.
Anyone with any link sees the same team data. First extra link: for Gio, requested by Robert 2026-10-02.

## The new page: Robert's final GUI, first release (2026-10-01, built and tested on the PC, not pushed)

Robert delivered the final design (`AI video editor GUI mockups.zip` on his Desktop: Claude Design project, three tabs,
dark and light). Decisions he made (2026-10-01): **shared link + the team's one key** (no sign-in, no per-person keys),
**release in stages**, later features in this order: (1) "Change this ad" chat, (2) model and care pickers, (3) 4:5 and 1:1
sizes, (4) logo/screenshots, voice dictation, notifications.

What the first release is (`web/static/index.html`, plain HTML/CSS/JS in the design's look, icons inline, Nunito from
Google Fonts, no other outside scripts; light/dark follows the OS, Settings can force one):
- **Overview:** this month's stats (videos, ads, AI cost, average wait), Recent videos for the whole team with who made
  them, status badges, progress, cost and the right action (See ads / View progress / Try again).
- **Make ads:** drop clips (several allowed), request with idea chips, How many ads (1/3/5), How long (20/40/60 s), end
  screen message and button for the batch, Make my ads (Ctrl or Cmd + Enter). Progress screen with six steps and an
  honest estimate, **Cancel** with confirmation. Results: ad list with real thumbnails, Download all, per-ad download,
  claims checklist (ticks kept in the browser), a phone-frame player with the design's controls (speed, back/forward 15 s,
  loop, mute, full screen), the self-check scorecard and the Good / Not right buttons under the selected ad.
- **Settings:** the person's name (stored in the browser; `options.by` on each job), appearance, the team's AI spend vs the
  ledger cap (read-only), the reference-video library with lessons. No API-key fields: the key stays in Railway.
- Not in this release (design features with no engine yet): Change-this-ad chat, model/care pickers, 4:5 and 1:1 previews,
  extras (logo, screenshots), voice dictation, notifications, editing reference videos from Settings.

Engine/API changes behind it: `POST api/jobs` accepts `ads`, `seconds`, `cta_line`, `cta_button`, `by` (the two choices are
added to the request as one sentence for Gemini; the typed request is kept as `options.request`; end-screen wording is
stored only when it differs from the default and the worker applies it for that job); `POST api/jobs/<id>/cancel`
(queued/uploading jobs stop at once; a running job stops at the worker's next step: `jobs.set_stage` now returns False
when the job is no longer `working` and this worker's, which raises `Cancelled`, a BaseException like `Stop`);
`GET api/overview` (stats, spend, defaults; each part fails alone); `GET assets/vp-mark.png` (allow-list of one file);
Recent now carries by/cost/ad counts/percent. The page's image copies `worker/config.json` for the end-screen defaults
(`web/Dockerfile`; `.railway/railway.ts` watch pattern added: applies only after the next `config apply`).
Tests: worker 240 passed + 5 skipped, web 52 passed. **Not run on real Postgres:** the Overview stats SQL and the Recent
SQL use `jsonb_path_query_array(result, '$.ads[*] ? (@.file_key like_regex ".")')` (Postgres 12+); verify on staging.
Preview without Railway: `dev_server.py` pattern (real app + fake DB, served locally) was used for the visual checks.

## Step B (2026-10-01, built, reviewed and tested on the PC; NOT yet pushed or deployed)

Self-check scorecards + feedback buttons, as the plan's table defines B. The bounded re-plan is step D and is NOT built:
scores only inform the person, they change no ad.

- `worker/review.py`: after the renders, one Gemini call per finished ad (zero-retention route, a 480p/10 fps copy of
  the ad, `prompts/review_ads.md`) returns seven 1-5 scores (hook, cuts, story, captions, overlays, request_fit,
  compliance), up to six problems (time, area, what, fix) and a verdict. Code checks every score is a whole 1-5
  number, drops problems with unknown areas, and sets `look` by the plan's rule (min of cuts/captions/compliance <= 2,
  hook <= 2, mean < 3.2, or speech match < 0.85, and at least one problem). A failed check is a note, never a failed
  job. New job stage `checking`. Live test on a finished test ad: 1.3 cents; it flagged "30% more gains" and an
  abrupt start.
- **Spend rule (architect blocker, fixed):** one attempt per call (no retry), 300 s timeout; before each call the worst
  case (`llm.estimate_cost`) must fit under `review_job_cap_usd` (0.30, `worker/config.json`) together with what the
  job has spent on checks; a failed call counts at its worst case, and two failed calls in a row stop the job's remaining checks.
  `review_enabled` switches it off. **Deviations from the plan text, to confirm with Robert:** one call per ad instead
  of one call per job (about 2-5 cents an ad), and the 30-cent cap is for self-checks alone; the plan's 30 cents for
  re-plans (step D) is a separate budget.
- `worker/feedback.py`: the newest feedback (one item per job, at most 5 "Not right" + 3 "Good", 8 in all, notes
  flattened to 300 characters, quotes/braces/backticks removed) goes into `prompts/plan_ads.md` under "What the team
  said about earlier ads", with a rule that it is data and never overrides the request or the format rules. Passed as
  `team_notes=` to `run_pipeline`. Review scores never reach the planning prompt.
- `db/migrations/004_feedback.sql`: `ad_feedback` (job, ad number, good|bad, note up to 1000 chars; newest row wins).
- Page: `POST api/jobs/<id>/ads/<k>/feedback` (only for a ready job's rendered ad; 60 clicks per job; body note under
  4000 chars, control characters stripped), each ad shows its scorecard ("Worth a look before you run it" when flagged)
  and Good / Not right buttons with a note box. The result JSON also holds `spoken`, `review` per ad and `review_cost`;
  the self-check is in Review Notes.md.
- Tests: worker 236 passed + 5 skipped, web 25 passed. `jobs.py --selftest` has a new `self-check copy` line to prove
  the ffmpeg 5.1 container makes the small copy (only the PC's ffmpeg 8 has run it so far).
- Reviews: code-reviewer approved; architect found one blocker (the spend cap, above) and six should-fixes, all applied
  except the plan deviations, which need Robert's word.
- **To verify after a deploy:** migration 004 applied; `RECENT_SQL` in `worker/feedback.py` and the page's three new
  statements run on the real Postgres (rolled-back transaction); selftest shows `self-check copy` OK; then one real job
  and a click on each button; compare the job's `cost_usd` with its `spend_ledger` rows labelled `check ad%`.

## Where we left off (latest)

**First web job, 2026-10-01:** Robert uploaded a 33 s clip (this morning's finished ad, so its old captions were
burned in) with the request "create an attention grabbing facebook ad". 2 ads in 3 min 46 s for $0.04 of Gemini,
both checks passed, two claims flagged. Robert: "It's still not perfect. It needs to be trained and it needs to work
in a loop. There's a lot of mistakes." He has not yet said which mistakes; ask for the top three in the next ad he
judges (mechanical ones get fixed directly; taste goes to the loop).

**Plan approved ("yes", 2026-10-01): the library and the loop.** Build order: A library plumbing + tier 1 (his 17
craft videos), B feedback buttons + self-check, C playbook v1 (Robert reads it before it goes live), D one bounded
re-plan, E tier 2 (Foreplay, channels, search; Warrior Trading dropped: FTC 2022) and playbook v2. Decisions:
YouTube watching on Google's free tier (Flash, public videos only; Robert: "strictly talking about watching the
youtube videos via API not rendering my edits"); his own footage stays on OpenRouter zero-retention; billing is
off on the Gemini key's Google project.

**Step A is built, reviewed (two blockers and a stalling breaker found, fixed, re-approved) and deployed to staging
as `c1a6f5c`** (migration 003 applied; `LIBRARY_ENABLED=1` on the worker; the page's Library section shows "Nothing
studied yet"; tests: worker 185 passed + 5 skipped, web 15 passed). Verified live on 2026-10-01: the worker has
`LIBRARY_ENABLED=1` but **`GEMINI_API_KEY` and `YOUTUBE_API_KEY` are not set on it yet**, so the library is idle.
**Waiting on Robert:** paste them into Railway → App · Video Agent → staging → worker → Variables (same values as
the Windows user variables of the same names on his PC; Claude cannot write secrets, the auto-mode rule blocks it).
The worker then studies the 17 videos while idle (about 4.5 hours of video, about 27 parts of up to 10 minutes, a day
at most, $0). Check it after he says it is done (first `jobs.py --selftest` shows the `library` line):

```bash
KEY="$HOME/.railway/ssh/railway_video_agent"
npm run railway -- ssh -i "$KEY" -- sh -c 'cd /app/worker && python tools/jobctl.py library'
npm run railway -- ssh -i "$KEY" -- sh -c 'cd /app/worker && python tools/jobctl.py lessons QR8LxximqWI'
```

If videos end up `failed`, fix the cause and run `python tools/jobctl.py library-retry failed`. Then show Robert the
first notes (the page's Library section, "Show the lessons") and start step B (Good / Not right buttons on each ad,
`ad_feedback` table, and the post-render Gemini self-check with one bounded re-plan; the architect's full design is
in the Desktop plan file). Ask Robert for the top three things he would change in the ads he has seen.

Keys on this PC (Windows user env vars, never in files): `GEMINI_API_KEY` (free tier), `YOUTUBE_API_KEY`,
`OPENROUTER_VIDEO_AGENT_KEY`, `OPENROUTER_VIDEO_AGENT_LIBRARY_KEY`. Scratch scripts used for live checks are in the
session scratchpad, not the repo.

## Where we left off (W1, earlier on 2026-10-01)

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

**Robert set `APP_LINK_TOKEN` on the web service on 2026-10-01** (Claude's own attempt was blocked by the auto-mode
rule against writing secrets). `/healthz` shows `"link": "set"`, the page loads at the link, and both services run
commit `3ca87de`. The team link is `https://web-staging-c524.up.railway.app/<token>/`; the token is in the Railway
variable, never in this repo. To change it, edit the variable; Railway redeploys the page.

**Next: the first real test through the page.** Open the link, drop one or more clips (10 minutes of footage per
job at most, 4 GB per clip), type what you want, click **Make ads**. Expect 10 to 20 minutes. If a job fails, the
page shows the plain-English reason; `jobctl.py events <id>` in the container has the steps:

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

## The reference library (worker/library.py, step A)

- **Tier 1 list:** `worker/library/foundation.txt`, Robert's 17 essential HillierSmith videos on the craft of
  editing (from his 30; 5 optional and 8 left-out lines stay in the file as comments with reasons). Edit the file
  and deploy to change it; removed lines become `skipped` and leave the library.
- **Watching:** `worker/gemini_free.py` calls Google's Gemini API free tier with only a YouTube watch URL built from
  a validated id, an optional time window, and the allow-listed prompt `prompts/watch_craft.md`. Nothing is
  downloaded. Models in order (config `library.watch_models`): gemini-3.5-flash, gemini-3.8-flash, gemini-2.5-flash;
  a busy model falls back to the next; a retry after a rejected note starts with a different model.
- **Parts:** videos longer than 12 minutes are watched in parts of 10 minutes (`windows()`); Gemini reports times
  from the start of the part (verified on the live API), and the code makes them whole-video times.
- **"Really watched" checks** (`validate_note`): reported seconds within 15% of the part, first and last words
  given, at least 50 video tokens per second (from Google's own usage count), lessons with a time outside the part
  dropped; more than 40% dropped rejects the note. Three rejected notes mark the video failed.
- **Limits:** 7 hours of free video per Pacific day (Google allows 8), 2,000 YouTube units per day, both in
  `api_quota`. Busy: pause 10 min. Rate limit: 30 min, the third in a row 6 hours. 403 on a video: the key is
  checked first, so a broken key pauses instead of failing every video.
- **Runs only when idle:** `jobs.serve()` claims editing jobs first; one library step is one part (about 1 to 2
  minutes), so a new job waits at most that long. Off unless `LIBRARY_ENABLED=1` and both keys are set (production
  has it set to 0 in `.railway/railway.ts`).
- **Data:** `ref_sources` (one row per video, metadata refreshed after 25 days per YouTube's 30-day rule),
  `ref_notes` (one row per watched part, kept even when rejected, with the problems), `api_quota`. Migration
  `003_library.sql`.
- **Seeing it:** the page's Library section (`GET api/library`, lessons on request), `jobctl.py library` and
  `jobctl.py lessons <video id>` in the container, and the self-test's `library` line.
- **Spike results (2026-10-01, $0):** 3.5 Flash watched by link correctly (about 90 tokens per second of video);
  2.5 Flash about 290 per second and put 5 of 14 lesson times past the end of a 6-minute video (hence the time
  checks); 3.8 Flash and 2.5 Flash were often "high demand" (503); a fake id gives 403; `video_metadata` start/end
  offsets work for YouTube links.

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
- **GitHub:** `VantagePoint-Marketing/video-agent` (renamed from `ad-cutter` on 2026-10-01 at Robert's request; GitHub
  redirects the old address and Railway followed the rename, both services still deploy from the branch).
- **Done 2026-10-01 at Robert's request:** the orphan 50 GB `postgres-volume` in staging was deleted (the live database
  uses `postgres-volume-oDH6`). The Railway $150/month usage limit is NOT set: the Railway tools here have no billing
  access; set it in Railway → account/workspace settings → Usage/Billing.
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
| Paste `GEMINI_API_KEY` and `YOUTUBE_API_KEY` into the worker's Railway variables (starts the library) | Robert |
| Name the top mistakes in the ads (mechanical fixes vs. taste for the loop) | Robert |
| Read playbook v1 before it goes into the prompts (step C) | Robert |
| ~~Delete the orphan volume `postgres-volume`~~ | done 2026-10-01 |
| Keep the $150 key in Railway or swap in the PC's $100/month key | Robert (he said keep it) |
| Railway $150/month usage limit | Robert, in Railway billing settings (the tools here cannot reach billing) |
| ~~Rename the repo to `video-agent`~~ | done 2026-10-01. Still open: move the Figma file to a company team |
| When to merge `w0-worker-cloud` into `main` and apply the IaC to `production` | Robert, after the staging test |
