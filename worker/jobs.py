"""Cloud worker: takes queued jobs from Postgres, runs the ad pipeline, uploads results to the private bucket.

    python jobs.py              run forever (Railway start command)
    python jobs.py --once       process at most one job, then exit (testing)
    python jobs.py --selftest   check database, bucket and OpenRouter connectivity without spending anything

Environment (Railway variables):
    DATABASE_URL                       Postgres (private network)
    BUCKET, ACCESS_KEY_ID, SECRET_ACCESS_KEY, ENDPOINT, REGION     the media bucket
    OPENROUTER_VIDEO_AGENT_KEY         staff editing key ($100/month limit on OpenRouter)
    MONTHLY_BUDGET_USD                 our own ledger cap for that key (default 100)
    WORK_ROOT                          scratch space for jobs (default /tmp/va-jobs)
    HYPERFRAMES_DIR                    where the pinned HyperFrames install lives (default /app/hyperframes)
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import shutil
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import ad_cutter as ac
import llm
from budget import BudgetExceeded
from pg_budget import PostgresLedger, release_stale
from storage import Bucket

log = logging.getLogger("ad-cutter")
HERE = Path(__file__).resolve().parent
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
KEY_ENV, KEY_NAME = "OPENROUTER_VIDEO_AGENT_KEY", "video-agent"
POLL_SECONDS, HEARTBEAT_SECONDS, STALE_SECONDS = 5, 30, 600

CLAIM_SQL = """
update jobs set status = 'working', locked_by = %(worker)s, heartbeat_at = now(), attempts = attempts + 1,
       started_at = coalesce(started_at, now()), stage = 'starting', stage_detail = null, error = null
 where id = (select id from jobs where status = 'queued' and attempts < max_attempts
              order by created_at for update skip locked limit 1)
returning id::text, kind, parent_job::text, source_key, source_name, options, attempts, max_attempts
"""
REQUEUE_STALE_SQL = """
update jobs set status = case when attempts < max_attempts then 'queued' else 'failed' end,
       locked_by = null, stage = null,
       error = case when attempts < max_attempts then error
                    else 'The editor stopped while working on this video and the retries are used up.' end,
       finished_at = case when attempts < max_attempts then null else now() end
 where status = 'working' and heartbeat_at < now() - make_interval(secs => %s)
returning id::text, status
"""


class Stop(BaseException):
    """Raised in the main thread when Railway asks the worker to shut down. A BaseException, so the pipeline's
    per-ad `except Exception` handlers can't swallow it."""


# ---------------------------------------------------------------- setup

def lock_down_process() -> None:
    """On Linux, make /proc/<this pid>/environ unreadable to child processes running as the same user (ffmpeg,
    Chromium), so a crafted upload can't read our keys from there. Children reset this on exec, unaffected."""
    if sys.platform.startswith("linux"):
        PR_SET_DUMPABLE = 4
        if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE, 0) failed")


def connect():
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=15)


def worker_config() -> dict:
    cfg = ac.load_config(HERE / "config.json")
    cfg["work_dir"] = os.environ.get("WORK_ROOT", "/tmp/va-jobs")
    cfg["hyperframes_dir"] = os.environ.get("HYPERFRAMES_DIR", "/app/hyperframes")
    cfg["monthly_budget_usd"] = float(os.environ.get("MONTHLY_BUDGET_USD", cfg["monthly_budget_usd"]))
    cfg["openrouter_key_env"] = KEY_ENV
    return cfg


# ---------------------------------------------------------------- job helpers

def event(conn, job_id: str, message: str) -> None:
    conn.execute("insert into job_events (job_id, message) values (%s, %s)", (job_id, message[:500]))


def set_stage(conn, job_id: str, stage: str, detail: str = "") -> None:
    conn.execute("update jobs set stage = %s, stage_detail = %s, heartbeat_at = now() "
                 "where id = %s and locked_by = %s", (stage, detail or None, job_id, WORKER_ID))
    event(conn, job_id, f"{stage}{': ' + detail if detail else ''}")


