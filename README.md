# ad-cutter (the VantagePoint Video Agent)

Turns raw vertical talking-head clips plus a short request into ready-to-review Meta ad cuts.

**On the web (Railway):** open the team link (`https://<domain>/<token>/`, ask Robert for it), drop one or more
clips, say what you want, and wait. Gemini decides how many ads to make, how long, which parts of the footage to
use, the headline, callouts and ad copy. The ads appear on the page with download links and review notes.

**On this PC:** drag one or more clips onto `Cut ads.cmd` (or run `python worker/ad_cutter.py CLIP [CLIP ...]
--brief "..."`). The finished ads land in `OneDrive\RevOps\Meta Ads\05 Ad Cutter Drafts\<first clip> (<date>)\`
with a `Review Notes.md`.

How it works:
1. **Preparing:** ffmpeg joins the clips, in the order given, into one 1080x1920 working copy (10 minutes of
   footage per job at most). Whisper transcribes it with word timings.
2. **Planning (Gemini Pro, via OpenRouter):** Gemini watches a small proxy of the footage, reads the transcript and
   the request, and plans the ads: segments, headline, callouts, caption fixes and claims to review. It picks words
   by index, so it can't invent timestamps, and it says how it read the request.
3. **Cutting and rendering:** every cut edge is snapped to the gap between words, pauses are trimmed (how hard
   depends on the ad's pace), and audio is levelled to -14 LUFS. HyperFrames renders the captions, headline,
   callouts, punch-in zooms and end screen. **No two ads share a fixed template:** for each ad Gemini designs the
   whole look (pace, caption style and size and position, headline style, callout style and sides, motion,
   colours, and an end screen with its own call-to-action text) from a menu in `worker/design.py`. The model only
   picks names and short text; the code renders them, so no model-written markup reaches the render browser.
4. **Checking:** each finished ad is transcribed again and compared with its captions.

- The pipeline lives in `worker/` (the processing service on Railway and the local command line are the same code).
- The agent's **brain** is every `.md` / `.json` file in `worker/brain/` (editing craft notes from Robert's Drive,
  plus a short-ads playbook). They go into the planning prompt as guidance; edit or add files there to change
  what it knows, no code change needed. `03_short_ads_playbook.md` is a draft awaiting Robert's review.
- The reference library lives in `worker/library.py`: while no ads are being made, Gemini studies the videos in
  `worker/library/foundation.txt` (the craft of editing) by their YouTube links on Google's free tier and keeps
  checked notes. The page's Library section shows what it has learned. Next: a playbook built from those notes,
  a self-check after rendering, and Good / Not right feedback on each ad.
- The page lives in `web/` (FastAPI; one shared link, no accounts). `web/static/index.html` is the whole front end.
- Settings live in `worker/config.json`: brand, CTA, model, default ad count (`ad_count`) and the most ads Gemini
  may plan (`ad_count_max`).
- Fonts and GSAP are stored in `worker/template/vendor/` (see `SOURCES.md` there), so renders need no internet.
- Useful local options: `--replan` (ask Gemini again, about $0.10), `--only 2` (rebuild one ad), `--no-render`.
- Tests: `python -m pytest -q` in `worker/` and in `web/`.
- Cloud setup (Railway worker + web, private Postgres, media bucket): `.railway/README.md` for the infrastructure
  file and `HANDOFF.md` for the current state and test steps.

**Data:** only a small 640p proxy of the footage goes to Gemini, through OpenRouter's zero-retention Google
endpoints. The key comes from the `OPENROUTER_VIDEO_AGENT_KEY` environment variable, never from a file. Uploads and
finished ads live in a private bucket and are served through links that expire after an hour.
