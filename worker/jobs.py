"""Cloud worker: takes queued jobs from Postgres, runs the ad pipeline, uploads results to the private bucket.

    python jobs.py              run forever (Railway start command)
    python jobs.py --once       process at most one job, then exit (testing)
    python jobs.py --selftest   check database, bucket and OpenRouter connectivity without spending anything

Environment (Railway variables):
    DATABASE_URL                       Postgres (private network)
    BUCKET, ACCESS_KEY_ID, SECRET_ACCESS_KEY, ENDPOINT, REGION     the media bucket
    OPENROUTER_VIDEO_AGENT_KEY         staff editing key ($100/month limit on OpenRouter)
    MONTHLY_BUDGET_USD                 our own ledger cap for that key (default 100)
    LIBRARY_ENABLED, GEMINI_API_KEY, YOUTUBE_API_KEY    the reference library (library.py), studied while idle
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
import feedback
import hub_import
import library
import llm
import models
import review
from budget import BudgetExceeded
from pg_budget import PostgresLedger, release_stale
from storage import Bucket

log = logging.getLogger("ad-cutter")
HERE = Path(__file__).resolve().parent
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
KEY_ENV, KEY_NAME = "OPENROUTER_VIDEO_AGENT_KEY", "video-agent"
POLL_SECONDS, HEARTBEAT_SECONDS, STALE_SECONDS = 5, 30, 600
MAX_JOB_BYTES = 10 * 1024 ** 3        # all of a job's clips together (the web page applies the same limit)

CLAIM_SQL = """
update jobs set status = 'working', locked_by = %(worker)s, heartbeat_at = now(), attempts = attempts + 1,
       started_at = coalesce(started_at, now()), stage = 'starting', stage_detail = null, error = null
 where id = (select id from jobs where status = 'queued' and attempts < max_attempts
              order by created_at for update skip locked limit 1)
returning id::text, kind, parent_job::text, source_key, source_name, options, attempts, max_attempts, sources
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


class Cancelled(BaseException):
    """Raised at the next progress report once the page has cancelled the job (or another worker owns it now). Also a
    BaseException, so no per-ad or self-check handler can swallow it."""


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


def set_stage(conn, job_id: str, stage: str, detail: str = "") -> bool:
    """Record what the job is doing. False when it is no longer this worker's running job (the page cancelled it, or
    another worker took it over): the caller stops, so no more money is spent on it."""
    done = conn.execute("update jobs set stage = %s, stage_detail = %s, heartbeat_at = now() "
                        "where id = %s and locked_by = %s and status = 'working'",
                        (stage, detail or None, job_id, WORKER_ID))
    if getattr(done, "rowcount", 1) == 0:
        return False
    event(conn, job_id, f"{stage}{': ' + detail if detail else ''}")
    return True


