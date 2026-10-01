"""Worker job handling with a fake database and bucket (real-Postgres checks live in test_pg_integration.py)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ad_cutter as ac  # noqa: E402
import jobs  # noqa: E402
import migrate  # noqa: E402
from budget import BudgetExceeded  # noqa: E402
from storage import Bucket  # noqa: E402

JOB = "11111111-2222-3333-4444-555555555555"


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, log, parent_result=None):
        self.log, self.parent_result = log, parent_result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))
        if sql.strip().startswith("select result from jobs"):
            return FakeResult([(self.parent_result,)])
        return FakeResult([])


class FakeBucket:
    def __init__(self):
        self.uploads, self.downloads = [], []

    def download(self, key, dest):
        self.downloads.append(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00\x00\x00\x18ftypisom")
        return dest

    def upload(self, path, key, content_type):
        assert Path(path).exists()
        self.uploads.append((key, content_type))
        return key


def job(**kw):
    return {"id": JOB, "kind": "edit", "parent_job": None, "source_key": f"uploads/{JOB}/source",
            "source_name": "IMG.MOV", "options": {"note": "push the live session"}, "attempts": 1,
            "max_attempts": 3, "sources": [], **kw}


def fake_run(files=True, cost=0.12, response=""):
    def run_pipeline(cfg, srcs, work, out_dir, client, **kw):
        work.mkdir(parents=True, exist_ok=True)
        (work / "plan.json").write_text(json.dumps({"ads": [{"name": "A"}]}), encoding="utf-8")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "Review Notes.md").write_text("# notes", encoding="utf-8")
        kw["progress"]("planning", "Gemini is watching the footage")
        report = []
        for k in (1, 2):
            f = f"Ad - A{k} (2026-09-30).mp4"
            if files:
                (out_dir / f).write_bytes(b"mp4")
            report.append({"k": k, "ad": {"name": f"A{k}", "headline": "h", "callouts": [{"text": "c"}]},
                           "len": 30.0, "check": "passed", "file": f if files else None,
                           **({} if files else {"error": "render failed"})})
        run_pipeline.kwargs, run_pipeline.srcs = kw, srcs
        return {"plan": {"summary": "s", "response_to_request": response, "claims_to_review": [{"claim": "x"}]},
                "report": report, "notes": [], "cost": cost, "duration": 42.0,
                "clips": [{"name": "IMG.MOV", "start": 0.0, "seconds": 42.0}]}
    return run_pipeline


@pytest.fixture
def env(monkeypatch, tmp_path):
    log = []
    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 3600)
    cfg = {"work_dir": str(tmp_path / "jobs"), "monthly_budget_usd": 100}
    return log, cfg, FakeBucket(), (lambda: FakeConn(log))


def statuses(log):
    return [p[0] for s, p in log if s.startswith("update jobs set status = %s")]


def test_success_uploads_results_and_marks_ready(monkeypatch, env):
    log, cfg, bucket, conn = env
    monkeypatch.setattr(ac, "run_pipeline", fake_run(response="Three cold ads, as asked."))
    assert jobs.run_job(conn, bucket, job(), cfg) == "ready"
    assert statuses(log) == ["ready"]
    keys = [k for k, _ in bucket.uploads]
    assert f"results/{JOB}/Ad - A1 (2026-09-30).mp4" in keys
    assert keys[-1].endswith("/Review Notes.md")
    result = json.loads([p[1] for s, p in log if s.startswith("update jobs set status")][0])
    assert result["ads"][0]["file_key"].startswith("results/") and result["raw_plan"] == {"ads": [{"name": "A"}]}
    assert result["response_to_request"] == "Three cold ads, as asked." and result["clips"][0]["name"] == "IMG.MOV"
    # an older row with only source_key: the single upload, and its note becomes the request
    assert bucket.downloads == [f"uploads/{JOB}/source"]
    assert ac.run_pipeline.kwargs["brief"] == "push the live session" and ac.run_pipeline.kwargs["replan"] is False
    assert ac.run_pipeline.kwargs["names"] == ["IMG.MOV"] and [p.name for p in ac.run_pipeline.srcs] == ["clip-0.bin"]
    assert not (Path(cfg["work_dir"]) / JOB).exists()        # work dir always cleaned up


def test_several_clips_are_downloaded_in_order_with_their_names(monkeypatch, env):
    _, cfg, bucket, conn = env
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    sources = [{"key": f"uploads/{JOB}/clip-0", "name": "A.MOV", "bytes": 10},
               {"key": f"uploads/{JOB}/clip-1", "name": "B.MOV", "bytes": 20}]
    assert jobs.run_job(conn, bucket, job(sources=sources, options={"brief": "two cold ads"}), cfg) == "ready"
    assert bucket.downloads == [f"uploads/{JOB}/clip-0", f"uploads/{JOB}/clip-1"]
    assert [p.name for p in ac.run_pipeline.srcs] == ["clip-0.bin", "clip-1.bin"]
    assert ac.run_pipeline.kwargs["names"] == ["A.MOV", "B.MOV"] and ac.run_pipeline.kwargs["brief"] == "two cold ads"


def test_a_job_may_only_read_its_own_uploads(monkeypatch, env):
    log, cfg, bucket, conn = env
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    other = [{"key": "uploads/99999999-2222-3333-4444-555555555555/clip-0", "name": "X.MOV"}]
    assert jobs.run_job(conn, bucket, job(sources=other), cfg) == "failed"
    upd = next(p for s, p in log if s.startswith("update jobs set status"))
    assert upd[0] == "failed" and "not one of this job's uploads" in upd[2] and upd[4] is True
    assert bucket.downloads == []


def test_clips_that_add_up_to_too_much_fail_before_any_download(monkeypatch, env):
    log, cfg, bucket, conn = env
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    big = [{"key": f"uploads/{JOB}/clip-{n}", "name": f"{n}.MOV", "bytes": 3 * 1024 ** 3} for n in range(4)]
    assert jobs.run_job(conn, bucket, job(sources=big), cfg) == "failed"
    upd = next(p for s, p in log if s.startswith("update jobs set status"))
    assert "10 GB per job" in upd[2] and upd[4] is True and bucket.downloads == []


def test_a_rebuild_may_read_its_parents_clips(monkeypatch, env):
    _, cfg, bucket, conn = env
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    parent = "99999999-2222-3333-4444-555555555555"
    theirs = [{"key": f"uploads/{parent}/clip-0", "name": "P.MOV"}]
    assert jobs.run_job(conn, bucket, job(kind="rebuild", parent_job=parent, sources=theirs), cfg) == "ready"
    assert bucket.downloads == [f"uploads/{parent}/clip-0"]
    assert jobs.run_job(conn, bucket, job(kind="edit", parent_job=parent, sources=theirs), cfg) == "failed"


def test_an_oversized_clip_fails_without_retry(monkeypatch, env):
    log, cfg, bucket, conn = env

    def too_big(key, dest):
        raise ValueError("upload is 5.0 GB; the limit is 4 GB")
    bucket.download = too_big
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    assert jobs.run_job(conn, bucket, job(), cfg) == "failed"
    upd = next(p for s, p in log if s.startswith("update jobs set status"))
    assert upd[0] == "failed" and "4 GB" in upd[2] and upd[4] is True


def test_progress_is_written_to_the_job(monkeypatch, env):
    log, cfg, bucket, conn = env
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    jobs.run_job(conn, bucket, job(), cfg)
    stages = [p[0] for s, p in log if s.startswith("update jobs set stage = %s")]
    assert stages == ["downloading", "planning", "uploading"]


def test_no_rendered_ads_is_a_failure_without_retry(monkeypatch, env):
    log, cfg, bucket, conn = env
    monkeypatch.setattr(ac, "run_pipeline", fake_run(files=False))
    assert jobs.run_job(conn, bucket, job(), cfg) == "failed"
    upd = [p for s, p in log if s.startswith("update jobs set status")][0]
    assert upd[0] == "failed" and upd[4] is True           # no_retry


@pytest.mark.parametrize("err", [ac.AdCutterError("can't use this video: it has no sound track"),
                                 BudgetExceeded("only $0.10 of this month's $100 Gemini budget is left")])
def test_video_or_budget_problems_fail_cleanly_without_retry(monkeypatch, env, err):
    log, cfg, bucket, conn = env

    def boom(*a, **k):
        raise err
    monkeypatch.setattr(ac, "run_pipeline", boom)
    assert jobs.run_job(conn, bucket, job(), cfg) == "failed"
    upd = [p for s, p in log if s.startswith("update jobs set status")][0]
    assert upd[0] == "failed" and upd[2][0].isupper() and upd[4] is True


def test_unexpected_crash_is_retried_while_attempts_remain(monkeypatch, env):
    log, cfg, bucket, conn = env

    def boom(*a, **k):
        raise RuntimeError("out of memory")
    monkeypatch.setattr(ac, "run_pipeline", boom)
    assert jobs.run_job(conn, bucket, job(attempts=1), cfg) == "retry"
    assert statuses(log) == ["queued"]
    log.clear()
    assert jobs.run_job(conn, bucket, job(attempts=3), cfg) == "failed"
    assert statuses(log) == ["failed"]


def test_shutdown_hands_the_job_back_without_using_an_attempt(monkeypatch, env):
    log, cfg, bucket, conn = env

    def stop(*a, **k):
        raise jobs.Stop()
    monkeypatch.setattr(ac, "run_pipeline", stop)
    with pytest.raises(jobs.Stop):
        jobs.run_job(conn, bucket, job(), cfg)
    assert any("status = 'queued', attempts = greatest(attempts - 1, 0)" in s for s, _ in log)


def test_stop_is_not_swallowed_by_per_ad_error_handling():
    assert not issubclass(jobs.Stop, Exception)            # `except Exception` in the pipeline can't catch it


def test_finish_only_applies_to_the_owning_worker(monkeypatch, env):
    log, cfg, bucket, conn = env
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    jobs.run_job(conn, bucket, job(), cfg)
    sql, params = [(s, p) for s, p in log if s.startswith("update jobs set status = %s")][0]
    assert "locked_by = %s and status = 'working'" in sql and params[-1] == jobs.WORKER_ID


def test_rebuild_reuses_the_parent_plan(monkeypatch, tmp_path):
    log = []
    parent = {"raw_plan": {"ads": [{"name": "from parent"}]}}
    seen = {}

    def run_pipeline(cfg, srcs, work, out_dir, client, **kw):
        seen["plan"] = json.loads((work / "plan.json").read_text())
        seen["only"] = kw["only"]
        raise ac.AdCutterError("stop here")
    monkeypatch.setattr(ac, "run_pipeline", run_pipeline)
    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 3600)
    jobs.run_job(lambda: FakeConn(log, parent), FakeBucket(),
                 job(kind="rebuild", parent_job="p", options={"only": [2]}), {"work_dir": str(tmp_path), "monthly_budget_usd": 1})
    assert seen == {"plan": {"ads": [{"name": "from parent"}]}, "only": [2]}


def test_claim_rows_carry_the_clip_list():
    assert jobs.CLAIM_SQL.strip().endswith("sources")
    row = (JOB, "edit", None, "k", "n", {}, 1, 3, [{"key": "k"}])

    class Conn:
        def execute(self, sql, params):
            return FakeResult([row])
    assert jobs.claim(Conn())["sources"] == [{"key": "k"}]


# ---------------------------------------------------------------- storage and migrations

@pytest.mark.parametrize("name", ["../x.mp4", ".hidden", "a/b.mp4", "", "x" * 200])
def test_result_key_rejects_unsafe_names(name):
    with pytest.raises(ValueError):
        Bucket.result_key("j", name)


def test_download_refuses_files_over_4gb(tmp_path):
    class S3:
        def head_object(self, **k):
            return {"ContentLength": 5 * 1024 ** 3}
    with pytest.raises(ValueError, match="4 GB"):
        Bucket(client=S3(), bucket="b").download("uploads/j/source", tmp_path / "x")


def test_a_job_owns_only_its_own_upload_keys():
    assert Bucket.owns(JOB, f"uploads/{JOB}/source") and Bucket.owns(JOB, f"uploads/{JOB}/clip-0")
    assert Bucket.owns(JOB, Bucket.clip_key(JOB, 9))
    for key in (f"uploads/{JOB}/clip-10", f"uploads/{JOB}/other", "uploads/x/source",
                f"results/{JOB}/Ad.mp4", f"uploads/{JOB}/clip-0/../../x", ""):
        assert not Bucket.owns(JOB, key)
    assert not Bucket.owns("not-a-job-id", "uploads/not-a-job-id/source")
    with pytest.raises(ValueError):
        Bucket.clip_key(JOB, 10)


def test_signed_links_grant_one_key_and_one_method():
    calls = []

    class S3:
        def generate_presigned_url(self, op, Params, ExpiresIn, HttpMethod=None):
            calls.append((op, Params, ExpiresIn, HttpMethod))
            return f"https://bucket/{Params['Key']}?signed"
    b = Bucket(client=S3(), bucket="media")
    assert b.put_url(f"uploads/{JOB}/clip-0").endswith("clip-0?signed")
    assert calls[-1] == ("put_object", {"Bucket": "media", "Key": f"uploads/{JOB}/clip-0"}, 3600, "PUT")
    b.get_url(f"results/{JOB}/Ad - A (2026-10-01).mp4", "Ad - A (2026-10-01).mp4", attachment=True)
    assert calls[-1][1]["ResponseContentDisposition"] == 'attachment; filename="Ad - A (2026-10-01).mp4"'
    with pytest.raises(ValueError):
        b.get_url("results/x", 'bad"name.mp4')


def test_size_is_none_for_a_missing_object_and_raises_otherwise():
    from botocore.exceptions import ClientError

    class S3:
        def head_object(self, Bucket, Key):
            if Key == "missing":
                raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
            if Key == "forbidden":
                raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
            return {"ContentLength": 42}
    b = Bucket(client=S3(), bucket="media")
    assert b.size("there") == 42 and b.size("missing") is None
    with pytest.raises(ClientError):
        b.size("forbidden")


def test_migrations_are_found_in_order():
    names = [p.name for p in migrate.pending(set())]
    assert names and names == sorted(names) and names[:2] == ["001_worker.sql", "002_web.sql"]
    assert migrate.pending(set(names)) == []
