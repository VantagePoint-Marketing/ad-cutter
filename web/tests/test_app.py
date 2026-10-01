"""The web page's API with a fake database and bucket (no network, no Postgres)."""
import datetime as dt
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as web  # noqa: E402

TOKEN = "test-link-token-1234567890"
UUID = re.compile(r"^[0-9a-f-]{36}$")


class Cursor:
    def __init__(self, cols, rows):
        self.description = [type("C", (), {"name": c})() for c in cols]
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeDB:
    """Understands exactly the statements app.py issues, against an in-memory table of jobs."""

    def __init__(self):
        self.jobs, self.events = {}, []

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _row(self, job, cols):
        values = {**job, "job_id": job["id"], "label": job["source_name"], "brief": job["options"].get("brief"),
                  "clips": len(job["sources"])}
        return tuple(values[c] for c in cols)

    @staticmethod
    def _cols(sql):
        select = sql.split(" from ")[0][len("select "):]
        return [re.sub(r"^.* as ", "", c.strip()) for c in select.split(", ")]

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        if sql.startswith("insert into jobs"):
            job_id, source_key, label, sources, options = params
            self.jobs[job_id] = {"id": job_id, "status": "uploading", "stage": None, "stage_detail": None, "error": None,
                                 "created_at": dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc),
                                 "started_at": None, "finished_at": None, "source_name": label, "source_key": source_key,
                                 "sources": json.loads(sources), "options": json.loads(options), "result": None,
                                 "cost_usd": 0, "created_by": "web"}
            return Cursor([], [])
        if sql.startswith("insert into job_events"):
            self.events.append(params)
            return Cursor([], [])
        if sql.startswith("update jobs set status = 'queued'"):
            job = self.jobs.get(params[0])
            if job and job["status"] == "uploading":
                job["status"] = "queued"
            return Cursor([], [])
        if sql.startswith("select count(*)"):
            return Cursor(["count"], [(2,)])
        cols = self._cols(sql)
        if "where id = %s" in sql:
            job = self.jobs.get(params[0])
            return Cursor(cols, [self._row(job, cols)] if job and job["created_by"] == "web" else [])
        if "order by created_at desc" in sql:
            listed = [j for j in self.jobs.values() if j["status"] != "uploading"]
            return Cursor(cols, [self._row(j, cols) for j in listed])
        raise AssertionError(f"unexpected SQL: {sql}")


class FakeBucket:
    def __init__(self):
        self.sizes, self.origins = {}, None

    def put_url(self, key, expires):
        return f"https://bucket.test/{key}?put&expires={expires}"

    def get_url(self, key, filename=None, attachment=False, expires=3600):
        return f"https://bucket.test/{key}?{'download' if attachment else 'inline'}&name={filename}"

    def size(self, key):
        return self.sizes.get(key)

    def allow_browser_uploads(self, origins):
        self.origins = list(origins)


@pytest.fixture
def client(monkeypatch):
    db, bucket = FakeDB(), FakeBucket()
    monkeypatch.setattr(web, "TOKEN", TOKEN)
    monkeypatch.setattr(web, "connect", db.connect)
    monkeypatch.setattr(web, "bucket", lambda: bucket)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "video-agent-staging.up.railway.app")
    with TestClient(web.app) as c:
        c.db, c.bucket = db, bucket
        yield c


def new_job(client, clips=({"name": "A.MOV", "bytes": 100}, {"name": "B.MOV", "bytes": 200}), brief="two cold ads"):
    res = client.post(f"/{TOKEN}/api/jobs", json={"brief": brief, "clips": list(clips)})
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------- the link

def test_only_the_right_link_works(client):
    assert client.get("/wrong-token/").status_code == 404
    assert client.get("/wrong-token/api/jobs").status_code == 404
    assert client.post("/wrong-token/api/jobs", json={"clips": [{"name": "a", "bytes": 1}]}).status_code == 404
    assert client.get("/").status_code == 404
    for url in (f"/{TOKEN}/", f"/{TOKEN}"):
        res = client.get(url)
        assert res.status_code == 200 and "Video Agent" in res.text and "<video" in res.text