def finish(conn, job_id: str, status: str, *, result: dict | None = None, error: str | None = None,
           no_retry: bool = False) -> None:
    # only the worker that holds the job may finish it (another worker may have taken it over after an outage)
    done = conn.execute("update jobs set status = %s, result = %s, error = %s, stage = null, stage_detail = null, "
                        "locked_by = null, finished_at = case when %s in ('ready', 'failed') then now() end, "
                        "max_attempts = case when %s then attempts else max_attempts end "
                        "where id = %s and locked_by = %s and status = 'working'",
                        (status, json.dumps(result) if result is not None else None, error, status, no_retry, job_id,
                         WORKER_ID))
    if getattr(done, "rowcount", 1) == 0:       # cancelled or taken over meanwhile: nothing changed, so nothing to log
        return
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
                    "verify": e.get("verify"), "file_key": uploaded.get(e["k"]), "error": e.get("error"),
                    "spoken": e.get("spoken", ""), "review": e.get("review")})
    return {"summary": plan.get("summary", ""), "response_to_request": plan.get("response_to_request", ""),
            "ads": ads, "claims_to_review": plan.get("claims_to_review", []),
            "pipeline_notes": run["notes"], "notes_key": notes_key, "planning_cost": run["cost"],
            "review_cost": run.get("review_cost", 0.0),
            "source_seconds": round(run["duration"], 1), "clips": run.get("clips", [])}


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

    def progress(stage: str, detail: str = "") -> None:
        if not db(set_stage, job_id, stage, detail):
            raise Cancelled()

    try:
        progress("downloading")
        # the clips, in the order the person gave them; older rows have only source_key
        clips = job.get("sources") or [{"key": job["source_key"], "name": job["source_name"]}]
        declared = sum(int(clip.get("bytes") or 0) for clip in clips)
        if declared > MAX_JOB_BYTES:
            raise ac.AdCutterError(f"the clips add up to {declared / 1024 ** 3:.1f} GB; the limit is "
                                   f"{MAX_JOB_BYTES // 1024 ** 3} GB per job")
        # a job may only read its own uploads; a replan or rebuild may also read its parent's
        owners = [job_id] + ([job["parent_job"]] if job["kind"] in ("replan", "rebuild") and job["parent_job"] else [])
        srcs, names = [], []
        for n, clip in enumerate(clips):
            key = str(clip.get("key", ""))
            if not any(Bucket.owns(owner, key) for owner in owners):
                raise ac.AdCutterError(f"clip {n + 1} is not one of this job's uploads ({key})")
            try:
                srcs.append(bucket.download(key, work / "upload" / f"clip-{n}.bin"))
            except ValueError as err:                   # over the size limit: a problem with the upload, no retry
                raise ac.AdCutterError(f"clip {n + 1}: {err}") from err
            names.append(str(clip.get("name") or f"clip {n + 1}"))
        if job["kind"] == "rebuild" and job["parent_job"]:
            parent = db(lambda c: c.execute("select result from jobs where id = %s", (job["parent_job"],)).fetchone())
            raw = (parent[0] or {}).get("raw_plan") if parent else None
            if raw:
                (work / "pipeline").mkdir()
                (work / "pipeline" / "plan.json").write_text(json.dumps(raw), encoding="utf-8")
        client = llm.OpenRouter(KEY_ENV, PostgresLedger(conn_factory, KEY_NAME, cfg["monthly_budget_usd"], job_id))
        # the end-screen wording the person chose for this batch (the page checked and trimmed it)
        brand = {k: str(opts[k]) for k in ("cta_line", "cta_button") if isinstance(opts.get(k), str) and opts[k].strip()}
        run_cfg = {**cfg, "brand": {**cfg.get("brand", {}), **brand}} if brand else dict(cfg)
        # the AI model and effort the person picked on the page, checked against models.py (never passed on unchecked);
        # a job that picked nothing keeps the worker's configured model
        picked = opts.get("model") is not None or opts.get("effort") is not None
        model_key, model_id, effort = models.resolve(opts.get("model"), opts.get("effort"))
        if picked:
            if opts.get("model") is not None:
                run_cfg["plan_model"] = model_id
            run_cfg["plan_effort"] = effort
        run = ac.run_pipeline(run_cfg, srcs, work / "pipeline", work / "out", client,
                              replan=job["kind"] == "replan", only=opts.get("only"),
                              brief=str(opts.get("brief") or opts.get("note") or ""), names=names,
                              progress=progress, team_notes=db(feedback.team_notes))
        progress("uploading")
        uploaded = {}
        for e in run["report"]:
            if e.get("file"):
                uploaded[e["k"]] = bucket.upload(work / "out" / e["file"], Bucket.result_key(job_id, e["file"]),
                                                 "video/mp4")
        notes = sorted((work / "out").glob("Review Notes*.md"))
        notes_key = bucket.upload(notes[-1], Bucket.result_key(job_id, "Review Notes.md"), "text/markdown")             if notes else None
        result = build_result(run, uploaded, notes_key)
        used = run_cfg.get("plan_model", "")
        result["planned_with"] = {"model": used, "effort": run_cfg.get("plan_effort", "medium"),
                                  "model_name": next((m["tech"] for m in models.MODELS.values()
                                                      if m["openrouter"] == used), used)}
        result["raw_plan"] = json.loads((work / "pipeline" / "plan.json").read_text(encoding="utf-8"))
        if uploaded:
            db(finish, job_id, "ready", result=result)
            return "ready"
        db(finish, job_id, "failed", result=result, no_retry=True,
           error="None of the ads could be rendered. See the notes for each ad.")
        return "failed"
    except Cancelled:                         # the page cancelled it: nothing to hand back, nothing to retry
        log.info("job %s cancelled; stopped at its next step", job_id)
        return "cancelled"
    except Stop:
        with conn_factory() as conn:          # hand the job back without using up an attempt
            done = conn.execute("update jobs set status = 'queued', attempts = greatest(attempts - 1, 0), locked_by = null, "
                                "stage = null where id = %s and locked_by = %s and status = 'working'", (job_id, WORKER_ID))
            if getattr(done, "rowcount", 1) != 0:
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
    keys = ("id", "kind", "parent_job", "source_key", "source_name", "options", "attempts", "max_attempts", "sources")
    return dict(zip(keys, row))


