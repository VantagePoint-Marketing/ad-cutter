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
        self.library_sources, self.library_notes, self.library_missing = [], [], False
        self.feedback, self.feedback_missing = [], False        # rows of (job_id, ad_k, verdict, note), oldest first

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
                return Cursor(["id"], [(job["id"],)])      # "returning id": one row when it flipped
            return Cursor(["id"], [])
        if "ad_feedback" in sql:
            if self.feedback_missing:
                import psycopg
                raise psycopg.errors.UndefinedTable('relation "ad_feedback" does not exist')
            if sql.startswith("insert into ad_feedback"):
                self.feedback.append(tuple(params))
                return Cursor([], [])
            if sql.startswith("select count(*) from ad_feedback"):
                return Cursor(["count"], [(sum(1 for f in self.feedback if f[0] == params[0]),)])
            if sql.startswith("select distinct on (ad_k)"):          # the newest click for each ad of one job
                newest = {f[1]: f for f in self.feedback if f[0] == params[0]}
                return Cursor(["ad_k", "verdict", "note"], [(k, f[2], f[3]) for k, f in sorted(newest.items())])
        if self.library_missing and ("ref_sources" in sql or "ref_notes" in sql):
            import psycopg
            raise psycopg.errors.UndefinedTable('relation "ref_sources" does not exist')
        if "from ref_sources" in sql:
            cols = self._cols(sql)
            return Cursor(cols, [tuple(s[c] for c in cols) for s in self.library_sources])
        if "from ref_notes" in sql:
            with_lessons = params[0]
            return Cursor(["source_id", "window_start", "summary", "lesson_count", "lessons"],
                          [(n["source_id"], n["window_start"], n["note"].get("summary"),
                            len(n["note"].get("lessons", [])), n["note"].get("lessons") if with_lessons else None)
                           for n in self.library_notes])
        if "from api_quota" in sql:
            return Cursor(["used"], [(1800,)])
        if sql.startswith("select count(*)"):
            if "interval '1 day'" in sql:          # the daily cap: every job the page made counts
                return Cursor(["count"], [(len(self.jobs),)])
            return Cursor(["count"], [(2,)])       # queue position
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
    assert job["clips"][0]["put_url"] == f"https://bucket.test/uploads/{jid}/clip-0?put&expires=21600"
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
        ({"brief": "", "clips": [{"name": f"{i}.mov", "bytes": 3 * 1024 ** 3} for i in range(4)]}, "10 GB per job"),
    ]
    for body, message in bad:
        res = client.post(f"/{TOKEN}/api/jobs", json=body)
        assert res.status_code == 400 and message in res.json()["detail"], (body, res.text)
    assert client.post(f"/{TOKEN}/api/jobs", json={"clips": "nope"}).status_code == 422


def test_the_page_stops_taking_jobs_after_the_daily_limit(client, monkeypatch):
    monkeypatch.setattr(web, "DAILY_JOBS", 2)
    new_job(client)
    new_job(client)
    res = client.post(f"/{TOKEN}/api/jobs", json={"brief": "", "clips": [{"name": "c.mov", "bytes": 1}]})
    assert res.status_code == 429 and "daily limit" in res.json()["detail"] and len(client.db.jobs) == 2


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
    # starting again is harmless and records nothing new
    events_before = len(client.db.events)
    assert client.post(f"/{TOKEN}/api/jobs/{jid}/start").json()["status"] == "queued"
    assert len(client.db.events) == events_before
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


def test_library_lists_videos_with_their_lessons_in_order(client):
    client.db.library_sources = [
        {"id": 1, "tier": 1, "external_id": "IROKEjmIIlM", "title": "When Editing Ruins Your Video",
         "channel": "HillierSmith", "seconds": 2184, "status": "working", "reason": None},
        {"id": 2, "tier": 1, "external_id": "QR8LxximqWI", "title": "5 MORE Editing Mistakes", "channel": "HillierSmith",
         "seconds": 348, "status": "done", "reason": None},
        {"id": 3, "tier": 1, "external_id": "zzzzzzzzzzz", "title": "", "channel": "", "seconds": None,
         "status": "failed", "reason": "not on YouTube"}]
    client.db.library_notes = [
        {"source_id": 1, "window_start": 600, "note": {"summary": "part two", "lessons": [
            {"at_s": 700, "topic": "Story", "principle": "Later lesson.", "lever": "hook", "how_we_apply": "x"}]}},
        {"source_id": 1, "window_start": 0, "note": {"summary": "part one", "lessons": [
            {"at_s": 41, "topic": "Pacing", "principle": "Earlier <b>lesson</b>.", "lever": "pause_trim"}]}}]
    short = client.get(f"/{TOKEN}/api/library").json()
    assert (short["studied"], short["total"], short["lessons"], short["hours_today"]) == (1, 3, 2, 0.5)
    assert "lessons" not in short["videos"][0] and short["videos"][0]["lesson_count"] == 2
    full = client.get(f"/{TOKEN}/api/library?lessons=true").json()
    first = full["videos"][0]
    assert [x["at_s"] for x in first["lessons"]] == [41, 700] and first["minutes"] == 36.4
    assert first["url"] == "https://www.youtube.com/watch?v=IROKEjmIIlM"
    assert full["videos"][2]["reason"] == "not on YouTube" and full["videos"][2]["lessons"] == []
    assert client.get("/wrong-token-1234567/api/library").status_code == 404