def test_healthz_reports_upload_setup(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "link": "set", "uploads": "allowed from https://video-agent-staging.up.railway.app"}
    assert client.bucket.origins == ["https://video-agent-staging.up.railway.app"]


@pytest.mark.parametrize("bad", ["", "short", "has spaces in it, oh no", "x" * 15])
def test_without_a_proper_link_token_nothing_is_reachable_but_health_says_why(client, monkeypatch, bad):
    monkeypatch.setattr(web, "TOKEN", bad)
    assert client.get(f"/{bad}/").status_code == 404 and client.get(f"/{bad}/api/jobs").status_code == 404
    assert client.get(f"/{TOKEN}/").status_code == 404
    assert client.get("/healthz").json()["link"].startswith("APP_LINK_TOKEN is not set")


def test_start_up_without_a_domain_or_with_a_broken_bucket_still_serves(monkeypatch):
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    assert "no public domain" in web.allow_uploads_from_this_page()

    class Broken:
        def allow_browser_uploads(self, origins):
            raise RuntimeError("AccessDenied")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "x.up.railway.app")
    monkeypatch.setattr(web, "bucket", lambda: Broken())
    assert web.allow_uploads_from_this_page().startswith("FAILED: RuntimeError")


# ---------------------------------------------------------------- creating a job

def test_create_job_hands_out_one_upload_link_per_clip_in_order(client):
    job = new_job(client)
    assert UUID.match(job["job_id"])
    jid = job["job_id"]
    assert [c["n"] for c in job["clips"]] == [0, 1] and [c["name"] for c in job["clips"]] == ["A.MOV", "B.MOV"]
    assert job["clips"][0]["put_url"] == f"https://bucket.test/uploads/{jid}/clip-0?put&expires=3600"
    row = client.db.jobs[jid]
    assert row["status"] == "uploading" and row["source_name"] == "A.MOV + 1 more"
    assert row["sources"] == [{"key": f"uploads/{jid}/clip-0", "name": "A.MOV", "bytes": 100},
                              {"key": f"uploads/{jid}/clip-1", "name": "B.MOV", "bytes": 200}]
    assert row["options"] == {"brief": "two cold ads"} and row["source_key"] == f"uploads/{jid}/clip-0"
    assert client.db.events[-1] == (jid, "created from the web page with 2 clip(s)")


def test_create_job_cleans_names_and_refuses_bad_input(client):
    job = new_job(client, clips=[{"name": "C:\\Users\\rob\\  IMG  1.MOV", "bytes": 5}], brief="")
    assert client.db.jobs[job["job_id"]]["sources"][0]["name"] == "IMG 1.MOV"
    assert client.db.jobs[job["job_id"]]["source_name"] == "IMG 1.MOV"
    bad = [
        ({"brief": "", "clips": []}, "1 to 10 clips"),
        ({"brief": "", "clips": [{"name": f"{i}.mov", "bytes": 1} for i in range(11)]}, "1 to 10 clips"),
        ({"brief": "", "clips": [{"name": "big.mov", "bytes": 5 * 1024 ** 3}]}, "under 4 GB"),
        ({"brief": "", "clips": [{"name": "empty.mov", "bytes": 0}]}, "under 4 GB"),
        ({"brief": "x" * 2001, "clips": [{"name": "a.mov", "bytes": 1}]}, "under 2,000 characters"),
    ]
    for body, message in bad:
        res = client.post(f"/{TOKEN}/api/jobs", json=body)
        assert res.status_code == 400 and message in res.json()["detail"], (body, res.text)
    assert client.post(f"/{TOKEN}/api/jobs", json={"clips": "nope"}).status_code == 422


# ---------------------------------------------------------------- starting it