def serve(once: bool = False) -> None:
    cfg = worker_config()
    bucket = Bucket()
    stopping = threading.Event()

    def on_term(signum, frame):
        stopping.set()
        raise Stop()

    signal.signal(signal.SIGTERM, on_term)
    lib = library.Runner(connect, cfg, worker_id=WORKER_ID) if library.enabled() else None
    # the knowledge hub fills and links itself a little at a time while no job waits (no outside calls, no spend); HUB_ENABLED=0 turns it off
    hub_m = hub_import.Maintainer(connect) if os.environ.get("HUB_ENABLED", "1").strip() != "0" else None
    log.info("worker %s ready; library %s", WORKER_ID, "on" if lib else f"off ({library.why_off()})")
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
            elif hub_m and hub_m.ready():             # no editing job waiting: keep the hub filled and linked
                log.info("hub: %s", hub_m.step())
            elif lib and lib.ready():                 # ... or study one part of one video
                log.info("library: %s", lib.step())
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
    check("working copy chain", prepare_chain_works)
    check("ad body chain", body_chain_renders)
    check("self-check copy", review_copy_works)
    check("hdr conversion", hdr_chain_works)
    check("whisper model", whisper_loads_offline)
    check("browser has no internet", browser_is_offline)
    check("render", smoke_render)
    check("library", library_check)
    return 0 if ok else 1


def library_check() -> str:
    """Whether the library is on, and if so that both keys work: one free Gemini call and 1 YouTube quota unit."""
    off = library.why_off()
    if off:
        return f"off ({off})"
    import gemini_free
    import youtube
    gemini = gemini_free.GeminiFree().key_ok()
    seen = youtube.videos(["jNQXAC9IVRw"])
    if "jNQXAC9IVRw" not in seen:
        raise RuntimeError("the YouTube key answered but returned no video")
    with connect() as conn:
        counts = dict(conn.execute("select status, count(*) from ref_sources group by status").fetchall())
    return f"on; Gemini {gemini}; YouTube key works; videos by status: {counts or 'none yet'}"


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


def body_chain_renders() -> str:
    """Cut two pieces out of a generated stereo clip and a mono one through the real ad body chain. Filter
    negotiation differs between ffmpeg versions (5.1 rejected the chain once), and a PC test can't catch that."""
    work = Path(worker_config()["work_dir"]) / "selftest-body"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    sizes = []
    try:
        for layout in ("stereo", "mono"):
            src, dest = work / f"{layout}.mp4", work / f"{layout}-body.mp4"
            ac.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
                    "-af", f"aformat=channel_layouts={layout}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", str(src)], timeout=120)
            ac.render_body(src, [ac.Interval(0.5, 1.5, 0.0, 0), ac.Interval(2.0, 3.0, 1.0, 0)], body_len=2.0,
                           cta=1.0, dest=dest)
            sizes.append(f"{layout} {dest.stat().st_size // 1024} KB")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return ", ".join(sizes)


def hdr_chain_works() -> str:
    """Run the HDR-to-standard conversion (ad_cutter.hdr_to_sdr) on a generated clip that is tagged as iPhone HDR (HLG).
    If this ffmpeg has no zscale filter the check says so; real HDR clips then use the simpler tag-only fallback."""
    work = Path(worker_config()["work_dir"]) / "selftest-hdr"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        dest = work / "out.mp4"
        ac.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=540x960:rate=30:duration=1",
                "-vf", "format=yuv420p10le,setparams=colorspace=bt2020nc:color_primaries=bt2020:color_trc=arib-std-b67:range=tv,"
                       + ac.hdr_to_sdr("arib-std-b67"), "-c:v", "libx264", "-preset", "ultrafast", str(dest)], timeout=120)
        return f"HLG converted to standard video ({dest.stat().st_size // 1024} KB)"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def review_copy_works() -> str:
    """Make the small copy the self-check sends to Gemini (review.make_proxy) from a generated finished-ad-sized
    clip. Filter and encoder negotiation differs between ffmpeg versions (5.1 in this image, 8 on the PC)."""
    work = Path(worker_config()["work_dir"]) / "selftest-review"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        src = work / "ad.mp4"
        ac.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], timeout=120)
        small = review.make_proxy(src, work / "small.mp4")
        return f"{small.stat().st_size // 1024} KB for a 3-second ad"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def prepare_chain_works() -> str:
    """Join two generated clips (mono and stereo, different sizes and rates) through the real working-copy chain,
    then make the wav and the Gemini proxy from the result. Filter negotiation differs between ffmpeg versions
    (5.1 in this image, 8 on the PC), and a PC test can't prove the container accepts the chain."""
    work = Path(worker_config()["work_dir"]) / "selftest-prepare"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        srcs = []
        for n, (layout, size, rate) in enumerate((("mono", "320x568", 44100), ("stereo", "640x360", 48000))):
            src = work / f"in{n}.mp4"
            ac.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration=2",
                    "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration=2",
                    "-af", f"aformat=channel_layouts={layout}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], timeout=120)
            srcs.append(src)
        media = ac.prepare(srcs, work / "pipeline", max_seconds=60, names=["mono.mp4", "stereo.mp4"])
        if not 3.5 <= media["duration"] <= 4.5 or len(media["clips"]) != 2:
            raise RuntimeError(f"joined {len(media['clips'])} clips into {media['duration']:.1f}s; expected 2 clips, 4s")
        return f"2 clips joined into {media['duration']:.1f}s, proxy {media['proxy'].stat().st_size // 1024} KB"
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
