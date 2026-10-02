"""The Ad Cutter page: upload clips, say what you want, get ads. One shared link, no accounts.

    python app.py                      serve on $PORT (Railway) or 8000

Environment (Railway variables, declared in .railway/railway.ts):
    APP_LINK_TOKEN                     the secret part of the link, https://<domain>/<token>/  (set by hand)
    APP_EXTRA_LINK_TOKENS              optional, more links for other people (comma separated, set by hand)
    DATABASE_URL                       the Postgres the worker uses (private network)
    BUCKET, ACCESS_KEY_ID, SECRET_ACCESS_KEY, ENDPOINT, REGION     the media bucket, the same one the worker uses
    RAILWAY_PUBLIC_DOMAIN              set by Railway: this page's own address, allowed to upload into the bucket
    APP_EXTRA_ORIGIN                   optional, a second address allowed to upload (a local test run)

How a job flows: the page asks for a job (POST .../api/jobs) and gets one short-lived signed upload link per clip;
the browser sends each clip straight into the bucket; then it starts the job (POST .../start), which checks that
every clip arrived in full and queues it for the worker. The page polls the job until it is ready, then shows the
ads with signed preview and download links. Only the worker talks to Gemini, and its monthly ledger caps the spend.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import logging
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Path as UrlPath
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))      # storage.py is shared with the worker (copied into the image)
import models  # noqa: E402
from storage import MAX_CLIPS, Bucket  # noqa: E402

log = logging.getLogger("video-agent-web")
TOKEN = os.environ.get("APP_LINK_TOKEN", "").strip()
EXTRA_TOKENS = [t.strip() for t in os.environ.get("APP_EXTRA_LINK_TOKENS", "").split(",") if t.strip()]
MAX_CLIP_BYTES = 4 * 1024 ** 3           # the worker refuses larger downloads
MAX_JOB_BYTES = 10 * 1024 ** 3           # all clips together (10 minutes of 4K phone footage is about 4 GB)
MAX_BRIEF = 2000                         # characters of the request that reach Gemini (worker: ad_cutter.MAX_BRIEF)
LINK_SECONDS = 3600                      # how long signed preview/download links stay valid
UPLOAD_LINK_SECONDS = 6 * 3600           # upload links last longer: several big clips on a home connection
DAILY_JOBS = 30                          # jobs the page accepts per rolling day (worker time is not capped elsewhere)
RECENT = 30
BUDGET_KEY = "video-agent"               # the worker's ledger name for the staff editing key (worker/jobs.py KEY_NAME)
ADS_CHOICES, SECONDS_CHOICES = (1, 3, 5), (20, 40, 60)       # the page's "How many ads?" and "How long?"
MAX_NAME, CTA_LINE_MAX, CTA_BUTTON_MAX = 40, 70, 28          # a typed name; end-screen message and button wording
MAX_NOTE = 1000                          # characters of a feedback note (migration 004 enforces the same)
FEEDBACK_PER_JOB = 60                    # clicks one job accepts: the page's buttons can be changed, not spammed
LIBRARY_REMOVED = "removed from the foundation list"     # worker/library.py REMOVED
STATE = {"uploads": "not set up"}
JOB_COLUMNS = ("id::text as job_id, status, stage, stage_detail, error, created_at, started_at, finished_at, "
               "source_name, sources, options, result, cost_usd")


# ---------------------------------------------------------------- plumbing

def connect():
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=15)


def rows(conn, sql: str, params=()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def bucket() -> Bucket:
    return Bucket()


def token_problem() -> str | None:
    """Why the link cannot work yet, or None. Checked on every request, so setting the variable needs no restart
    logic: Railway redeploys the service when a variable changes."""
    if len(TOKEN) < 16 or not re.fullmatch(r"[A-Za-z0-9_-]+", TOKEN):
        return "APP_LINK_TOKEN is not set to 16 or more letters, digits, - or _; the page is unreachable until it is"
    return None


def usable(token: str) -> bool:
    return len(token) >= 16 and re.fullmatch(r"[A-Za-z0-9_-]+", token) is not None


def links() -> list[str]:
    """Every link that works: the main one plus any extra ones (APP_EXTRA_LINK_TOKENS, comma separated), so one
    person's link can be switched off without touching the others. An extra that is too short or has odd
    characters is ignored, never accepted."""
    return [t for t in [TOKEN, *EXTRA_TOKENS] if usable(t)]


def link(token: str) -> str:
    """The secret part of the link is the whole access control. Anything else is a plain 404."""
    if token_problem():
        raise HTTPException(404, "Not found")
    given, ok = token.encode("utf-8", "replace"), False
    for good in links():                 # compare against every link, without stopping at the first match
        ok |= hmac.compare_digest(given, good.encode("utf-8"))
    if not ok:
        raise HTTPException(404, "Not found")
    return token


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", re.split(r"[\\/]", name)[-1]).strip()
    return name[:120] or "clip"


def iso(value):
    return value.isoformat() if isinstance(value, dt.datetime) else value


def allow_uploads_from_this_page() -> str:
    """Set the bucket's CORS rule to this page's own address, so the browser can PUT clips straight into it."""
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    origins = [f"https://{domain}"] if domain else []
    if os.environ.get("APP_EXTRA_ORIGIN", "").strip():
        origins.append(os.environ["APP_EXTRA_ORIGIN"].strip())
    if not origins:
        return "no public domain yet; browser uploads will fail until the service has one"
    try:
        bucket().allow_browser_uploads(origins)
    except Exception as err:          # noqa: BLE001 - the page still serves; /healthz names the error type only
        log.exception("could not set the bucket's CORS rule")
        return f"FAILED: {type(err).__name__} (see the service logs)"
    return f"allowed from {', '.join(origins)}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE["uploads"] = allow_uploads_from_this_page()
    log.info("browser uploads: %s", STATE["uploads"])
    yield


app = FastAPI(title="Video Agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# ---------------------------------------------------------------- the page

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "link": token_problem() or "set", "links": len(links()), "uploads": STATE["uploads"]}


@app.get("/{token}/", response_class=HTMLResponse)
@app.get("/{token}", response_class=HTMLResponse)
def page(token: str = Depends(link)) -> str:
    return (HERE / "static" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- jobs

class Clip(BaseModel):
    name: str
    bytes: int


class NewJob(BaseModel):
    brief: str = ""
    clips: list[Clip]
    ads: int | None = None               # the page's "How many ads?" (1, 3 or 5); None: Gemini decides
    seconds: int | None = None           # the page's "How long?" (20, 40 or 60); None: Gemini decides
    model: str | None = None             # the page's AI menu (a key of models.MODELS); None: the default
    effort: str | None = None            # the page's effort menu (low, medium, high); None: the default
    cta_line: str = Field("", max_length=400)      # end-screen wording for this batch; blank keeps the default
    cta_button: str = Field("", max_length=400)
    by: str = Field("", max_length=400)            # the name the person typed in their browser, shown in Recent


def tidy(text: str, limit: int) -> str:
    """Typed text kept to one line: control characters and runs of spaces become one space, cut to `limit`."""
    return re.sub(r"[\x00-\x1f\x7f\s]+", " ", text or "").strip()[:limit].rstrip()


def team_defaults() -> dict:
    """The end-screen wording the worker uses when a batch does not change it (worker/config.json is copied into the
    page's image for this)."""
    try:
        cfg = json.loads((HERE.parent / "worker" / "config.json").read_text(encoding="utf-8"))
        brand, seconds = cfg.get("brand", {}), cfg.get("cta_seconds", 3)
        return {"cta_line": str(brand.get("cta_line", "")), "cta_button": str(brand.get("cta_button", "")),
                "cta_seconds": seconds}
    except (OSError, ValueError, AttributeError):
        return {"cta_line": "", "cta_button": "", "cta_seconds": 3}


@app.post("/{token}/api/jobs")
def create_job(body: NewJob, token: str = Depends(link)) -> dict:
    """A new job in 'uploading', with one signed upload link per clip. Nothing runs until /start."""
    brief = body.brief.strip()
    if body.ads is not None and body.ads not in ADS_CHOICES:
        raise HTTPException(400, "Choose 1, 3 or 5 ads.")
    if body.seconds is not None and body.seconds not in SECONDS_CHOICES:
        raise HTTPException(400, "Choose 20, 40 or 60 seconds.")
    if body.model is not None and body.model not in models.MODELS:
        raise HTTPException(400, "Choose one of the AIs in the list.")
    if body.effort is not None and body.effort not in models.EFFORTS:
        raise HTTPException(400, "Choose Quick, Balanced or Thorough.")
    asked = brief            # what the person typed: shown back to them
    if body.ads is not None or body.seconds is not None:       # the page's choices go to Gemini as one more sentence
        parts = ([f"{body.ads} ad{'s' if body.ads != 1 else ''}"] if body.ads is not None else []) + \
                ([f"each about {body.seconds} seconds long"] if body.seconds is not None else [])
        brief = (brief + "\n\n" if brief else "") + "Make " + ", ".join(parts) + "."
    if len(brief) > MAX_BRIEF:
        raise HTTPException(400, f"Keep the description under {MAX_BRIEF:,} characters.")
    if not 1 <= len(body.clips) <= MAX_CLIPS:
        raise HTTPException(400, f"Upload 1 to {MAX_CLIPS} clips.")
    job_id = str(uuid.uuid4())
    sources = []
    for n, clip in enumerate(body.clips):
        name = clean_name(clip.name)
        if not 0 < clip.bytes <= MAX_CLIP_BYTES:
            raise HTTPException(400, f"{name} is {clip.bytes / 1024 ** 3:.1f} GB; each clip must be under 4 GB.")
        sources.append({"key": Bucket.clip_key(job_id, n), "name": name, "bytes": int(clip.bytes)})
    total = sum(s["bytes"] for s in sources)
    if total > MAX_JOB_BYTES:
        raise HTTPException(400, f"The clips add up to {total / 1024 ** 3:.1f} GB; the limit is "
                                 f"{MAX_JOB_BYTES // 1024 ** 3} GB per job.")
    label = sources[0]["name"] + (f" + {len(sources) - 1} more" if len(sources) > 1 else "")
    # `brief` is what Gemini reads; `request` is what the person typed (shown back to them). The end-screen wording is
    # kept only when it differs from the default, and the worker trims and escapes it again.
    options = {"brief": brief, "request": asked}
    for key, value in (("ads", body.ads), ("seconds", body.seconds), ("model", body.model), ("effort", body.effort)):
        if value is not None:
            options[key] = value
    if tidy(body.by, MAX_NAME):
        options["by"] = tidy(body.by, MAX_NAME)
    for key, limit in (("cta_line", CTA_LINE_MAX), ("cta_button", CTA_BUTTON_MAX)):
        text = tidy(getattr(body, key), limit)
        if text and text != team_defaults()[key]:
            options[key] = text
    with connect() as conn:
        # jobs that never reached the worker (an upload that failed or was stopped) spent nothing: they do not count
        today = conn.execute("select count(*) from jobs where created_by = 'web' and created_at > now() - "
                             "interval '1 day' and not (status in ('uploading', 'cancelled') and started_at is null)"
                             ).fetchone()[0]
        if int(today or 0) >= DAILY_JOBS:
            raise HTTPException(429, f"The editor has taken {DAILY_JOBS} jobs in the last 24 hours, which is its "
                                     "daily limit. Please try again tomorrow.")
        conn.execute("insert into jobs (id, created_by, kind, status, source_key, source_name, sources, options) "
                     "values (%s, 'web', 'edit', 'uploading', %s, %s, %s, %s)",
                     (job_id, sources[0]["key"], label, json.dumps(sources), json.dumps(options)))
        conn.execute("insert into job_events (job_id, message) values (%s, %s)",
                     (job_id, f"created from the web page with {len(sources)} clip(s)"))
    b = bucket()
    return {"job_id": job_id, "clips": [{"n": n, "name": s["name"], "put_url": b.put_url(s["key"], UPLOAD_LINK_SECONDS)}
                                        for n, s in enumerate(sources)]}


@app.post("/{token}/api/jobs/{job_id}/start")
def start_job(job_id: uuid.UUID, token: str = Depends(link)) -> dict:
    """Every clip must be in the bucket at its declared size; then the job joins the worker's queue."""
    jid = str(job_id)
    with connect() as conn:
        found = rows(conn, "select status, sources from jobs where id = %s and created_by = 'web'", (jid,))
    if not found:
        raise HTTPException(404, "No such job.")
    if found[0]["status"] == "uploading":
        b = bucket()
        for n, s in enumerate(found[0]["sources"] or []):
            size = b.size(s["key"])
            if size != int(s["bytes"]):
                what = "did not arrive" if size is None else f"arrived incomplete ({size:,} of {int(s['bytes']):,} bytes)"
                raise HTTPException(409, f"Clip {n + 1} ({s['name']}) {what}. Please try again.")
        with connect() as conn:
            flipped = conn.execute("update jobs set status = 'queued' where id = %s and status = 'uploading' "
                                   "returning id", (jid,)).fetchone()
            if flipped:               # two overlapping /start calls record the event once
                conn.execute("insert into job_events (job_id, message) values (%s, %s)",
                             (jid, "all clips arrived; queued"))
    return job_view(jid)


@app.post("/{token}/api/jobs/{job_id}/cancel")
def cancel_job(job_id: uuid.UUID, token: str = Depends(link)) -> dict:
    """Stop a job that has not finished. A job still waiting stops at once; one the worker is running stops at the
    worker's next step (it checks before each one), so the money already spent on the current step is not recovered.
    Once the worker is saving the finished ads (stage 'uploading') it is too late to stop: the ads are paid for, so they
    are kept and the page shows them."""
    jid = str(job_id)
    with connect() as conn:
        if not rows(conn, "select status from jobs where id = %s and created_by = 'web'", (jid,)):
            raise HTTPException(404, "No such job.")
        stopped = conn.execute("update jobs set status = 'cancelled', finished_at = now(), stage = null, "
                               "stage_detail = null where id = %s and created_by = 'web' and "
                               "status in ('uploading', 'queued', 'working') and "
                               "not (status = 'working' and stage = 'uploading') returning id", (jid,)).fetchone()
        if stopped:
            conn.execute("insert into job_events (job_id, message) values (%s, %s)", (jid, "cancelled from the web page"))
    return job_view(jid)


@app.get("/{token}/api/jobs/{job_id}")
def job_status(job_id: uuid.UUID, token: str = Depends(link)) -> dict:
    return job_view(str(job_id))


@app.get("/{token}/api/overview")
def overview(token: str = Depends(link)) -> dict:
    """The Overview tab: this month's numbers, the team's AI spend against its limit, and the end-screen defaults.
    Each part is read on its own, so one failing query leaves a gap (null) in the page, not a broken page."""
    out: dict = {"stats": None, "spend": None, "defaults": team_defaults(), "choices": models.catalog()}
    try:
        with connect() as conn:
            found = rows(conn, "select count(*) filter (where status in ('queued', 'working', 'ready', 'failed')) as videos, "
                               "coalesce(sum(cost_usd), 0) as cost, "
                               "coalesce(avg(extract(epoch from finished_at - created_at)) filter (where status = 'ready'), 0) "
                               "as wait_seconds, "
                               "coalesce(sum(jsonb_array_length(jsonb_path_query_array(result, "
                               "'$.ads[*] ? (@.file_key like_regex \".\")'))) filter (where status = 'ready'), 0) as ads "
                               "from jobs where created_by = 'web' and created_at >= date_trunc('month', now())")
        row = found[0]
        out["stats"] = {"videos": int(row["videos"]), "ads": int(row["ads"]), "cost": round(float(row["cost"]), 2),
                        "wait_minutes": round(float(row["wait_seconds"]) / 60)}
    except Exception as err:   # noqa: BLE001 - a gap in the page, not a broken page
        log.warning("overview stats failed: %s", type(err).__name__)
    try:
        with connect() as conn:
            found = rows(conn, "select spent, reserved, cap_usd from budget_months where key_name = %s and month = "
                               "to_char(now() at time zone 'UTC', 'YYYY-MM')", (BUDGET_KEY,))
        row = found[0] if found else None
        out["spend"] = {"spent": round(float(row["spent"]), 2) if row else 0.0,
                        "reserved": round(float(row["reserved"]), 2) if row else 0.0,
                        "cap": float(row["cap_usd"]) if row else None}
    except Exception as err:   # noqa: BLE001
        log.warning("overview spend failed: %s", type(err).__name__)
    return out


ASSETS = {"vp-mark.png": "image/png"}          # the only files /assets serves: no path from the URL reaches the disk


@app.get("/{token}/assets/{name}")
def asset(name: str, token: str = Depends(link)) -> FileResponse:
    if name not in ASSETS:
        raise HTTPException(404, "Not found")
    return FileResponse(HERE / "static" / "assets" / name, media_type=ASSETS[name],
                        headers={"Cache-Control": "private, max-age=86400"})


@app.get("/{token}/api/jobs")
def recent_jobs(token: str = Depends(link)) -> dict:
    head = ("select id::text as job_id, status, stage, stage_detail, error, created_at, finished_at, "
            "source_name as label, coalesce(options->>'request', options->>'brief') as brief, "
            "options->>'by' as by, (options->>'ads')::int as asked_ads, cost_usd, jsonb_array_length(sources) as clips, ")
    tail = " from jobs where created_by = 'web' and status <> 'uploading' order by created_at desc limit %s"
    counts = ("jsonb_array_length(jsonb_path_query_array(result, '$.ads[*]')) as ads_planned, "
              "jsonb_array_length(jsonb_path_query_array(result, '$.ads[*] ? (@.file_key like_regex \".\")')) as ads_ready")
    with connect() as conn:
        try:
            found = rows(conn, head + counts + tail, (RECENT,))
        except Exception as err:   # noqa: BLE001 - the ad counts are a nicety; the list itself must still load
            log.warning("recent jobs: ad counts failed (%s); listing without them", type(err).__name__)
            found = rows(conn, head + "0 as ads_planned, 0 as ads_ready" + tail, (RECENT,))
    out = []
    for r in found:
        percent, left = estimate(r["status"], r["stage"], r["stage_detail"], r["asked_ads"] or 3)
        out.append({"job_id": r["job_id"], "status": r["status"], "stage": r["stage"], "stage_detail": r["stage_detail"],
                    "error": r["error"], "label": r["label"], "brief": r["brief"], "by": r["by"], "clips": r["clips"],
                    "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
                    "ads_planned": r["ads_planned"] or 0, "ads_ready": r["ads_ready"] or 0,
                    "percent": percent, "minutes_left": left,
                    "created_at": iso(r["created_at"]), "finished_at": iso(r["finished_at"])})
    return {"jobs": out}


class Feedback(BaseModel):
    verdict: Literal["good", "bad"]
    note: str = Field("", max_length=4000)         # a bound on the body; the tidied note is held to MAX_NOTE


@app.post("/{token}/api/jobs/{job_id}/ads/{k}/feedback")
def save_feedback(job_id: uuid.UUID, body: Feedback, k: int = UrlPath(ge=1, le=20),
                  token: str = Depends(link)) -> dict:
    """"Good" or "Not right" (and an optional note) for one finished ad. The newest click for an ad wins; the worker
    shows the latest few to Gemini as examples when it plans the next ads."""
    jid = str(job_id)
    note = re.sub(r"[\x00-\x1f\x7f\s]+", " ", body.note).strip()      # one line; Postgres refuses a NUL character
    if len(note) > MAX_NOTE:
        raise HTTPException(400, f"Keep the note under {MAX_NOTE:,} characters.")
    with connect() as conn:
        found = rows(conn, "select status, result from jobs where id = %s and created_by = 'web'", (jid,))
        ads = ((found[0]["result"] or {}).get("ads") or []) if found else []
        if not found or found[0]["status"] != "ready" or not any(a.get("k") == k and a.get("file_key") for a in ads):
            raise HTTPException(404, "No such ad.")
        if int(conn.execute("select count(*) from ad_feedback where job_id = %s", (jid,)).fetchone()[0] or 0) \
                >= FEEDBACK_PER_JOB:
            raise HTTPException(429, "That is plenty of feedback for one job.")
        conn.execute("insert into ad_feedback (job_id, ad_k, verdict, note) values (%s, %s, %s, %s)",
                     (jid, k, body.verdict, note))
    return {"k": k, "verdict": body.verdict, "note": note}


@app.get("/{token}/api/library")
def library(lessons: bool = False, token: str = Depends(link)) -> dict:
    """What the agent has studied: one entry per video, with its kept lessons when `lessons` is true."""
    import psycopg
    try:
        with connect() as conn:
            sources = rows(conn, "select id, tier, external_id, title, channel, seconds, status, reason "
                                 "from ref_sources where not (status = 'skipped' and reason is not distinct from %s) "
                                 "order by tier, id", (LIBRARY_REMOVED,))
            # the lessons themselves only when asked for; the page's summary line needs counts only
            notes = rows(conn, "select distinct on (source_id, window_start) source_id, window_start, "
                               "note->>'summary' as summary, "
                               "jsonb_array_length(coalesce(note->'lessons', '[]'::jsonb)) as lesson_count, "
                               "case when %s then note->'lessons' end as lessons "
                               "from ref_notes where reference_ok order by source_id, window_start, created_at desc",
                         (lessons,))
            used = conn.execute("select coalesce(sum(used), 0) from api_quota where api = 'gemini-free-video-seconds' "
                                "and period = to_char(now() at time zone 'America/Los_Angeles', 'YYYY-MM-DD')"
                                ).fetchone()[0]
    except psycopg.errors.UndefinedTable:
        return {"videos": [], "studied": 0, "total": 0, "lessons": 0, "hours_today": 0}
    by_source: dict[int, list] = {}
    for n in sorted(notes, key=lambda n: (n["source_id"], n["window_start"])):
        by_source.setdefault(n["source_id"], []).append(n)
    videos, total_lessons = [], 0
    for s in sources:
        parts = by_source.get(s["id"], [])
        count = sum(int(p["lesson_count"] or 0) for p in parts)
        found = sorted((x for p in parts for x in (p["lessons"] or []) if isinstance(x, dict)),
                       key=lambda x: x.get("at_s") or 0)
        total_lessons += count
        v = {"id": s["external_id"], "tier": s["tier"], "title": s["title"], "channel": s["channel"],
             "minutes": round((s["seconds"] or 0) / 60, 1), "status": s["status"], "reason": s["reason"],
             "url": f"https://www.youtube.com/watch?v={s['external_id']}", "lesson_count": count,
             "summary": (parts[0]["summary"] if parts else "") or ""}
        if lessons:
            v["lessons"] = [{"at_s": x.get("at_s"), "topic": x.get("topic"), "principle": x.get("principle"),
                             "lever": x.get("lever"), "how_we_apply": x.get("how_we_apply")} for x in found]
        videos.append(v)
    studied = sum(1 for v in videos if v["status"] == "done")
    return {"videos": videos, "studied": studied, "total": len(videos), "lessons": total_lessons,
            "hours_today": round(float(used) / 3600, 1)}


def job_view(job_id: str) -> dict:
    with connect() as conn:
        found = rows(conn, f"select {JOB_COLUMNS} from jobs where id = %s and created_by = 'web'", (job_id,))
        if not found:
            raise HTTPException(404, "No such job.")
        row = found[0]
        ahead = 0
        if row["status"] == "queued":
            ahead = conn.execute("select count(*) from jobs where status in ('queued', 'working') and created_at < "
                                 "(select created_at from jobs where id = %s)", (job_id,)).fetchone()[0]
        said = {}
        if row["status"] in ("ready", "failed"):
            import psycopg
            try:      # the newest click for each ad; a deploy that beats migration 004 just shows no buttons state
                said = {r["ad_k"]: {"verdict": r["verdict"], "note": r["note"]} for r in rows(
                    conn, "select distinct on (ad_k) ad_k, verdict, note from ad_feedback where job_id = %s "
                          "order by ad_k, created_at desc, id desc", (job_id,))}
            except psycopg.errors.UndefinedTable:
                said = {}
    return present(row, int(ahead or 0), said)


# where each worker stage sits on the progress bar (percent), and the minutes a whole job usually takes
STAGE_PERCENT = {"starting": 5, "downloading": 8, "preparing": 18, "transcribing": 28, "planning": 45,
                 "rendering": 45, "checking": 88, "uploading": 97}
STAGE_SPAN = {"rendering": 42, "checking": 8}       # these two split their span across the ads (detail "ad 2 of 3")


def estimate(status: str, stage: str | None, detail: str | None, ads: int = 3) -> tuple[int, int | None]:
    """(percent done, minutes left) for the progress screen. An honest guess from the usual timings (about 6 minutes
    plus 3.5 per ad), not a promise; minutes left is None when the job is not running."""
    if status in ("ready", "failed", "cancelled"):
        return 100, None
    if status != "working":
        return 3, None if status == "uploading" else 6 + round(3.5 * ads)
    percent = STAGE_PERCENT.get(stage or "", 5)
    found = re.fullmatch(r"ad (\d+) of (\d+)", detail or "")
    if found and stage in STAGE_SPAN and 0 < int(found.group(1)) <= int(found.group(2)):
        n, m = int(found.group(1)), int(found.group(2))
        percent += round(STAGE_SPAN[stage] * (n - 1) / m)
        ads = m
    return percent, max(1, round((6 + 3.5 * ads) * (100 - percent) / 100))


def public_review(review) -> dict | None:
    """A self-check scorecard as the page needs it (the worker validated it; this only drops anything unexpected)."""
    if not isinstance(review, dict) or not isinstance(review.get("scores"), dict):
        return None
    return {"scores": review["scores"], "problems": review.get("problems") or [], "verdict": review.get("verdict", ""),
            "look": bool(review.get("look"))}


def present(row: dict, ahead: int = 0, feedback: dict | None = None) -> dict:
    """What the page shows: the job's state in plain fields, and when it is done, the ads with signed links."""
    opts = row.get("options") or {}
    percent, left = estimate(row["status"], row.get("stage"), row.get("stage_detail"), int(opts.get("ads") or 3))
    out = {"job_id": row["job_id"], "status": row["status"], "stage": row.get("stage"),
           "stage_detail": row.get("stage_detail"), "error": row.get("error"),
           "brief": opts.get("request", opts.get("brief", "")), "by": opts.get("by"), "percent": percent,
           "minutes_left": left, "asked": {"ads": opts.get("ads"), "seconds": opts.get("seconds"),
                                           "model": opts.get("model"), "effort": opts.get("effort")},
           "label": row.get("source_name"), "clips": [s.get("name") for s in (row.get("sources") or [])],
           "created_at": iso(row.get("created_at")), "started_at": iso(row.get("started_at")),
           "finished_at": iso(row.get("finished_at")), "ahead": ahead,
           "cost_usd": float(row["cost_usd"]) if row.get("cost_usd") is not None else None}
    res = row.get("result") or {}
    if row["status"] in ("ready", "failed") and res:
        b = bucket()
        ads = []
        for ad in res.get("ads", []):
            key = ad.get("file_key")
            name = key.rsplit("/", 1)[-1] if key else None
            ads.append({"k": ad.get("k"), "name": ad.get("name"), "headline": ad.get("headline"),
                        "funnel_stage": ad.get("funnel_stage"), "angle": ad.get("angle"),
                        "callouts": ad.get("callouts", []), "primary_text": ad.get("primary_text"),
                        "seconds": ad.get("seconds"), "layout_check": ad.get("layout_check"),
                        "speech_match": (ad.get("verify") or {}).get("match"), "error": ad.get("error"), "file": name,
                        "review": public_review(ad.get("review")), "feedback": (feedback or {}).get(ad.get("k")),
                        "preview_url": b.get_url(key, name, expires=LINK_SECONDS) if key else None,
                        "download_url": b.get_url(key, name, attachment=True, expires=LINK_SECONDS) if key else None})
        notes_key = res.get("notes_key")
        out["result"] = {"summary": res.get("summary", ""), "response_to_request": res.get("response_to_request", ""),
                         "claims_to_review": res.get("claims_to_review", []),
                         "pipeline_notes": res.get("pipeline_notes", []), "ads": ads,
                         "notes_url": (b.get_url(notes_key, "Review Notes.md", attachment=True, expires=LINK_SECONDS)
                                       if notes_key else None),
                         "planning_cost": res.get("planning_cost"), "source_seconds": res.get("source_seconds"),
                         "planned_with": res.get("planned_with")}
    return out


# ---------------------------------------------------------------- serve

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if token_problem():
        log.error("%s (set it in Railway: web service, Variables)", token_problem())
    import uvicorn
    # no access log: every request path carries the link token, and Railway keeps its own request logs anyway
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips="*", log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