def test_start_checks_every_clip_arrived_in_full_then_queues(client):
    job = new_job(client)
    jid = job["job_id"]
    res = client.post(f"/{TOKEN}/api/jobs/{jid}/start")
    assert res.status_code == 409 and "Clip 1 (A.MOV) did not arrive" in res.json()["detail"]
    client.bucket.sizes[f"uploads/{jid}/clip-0"] = 100
    client.bucket.sizes[f"uploads/{jid}/clip-1"] = 150
    res = client.post(f"/{TOKEN}/api/jobs/{jid}/start")
    assert res.status_code == 409 and "Clip 2 (B.MOV) arrived incomplete (150 of 200 bytes)" in res.json()["detail"]
    assert client.db.jobs[jid]["status"] == "uploading"
    client.bucket.sizes[f"uploads/{jid}/clip-1"] = 200
    res = client.post(f"/{TOKEN}/api/jobs/{jid}/start")
    assert res.status_code == 200 and res.json()["status"] == "queued" and res.json()["ahead"] == 2
    assert client.db.jobs[jid]["status"] == "queued" and client.db.events[-1][1] == "all clips arrived; queued"
    # starting again is harmless
    assert client.post(f"/{TOKEN}/api/jobs/{jid}/start").json()["status"] == "queued"
    assert client.post(f"/{TOKEN}/api/jobs/00000000-0000-0000-0000-000000000000/start").status_code == 404
    assert client.post(f"/{TOKEN}/api/jobs/not-a-uuid/start").status_code == 422


# ---------------------------------------------------------------- watching it

def test_status_shows_progress_then_the_ads_with_signed_links(client):
    jid = new_job(client)["job_id"]
    row = client.db.jobs[jid]
    row.update(status="working", stage="rendering", stage_detail="ad 2 of 3")
    j = client.get(f"/{TOKEN}/api/jobs/{jid}").json()
    assert (j["status"], j["stage"], j["stage_detail"], j["brief"], j["clips"]) == \
           ("working", "rendering", "ad 2 of 3", "two cold ads", ["A.MOV", "B.MOV"])
    assert "result" not in j and j["created_at"].startswith("2026-10-01T12:00")
    row.update(status="ready", stage=None, cost_usd="0.1025", result={
        "summary": "A whiteboard talk.", "response_to_request": "Two cold ads, as asked.",
        "claims_to_review": [{"ad": "Lag", "claim": "30% more", "reason": "a number"}], "pipeline_notes": ["n1"],
        "notes_key": f"results/{jid}/Review Notes.md", "planning_cost": 0.1025, "source_seconds": 149.0,
        "ads": [{"k": 1, "name": "Lag", "funnel_stage": "cold", "angle": "why", "headline": "H", "callouts": ["c1"],
                 "primary_text": "p", "seconds": 33.0, "layout_check": "passed", "verify": {"match": 0.93},
                 "file_key": f"results/{jid}/Ad - Lag (2026-10-01).mp4", "error": None},
                {"k": 2, "name": "Broken", "headline": "H2", "seconds": 0, "layout_check": "FAILED", "file_key": None,
                 "error": "render failed"}],
        "raw_plan": {"secret": "never shown"}})
    j = client.get(f"/{TOKEN}/api/jobs/{jid}").json()
    assert j["status"] == "ready" and j["cost_usd"] == 0.1025
    r = j["result"]
    assert "raw_plan" not in r and r["response_to_request"] == "Two cold ads, as asked."
    assert r["notes_url"] == f"https://bucket.test/results/{jid}/Review Notes.md?download&name=Review Notes.md"
    ad = r["ads"][0]
    assert ad["file"] == "Ad - Lag (2026-10-01).mp4" and ad["speech_match"] == 0.93 and ad["callouts"] == ["c1"]
    assert ad["preview_url"] == f"https://bucket.test/results/{jid}/Ad - Lag (2026-10-01).mp4?inline&name=Ad - Lag (2026-10-01).mp4"
    assert ad["download_url"].endswith("?download&name=Ad - Lag (2026-10-01).mp4")
    assert r["ads"][1]["preview_url"] is None and r["ads"][1]["error"] == "render failed"
    assert r["claims_to_review"][0]["claim"] == "30% more"


def test_recent_list_skips_jobs_still_uploading(client):
    a = new_job(client, brief="first")["job_id"]
    b = new_job(client, clips=[{"name": "C.MOV", "bytes": 1}], brief="second")["job_id"]
    client.db.jobs[a]["status"] = "ready"
    jobs = client.get(f"/{TOKEN}/api/jobs").json()["jobs"]
    assert [j["job_id"] for j in jobs] == [a]
    assert jobs[0]["label"] == "A.MOV + 1 more" and jobs[0]["brief"] == "first" and jobs[0]["clips"] == 2
    assert b not in [j["job_id"] for j in jobs]