def finish(conn, job_id: str, status: str, *, result: dict | None = None, error: str | None = None,
           no_retry: bool = False) -> None:
    # only the worker that holds the job may finish it (another worker may have taken it over after an outage)
    conn.execute("update jobs set status = %s, result = %s, error = %s, stage = null, stage_detail = null, "
                 "locked_by = null, finished_at = case when %s in ('ready', 'failed') then now() end, "
                 "max_attempts = case when %s then attempts else max_attempts end "
                 "where id = %s and locked_by = %s and status = 'working'",
                 (status, json.dumps(result) if result is not None else None, error, status, no_retry, job_id,
                  WORKER_ID))
    event(conn, job_id, f"{status}{': ' + error if error else ''}")


def build_result(run: dict, uploaded: dict[int, str], notes_key: str | None) -> dict:
    """What the web app shows on the Review page. Only data we produced (plan fields were validated)."""
    plan = run["plan"]
    ads = []
    for e in run["report"]:
        ad = e["ad"]
        ads.append({"k": e["k"], "name": ad["name"], "funnel_stage": ad.get("funnel_stage", ""),
                    "angle": ad.get("angle", ""), "headline": ad["headline"],
                    "callouts": [c["text"] for c in ad.get("callouts", [])], "primary_text": ad.get("primary_text", ""),
                    "seconds": round(e["len"], 1), "layout_check": (e["check"].splitlines() or ["not run"])[0],
                    "verify": e.get("verify"), "file_key": uploaded.get(e["k"]), "error": e.get("error")})
    return {"summary": plan.get("summary", ""), "ads": ads, "claims_to_review": plan.get("claims_to_review", []),
            "pipeline_notes": run["notes"], "notes_key": notes_key, "planning_cost": run["cost"],
            "source_seconds": round(run["duration"], 1)}


def plain_error(err: BaseException) -> str:
    text = str(err)
    return (text[:1].upper() + text[1:])[:500] if text else type(err).__name__


# ---------------------------------------------------------------- one job