def test_library_before_its_tables_exist_is_empty_not_an_error(client):
    client.db.library_missing = True
    res = client.get(f"/{TOKEN}/api/library")
    assert res.status_code == 200 and res.json() == {"videos": [], "studied": 0, "total": 0, "lessons": 0,
                                                     "hours_today": 0}


def test_recent_list_skips_jobs_still_uploading(client):
    a = new_job(client, brief="first")["job_id"]
    b = new_job(client, clips=[{"name": "C.MOV", "bytes": 1}], brief="second")["job_id"]
    client.db.jobs[a]["status"] = "ready"
    jobs = client.get(f"/{TOKEN}/api/jobs").json()["jobs"]
    assert [j["job_id"] for j in jobs] == [a]
    assert jobs[0]["label"] == "A.MOV + 1 more" and jobs[0]["brief"] == "first" and jobs[0]["clips"] == 2
    assert b not in [j["job_id"] for j in jobs]



# ---------------------------------------------------------------- the self-check and the feedback buttons

REVIEW = {"scores": {a: 4 for a in ("hook", "cuts", "story", "captions", "overlays", "request_fit", "compliance")},
          "problems": [{"at_s": 3.2, "area": "captions", "what": "Words run together.", "fix": "Split the group."}],
          "verdict": "Solid.", "look": False}


def ready_job(client):
    jid = new_job(client)["job_id"]
    client.db.jobs[jid].update(status="ready", result={
        "summary": "s", "ads": [
            {"k": 1, "name": "Lag", "headline": "H", "seconds": 30.0, "review": REVIEW, "spoken": "never shown",
             "file_key": f"results/{jid}/Ad - Lag.mp4"},
            {"k": 2, "name": "Broken", "headline": "H2", "seconds": 0, "file_key": None, "error": "render failed"}]})
    return jid


def test_the_scorecard_and_the_saved_verdict_come_with_each_ad(client):
    jid = ready_job(client)
    ads = client.get(f"/{TOKEN}/api/jobs/{jid}").json()["result"]["ads"]
    assert ads[0]["review"]["scores"]["hook"] == 4 and ads[0]["review"]["problems"][0]["area"] == "captions"
    assert ads[0]["review"]["look"] is False and ads[0]["feedback"] is None and "spoken" not in ads[0]
    assert ads[1]["review"] is None
    client.db.feedback += [(jid, 1, "bad", "first thought"), (jid, 1, "good", "changed my mind")]
    assert client.get(f"/{TOKEN}/api/jobs/{jid}").json()["result"]["ads"][0]["feedback"] == \
           {"verdict": "good", "note": "changed my mind"}


def test_feedback_is_saved_with_a_tidy_note(client):
    jid = ready_job(client)
    res = client.post(f"/{TOKEN}/api/jobs/{jid}/ads/1/feedback", json={"verdict": "bad", "note": "  Captions \n\n too small  "})
    assert res.status_code == 200 and res.json() == {"k": 1, "verdict": "bad", "note": "Captions too small"}
    assert client.db.feedback == [(jid, 1, "bad", "Captions too small")]
    assert client.post(f"/{TOKEN}/api/jobs/{jid}/ads/1/feedback", json={"verdict": "good"}).json()["note"] == ""


@pytest.mark.parametrize("body", [{"verdict": "meh"}, {"note": "no verdict"}, {"verdict": "bad", "note": "x" * 1001}])
def test_bad_feedback_is_refused(client, body):
    jid = ready_job(client)
    assert client.post(f"/{TOKEN}/api/jobs/{jid}/ads/1/feedback", json=body).status_code in (400, 422)
    assert client.db.feedback == []


def test_feedback_only_for_a_real_finished_ad_of_a_ready_job(client):
    jid = ready_job(client)
    url = lambda job, k: f"/{TOKEN}/api/jobs/{job}/ads/{k}/feedback"
    ok = {"verdict": "good"}
    assert client.post(url(jid, 2), json=ok).status_code == 404            # that ad never rendered
    assert client.post(url(jid, 3), json=ok).status_code == 404            # no such ad
    assert client.post(url(jid, 0), json=ok).status_code == 422 and client.post(url(jid, 21), json=ok).status_code == 422
    assert client.post(url("00000000-0000-0000-0000-000000000000", 1), json=ok).status_code == 404
    other = new_job(client)["job_id"]                                      # still uploading
    assert client.post(url(other, 1), json=ok).status_code == 404
    assert client.post(f"/wrong-token-1234567/api/jobs/{jid}/ads/1/feedback", json=ok).status_code == 404
    assert client.db.feedback == []


def test_one_job_takes_a_limited_amount_of_feedback(client):
    jid = ready_job(client)
    client.db.feedback = [(jid, 1, "good", "")] * web.FEEDBACK_PER_JOB
    assert client.post(f"/{TOKEN}/api/jobs/{jid}/ads/1/feedback", json={"verdict": "bad"}).status_code == 429


def test_the_page_still_works_before_the_feedback_table_exists(client):
    jid = ready_job(client)
    client.db.feedback_missing = True
    ads = client.get(f"/{TOKEN}/api/jobs/{jid}").json()["result"]["ads"]
    assert ads[0]["feedback"] is None and ads[0]["review"]["scores"]["cuts"] == 4
