"""Job control for the worker container (staging tests). Run it inside the container, where DATABASE_URL and the
bucket variables are set, for example `railway ssh -- sh -c 'cd /app/worker && python tools/jobctl.py status <id>'`
(the sh -c wrapper keeps Git Bash from rewriting /app paths):

    python tools/jobctl.py enqueue --job-id <uuid> --source-name IMG_3381.MOV [--note "..."]
    python tools/jobctl.py status <job_id>
    python tools/jobctl.py events <job_id>
    python tools/jobctl.py result <job_id>        the result without raw_plan
    python tools/jobctl.py list [--limit 20]
    python tools/jobctl.py spend                  every month's ledger totals

Upload the source first with tools/upload_source.py (from a PC, through `railway run`); it prints the job id.
`enqueue` only creates 'edit' jobs (a replan or rebuild needs a parent job, which the web app will provide).
Every command prints JSON.
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
    bucket = Bucket()
    head = bucket.s3.head_object(Bucket=bucket.name, Key=key)        # fails loudly if the upload is missing
    options = {"note": args.note} if args.note else {}
    with connect() as conn:
        conn.execute("insert into jobs (id, created_by, kind, source_key, source_name, options) "
                     "values (%s, 'cli', 'edit', %s, %s, %s)", (job_id, key, args.source_name, json.dumps(options)))
    return {"job_id": job_id, "source_key": key, "source_bytes": head["ContentLength"], "status": "queued"}


def status(args) -> dict:
    job_id = job_uuid(args.job_id)
    with connect() as conn:
        found = rows(conn, "select id, status, stage, stage_detail, attempts, max_attempts, error, cost_usd, created_at, "
                           "started_at, finished_at, locked_by, source_name from jobs where id = %s", (job_id,))
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


def list_jobs(args) -> list[dict]:
    with connect() as conn:
        return rows(conn, "select id, status, stage, attempts, source_name, cost_usd, created_at, finished_at from jobs "
                          "order by created_at desc limit %s", (args.limit,))


def spend(args) -> dict:
    with connect() as conn:
        months = rows(conn, "select key_name, month, cap_usd, spent, reserved from budget_months order by month desc")
        ledger = rows(conn, "select status, count(*) as entries, coalesce(sum(cost), 0) as cost, "
                            "coalesce(sum(reserved), 0) as reserved from spend_ledger group by status")
    return {"months": months, "ledger": ledger}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("enqueue")
    p.add_argument("--job-id", required=True)
    p.add_argument("--source-name", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(fn=enqueue)
    for name, fn in (("status", status), ("events", events), ("result", result)):
        p = sub.add_parser(name)
        p.add_argument("job_id")
        p.set_defaults(fn=fn)
    p = sub.add_parser("list")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=list_jobs)
    sub.add_parser("spend").set_defaults(fn=spend)
    args = ap.parse_args(argv)
    print(json.dumps(args.fn(args), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