def run_job(conn_factory, bucket: Bucket, job: dict, cfg: dict) -> str:
    job_id = job["id"]
    opts = job["options"] or {}
    work = Path(cfg["work_dir"]) / job_id
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, mode=0o700)
    stop_beat = threading.Event()

    def beat():
        while not stop_beat.wait(HEARTBEAT_SECONDS):
            try:
                with conn_factory() as c:
                    c.execute("update jobs set heartbeat_at = now() where id = %s and locked_by = %s", (job_id, WORKER_ID))
            except Exception as e:   # noqa: BLE001 - a missed heartbeat is not fatal
                log.warning("heartbeat failed: %s", e)

    threading.Thread(target=beat, daemon=True).start()

    def db(fn, *args, **kw):
        """Each database write uses its own short connection: a job can run for 20+ minutes."""
        with conn_factory() as c:
            return fn(c, *args, **kw)

    try:
        db(set_stage, job_id, "downloading")
        src = bucket.download(job["source_key"], work / "upload" / "source.bin")
        if job["kind"] == "rebuild" and job["parent_job"]:
            parent = db(lambda c: c.execute("select result from jobs where id = %s", (job["parent_job"],)).fetchone())
            raw = (parent[0] or {}).get("raw_plan") if parent else None
            if raw:
                (work / "pipeline").mkdir()
                (work / "pipeline" / "plan.json").write_text(json.dumps(raw), encoding="utf-8")
        client = llm.OpenRouter(KEY_ENV, PostgresLedger(conn_factory, KEY_NAME, cfg["monthly_budget_usd"], job_id))
        run = ac.run_pipeline(cfg, src, work / "pipeline", work / "out", client,
                              replan=job["kind"] == "replan", only=opts.get("only"), note=str(opts.get("note", "")),
                              progress=lambda stage, detail="": db(set_stage, job_id, stage, detail))
        db(set_stage, job_id, "uploading")
        uploaded = {}
        for e in run["report"]:
            if e.get("file"):
                uploaded[e["k"]] = bucket.upload(work / "out" / e["file"], Bucket.result_key(job_id, e["file"]),
                                                 "video/mp4")
        notes = sorted((work / "out").glob("Review Notes*.md"))
        notes_key = bucket.upload(notes[-1], Bucket.result_key(job_id, "Review Notes.md"), "text/markdown")             if notes else None
        result = build_result(run, uploaded, notes_key)
        result["raw_plan"] = json.loads((work / "pipeline" / "plan.json").read_text(encoding="utf-8"))
        if uploaded:
            db(finish, job_id, "ready", result=result)
            return "ready"
        db(finish, job_id, "failed", result=result, no_retry=True,
           error="None of the ads could be rendered. See the notes for each ad.")
        return "failed"
    except Stop:
        with conn_factory() as conn:          # hand the job back without using up an attempt
            conn.execute("update jobs set status = 'queued', attempts = greatest(attempts - 1, 0), locked_by = null, "
                         "stage = null where id = %s and locked_by = %s and status = 'working'", (job_id, WORKER_ID))
            event(conn, job_id, "worker restarting; job put back in the queue")
        raise
    except (ac.AdCutterError, BudgetExceeded) as err:        # a problem with this video or the budget: don't retry
        with conn_factory() as conn:
            finish(conn, job_id, "failed", error=plain_error(err), no_retry=True)
        return "failed"
    except Exception as err:                                 # noqa: BLE001 - unexpected: retry if attempts remain
        log.exception("job %s crashed", job_id)
        with conn_factory() as conn:
            retry = job["attempts"] < job["max_attempts"]
            finish(conn, job_id, "queued" if retry else "failed",
                   error=None if retry else f"Something went wrong while editing: {plain_error(err)}")
        return "retry" if retry else "failed"
    finally:
        stop_beat.set()
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- loop

def claim(conn) -> dict | None:
    row = conn.execute(CLAIM_SQL, {"worker": WORKER_ID}).fetchone()
    if not row:
        return None
    keys = ("id", "kind", "parent_job", "source_key", "source_name", "options", "attempts", "max_attempts")
    return dict(zip(keys, row))


def serve(once: bool = False) -> None:
    cfg = worker_config()
    bucket = Bucket()
    stopping = threading.Event()

    def on_term(signum, frame):
        stopping.set()
        raise Stop()

    signal.signal(signal.SIGTERM, on_term)
    log.info("worker %s ready", WORKER_ID)
    last_sweep = last_release = 0.0
    while not stopping.is_set():
        try:
            with connect() as conn:
                if time.time() - last_sweep > 60:
                    for jid, status in conn.execute(REQUEUE_STALE_SQL, (STALE_SECONDS,)).fetchall():
                        log.warning("job %s had no heartbeat; now %s", jid, status)
                    last_sweep = time.time()
                if time.time() - last_release > 3600:       # spend reservations left by a crashed worker
                    released = release_stale(connect)
                    if released:
                        log.warning("released %s stale spend reservation(s) at their worst-case cost", released)
                    last_release = time.time()
                job = claim(conn)
            if job:
                log.info("job %s (%s): %s, attempt %s", job["id"], job["kind"], job["source_name"], job["attempts"])
                log.info("job %s finished: %s", job["id"], run_job(connect, bucket, job, cfg))
                if once:
                    return
            elif once:
                return
            else:
                time.sleep(POLL_SECONDS)
        except Stop:
            log.info("stopping")
            return
        except Exception:                     # noqa: BLE001 - keep the worker alive through DB blips
            log.exception("worker loop error; retrying shortly")
            time.sleep(POLL_SECONDS * 3)


