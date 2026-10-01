"""Job control for the worker container (staging tests). Run it inside the container, where DATABASE_URL and the
bucket variables are set, for example `railway ssh -- sh -c 'cd /app/worker && python tools/jobctl.py status <id>'`
(the sh -c wrapper keeps Git Bash from rewriting /app paths):

    python tools/jobctl.py enqueue --job-id <uuid> --source-name IMG_3381.MOV [--brief "what you want"]
    python tools/jobctl.py status <job_id>
    python tools/jobctl.py events <job_id>
    python tools/jobctl.py result <job_id>        the result without raw_plan
    python tools/jobctl.py requeue <job_id>       run a failed job again, e.g. after a pipeline fix
    python tools/jobctl.py list [--limit 20]
    python tools/jobctl.py spend                  every month's ledger totals
    python tools/jobctl.py library                what the reference library has studied (one row per video)
    python tools/jobctl.py lessons <video id>     every kept lesson of one video, in order
    python tools/jobctl.py library-retry <video id | failed>   put one failed video, or all of them, back in line

Upload the source first with tools/upload_source.py (from a PC, through `railway run`); it prints the job id.
`enqueue` only creates single-clip 'edit' jobs; the web page creates multi-clip ones. Every command prints JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import Bucket  # noqa: E402


def connect():
    import os
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=15)


def rows(conn, sql, params=()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def job_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as err:
        raise SystemExit(f"not a job id: {value!r}") from err


def enqueue(args) -> dict:
    job_id = job_uuid(args.job_id)
    key = Bucket.upload_key(job_id)
    size = Bucket().size(key)
    if size is None:
        raise SystemExit(f"nothing uploaded at {key}; run tools/upload_source.py first")
    options = {"brief": args.brief} if args.brief else {}
    sources = [{"key": key, "name": args.source_name, "bytes": size}]
    with connect() as conn:
        conn.execute("insert into jobs (id, created_by, kind, source_key, source_name, sources, options) "
                     "values (%s, 'cli', 'edit', %s, %s, %s, %s)",
                     (job_id, key, args.source_name, json.dumps(sources), json.dumps(options)))
    return {"job_id": job_id, "source_key": key, "source_bytes": size, "status": "queued"}


def status(args) -> dict:
    job_id = job_uuid(args.job_id)
    with connect() as conn:
        found = rows(conn, "select id, status, stage, stage_detail, attempts, max_attempts, error, cost_usd, created_at, "
                           "started_at, finished_at, locked_by, source_name, jsonb_array_length(sources) as clips, "
                           "options->>'brief' as brief from jobs where id = %s", (job_id,))
    return found[0] if found else {"error": "no such job"}


def events(args) -> list[dict]:
    job_id = job_uuid(args.job_id)
    with connect() as conn:
        return rows(conn, "select at, message from job_events where job_id = %s order by at", (job_id,))


def result(args) -> dict:
    job_id = job_uuid(args.job_id)
    with connect() as conn:
        found = rows(conn, "select result from jobs where id = %s", (job_id,))
    if not found or found[0]["result"] is None:
        return {"error": "no result yet"}
    res = dict(found[0]["result"])
    res.pop("raw_plan", None)
    return res


def requeue(args) -> dict:
    """Put a failed job back in the queue with a fresh attempt budget, as a new run from scratch: the clips must
    still be in the bucket, the old result is dropped, and Gemini plans again (about $0.10 to $0.25)."""
    job_id = job_uuid(args.job_id)
    bucket = Bucket()
    with connect() as conn:
        found = rows(conn, "select status, source_key, sources from jobs where id = %s", (job_id,))
    if not found:
        return {"error": "no such job"}
    if found[0]["status"] != "failed":
        return {"error": f"the job is {found[0]['status']}; only failed jobs can be requeued"}
    keys = [s["key"] for s in (found[0]["sources"] or [])] or [found[0]["source_key"]]
    missing = [k for k in keys if bucket.size(k) is None]
    if missing:
        return {"error": f"the upload is no longer in the bucket: {', '.join(missing)}"}
    with connect() as conn, conn.transaction():
        done = rows(conn, "update jobs set status = 'queued', attempts = 0, max_attempts = default, stage = null, "
                          "stage_detail = null, locked_by = null, heartbeat_at = null, started_at = null, "
                          "finished_at = null, error = null, result = null "
                          "where id = %s and status = 'failed' returning id", (job_id,))
        if not done:
            return {"error": "the job changed status while requeueing; look at it again"}
        conn.execute("insert into job_events (job_id, message) values (%s, 'requeued by cli')", (job_id,))
    return {"job_id": job_id, "status": "queued"}


def list_jobs(args) -> list[dict]:
    with connect() as conn:
        return rows(conn, "select id, status, stage, attempts, created_by, source_name, jsonb_array_length(sources) as clips, "
                          "cost_usd, created_at, finished_at from jobs order by created_at desc limit %s", (args.limit,))


def spend(args) -> dict:
    with connect() as conn:
        months = rows(conn, "select key_name, month, cap_usd, spent, reserved from budget_months order by month desc")
        ledger = rows(conn, "select status, count(*) as entries, coalesce(sum(cost), 0) as cost, "
                            "coalesce(sum(reserved), 0) as reserved from spend_ledger group by status")
    return {"months": months, "ledger": ledger}


def library_status(args) -> dict:
    with connect() as conn:
        videos = rows(conn, "select s.external_id as id, s.status, round(s.seconds / 60.0, 1) as minutes, s.attempts, "
                            "left(s.title, 60) as title, s.reason, "
                            "(select count(distinct n.window_start) from ref_notes n "
                            " where n.source_id = s.id and n.reference_ok) as parts_ok, "
                            "(select count(*) from ref_notes n where n.source_id = s.id and not n.reference_ok) "
                            "as rejected "
                            "from ref_sources s order by s.tier, s.id")
        today = rows(conn, "select api, used from api_quota where period = to_char(now() at time zone "
                           "'America/Los_Angeles', 'YYYY-MM-DD')")
    return {"today": today, "videos": videos}


def lessons(args) -> list[dict]:
    with connect() as conn:
        notes = rows(conn, "select distinct on (n.window_start) n.window_start, n.model, n.note from ref_notes n "
                           "join ref_sources s on s.id = n.source_id where s.external_id = %s and n.reference_ok "
                           "order by n.window_start, n.created_at desc", (args.video_id,))
    out = []
    for n in notes:
        for lesson in n["note"].get("lessons", []):
            out.append({"at": f"{int(lesson['at_s']) // 60}:{int(lesson['at_s']) % 60:02d}", "lever": lesson["lever"],
                        "topic": lesson["topic"], "principle": lesson["principle"], "model": n["model"]})
    return out


def library_retry(args) -> dict:
    """Failed library videos back to 'new' with a fresh attempt count (after fixing whatever made them fail)."""
    with connect() as conn:
        if args.target == "failed":
            done = rows(conn, "update ref_sources set status = 'new', attempts = 0, reason = null, updated_at = now() "
                              "where status = 'failed' returning external_id")
        else:
            done = rows(conn, "update ref_sources set status = 'new', attempts = 0, reason = null, updated_at = now() "
                              "where status = 'failed' and external_id = %s returning external_id", (args.target,))
    return {"requeued": [r["external_id"] for r in done]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("enqueue")
    p.add_argument("--job-id", required=True)
    p.add_argument("--source-name", required=True)
    p.add_argument("--brief", default="", help="what the person wants from the footage (goes to Gemini)")
    p.set_defaults(fn=enqueue)
    for name, fn in (("status", status), ("events", events), ("result", result), ("requeue", requeue)):
        p = sub.add_parser(name)
        p.add_argument("job_id")
        p.set_defaults(fn=fn)
    p = sub.add_parser("list")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=list_jobs)
    sub.add_parser("spend").set_defaults(fn=spend)
    sub.add_parser("library").set_defaults(fn=library_status)
    p = sub.add_parser("lessons")
    p.add_argument("video_id")
    p.set_defaults(fn=lessons)
    p = sub.add_parser("library-retry")
    p.add_argument("target", help="a YouTube video id, or 'failed' for every failed video")
    p.set_defaults(fn=library_retry)
    args = ap.parse_args(argv)
    print(json.dumps(args.fn(args), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
