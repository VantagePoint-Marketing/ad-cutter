"""The Video Agent page: upload clips, say what you want, get ads. One shared link, no accounts.

    python app.py                      serve on $PORT (Railway) or 8000

Environment (Railway variables, declared in .railway/railway.ts):
    APP_LINK_TOKEN                     the secret part of the link, https://<domain>/<token>/  (set by hand)
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

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))      # storage.py is shared with the worker (copied into the image)
from storage import MAX_CLIPS, Bucket  # noqa: E402

log = logging.getLogger("video-agent-web")
TOKEN = os.environ.get("APP_LINK_TOKEN", "").strip()
MAX_CLIP_BYTES = 4 * 1024 ** 3           # the worker refuses larger downloads
MAX_JOB_BYTES = 10 * 1024 ** 3           # all clips together (10 minutes of 4K phone footage is about 4 GB)
MAX_BRIEF = 2000                         # characters of the request that reach Gemini (worker: ad_cutter.MAX_BRIEF)
LINK_SECONDS = 3600                      # how long signed preview/download links stay valid
UPLOAD_LINK_SECONDS = 6 * 3600           # upload links last longer: several big clips on a home connection
DAILY_JOBS = 30                          # jobs the page accepts per rolling day (worker time is not capped elsewhere)
RECENT = 30
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


def link(token: str) -> str:
    """The secret part of the link is the whole access control. Anything else is a plain 404."""
    if token_problem() or not hmac.compare_digest(token.encode("utf-8", "replace"), TOKEN.encode("utf-8")):
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
    return {"ok": True, "link": token_problem() or "set", "uploads": STATE["uploads"]}


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


@app.post("/{token}/api/jobs")
def create_job(body: NewJob, token: str = Depends(link)) -> dict:
    """A new job in 'uploading', with one signed upload link per clip. Nothing runs until /start."""
    brief = body.brief.strip()
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
    with connect() as conn:
        today = conn.execute("select count(*) from jobs where created_by = 'web' and created_at > now() - "
                             "interval '1 day'").fetchone()[0]
        if int(today or 0) >= DAILY_JOBS:
            raise HTTPException(429, f"The editor has taken {DAILY_JOBS} jobs in the last 24 hours, which is its "
                                     "daily limit. Please try again tomorrow.")
        conn.execute("insert into jobs (id, created_by, kind, status, source_key, source_name, sources, options) "
                     "values (%s, 'web', 'edit', 'uploading', %s, %s, %s, %s)",
                     (job_id, sources[0]["key"], label, json.dumps(sources), json.dumps({"brief": brief})))
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


@app.get("/{token}/api/jobs/{job_id}")
def job_status(job_id: uuid.UUID, token: str = Depends(link)) -> dict:
    return job_view(str(job_id))


@app.get("/{token}/api/jobs")
def recent_jobs(token: str = Depends(link)) -> dict:
    with connect() as conn:
        found = rows(conn, "select id::text as job_id, status, stage, stage_detail, error, created_at, finished_at, "
                           "source_name as label, options->>'brief' as brief, jsonb_array_length(sources) as clips "
                           "from jobs where created_by = 'web' and status <> 'uploading' "
                           "order by created_at desc limit %s", (RECENT,))
    return {"jobs": [{**r, "created_at": iso(r["created_at"]), "finished_at": iso(r["finished_at"])} for r in found]}


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
    return present(row, int(ahead or 0))


def present(row: dict, ahead: int = 0) -> dict:
    """What the page shows: the job's state in plain fields, and when it is done, the ads with signed links."""
    opts = row.get("options") or {}
    out = {"job_id": row["job_id"], "status": row["status"], "stage": row.get("stage"),
           "stage_detail": row.get("stage_detail"), "error": row.get("error"), "brief": opts.get("brief", ""),
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
                        "preview_url": b.get_url(key, name, expires=LINK_SECONDS) if key else None,
                        "download_url": b.get_url(key, name, attachment=True, expires=LINK_SECONDS) if key else None})
        notes_key = res.get("notes_key")
        out["result"] = {"summary": res.get("summary", ""), "response_to_request": res.get("response_to_request", ""),
                         "claims_to_review": res.get("claims_to_review", []),
                         "pipeline_notes": res.get("pipeline_notes", []), "ads": ads,
                         "notes_url": (b.get_url(notes_key, "Review Notes.md", attachment=True, expires=LINK_SECONDS)
                                       if notes_key else None),
                         "planning_cost": res.get("planning_cost"), "source_seconds": res.get("source_seconds")}
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