def selftest() -> int:
    """Connectivity checks that spend nothing: database, bucket, OpenRouter key limit, tools."""
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            log.info("OK   %s: %s", name, fn())
        except Exception as e:                # noqa: BLE001
            ok = False
            log.error("FAIL %s: %s", name, e)

    check("database", lambda: connect().execute("select count(*) from jobs").fetchone()[0])
    check("bucket", lambda: Bucket().s3.list_objects_v2(Bucket=Bucket().name, MaxKeys=1).get("KeyCount", 0))
    def key_check():
        remaining = llm.OpenRouter(KEY_ENV, None).key_remaining()
        if remaining is None:
            raise RuntimeError("the key has no monthly limit on OpenRouter; set one before using it")
        return f"${remaining:.2f} left this month"
    check("openrouter key", key_check)
    check("ffmpeg", lambda: shutil.which("ffmpeg"))
    check("whisper model", whisper_loads_offline)
    check("browser has no internet", browser_is_offline)
    check("render", smoke_render)
    return 0 if ok else 1


def browser_is_offline() -> str:
    """Ask the render browser (through the offline wrapper) to load a public page; it must fail."""
    import subprocess
    from safe_media import clean_env
    browser = os.environ.get("HYPERFRAMES_BROWSER_PATH", "")
    if not browser:
        raise RuntimeError("HYPERFRAMES_BROWSER_PATH is not set")
    for url, marker in (("https://example.com/", "example domain"), ("http://1.1.1.1/", "cloudflare")):
        res = subprocess.run([browser, "--headless", "--no-sandbox", "--dump-dom", url],
                             capture_output=True, text=True, timeout=60, env=clean_env())
        if marker in res.stdout.lower():
            raise RuntimeError(f"the render browser could load {url}: network block is NOT working")
    return "public sites and raw IP addresses unreachable, as intended"


def whisper_loads_offline() -> str:
    from faster_whisper import WhisperModel
    WhisperModel(worker_config()["whisper_model"], device="cpu", compute_type="int8")
    return f"{worker_config()['whisper_model']} loaded from the image (HF_HUB_OFFLINE={os.environ.get('HF_HUB_OFFLINE')})"


def smoke_render() -> str:
    """Render a one-second composition with the vendored fonts and GSAP: proves Chrome, HyperFrames and the
    offline assets work in this container."""
    cfg = worker_config()
    project = Path(cfg["work_dir"]) / "selftest-render"
    shutil.rmtree(project, ignore_errors=True)
    project.mkdir(parents=True)
    shutil.copytree(HERE / "template" / "vendor", project / "vendor", ignore=shutil.ignore_patterns("licenses", "*.md"))
    (project / "index.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="UTF-8"><link rel="stylesheet" href="vendor/fonts.css">'
        '<script src="vendor/gsap-3.14.2.min.js"></script><style>html,body{margin:0;width:1080px;height:1920px;'
        'background:#0071E3}#t{position:absolute;top:900px;width:100%;text-align:center;color:#fff;'
        'font:900 90px Montserrat}</style></head><body><div id="root" data-composition-id="main" data-start="0" '
        'data-duration="1" data-width="1080" data-height="1920"><div id="t" class="clip" data-start="0" '
        'data-duration="1" data-track-index="1">SELF TEST</div></div><script>const tl=gsap.timeline({paused:true});'
        'tl.fromTo("#t",{opacity:0},{opacity:1,duration:0.5},0);window.__timelines={main:tl};tl.seek(0);</script>'
        '</body></html>', encoding="utf-8")
    (project / "meta.json").write_text(json.dumps({"id": "selftest", "name": "selftest"}), encoding="utf-8")
    started = time.time()
    try:
        ac.render({**cfg, "render_quality": "draft"}, project, project / "out.mp4")
        size = (project / "out.mp4").stat().st_size
    finally:
        shutil.rmtree(project, ignore_errors=True)
    return f"1-second test video rendered in {time.time() - started:.0f}s ({size // 1024} KB)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lock_down_process()
    if args.selftest:
        return selftest()
    serve(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
