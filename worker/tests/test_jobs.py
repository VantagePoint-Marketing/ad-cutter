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
        self.uploads = []

    def download(self, key, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00\x00\x00\x18ftypisom")
        return dest

    def upload(self, path, key, content_type):
        assert Path(path).exists()
        self.uploads.append((key, content_type))
        return key


def job(**kw):
    return {"id": "11111111-2222-3333-4444-555555555555", "kind": "edit", "parent_job": None,
            "source_key": "uploads/11111111-2222-3333-4444-555555555555/source", "source_name": "IMG.MOV",
            "options": {"note": "push the live session"}, "attempts": 1, "max_attempts": 3, **kw}


def fake_run(files=True, cost=0.12):
    def run_pipeline(cfg, src, work, out_dir, client, **kw):
        work.mkdir(parents=True, exist_ok=True)
        (work / "plan.json").write_text(json.dumps({"ads": [{"name": "A"}]}), encoding="utf-8")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "Review Notes.md").write_text("# notes", encoding="utf-8")
        kw["progress"]("planning", "Gemini is watching the video")
        report = []
        for k in (1, 2):
            f = f"Ad - A{k} (2026-09-30).mp4"
            if files:
                (out_dir / f).write_bytes(b"mp4")
            report.append({"k": k, "ad": {"name": f"A{k}", "headline": "h", "callouts": [{"text": "c"}]},
                           "len": 30.0, "check": "passed", "file": f if files else None,
                           **({} if files else {"error": "render failed"})})
        run_pipeline.kwargs = kw
        return {"plan": {"summary": "s", "claims_to_review": [{"claim": "x"}]}, "report": report, "notes": [],
                "cost": cost, "duration": 42.0}
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
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    assert jobs.run_job(conn, bucket, job(), cfg) == "ready"
    assert statuses(log) == ["ready"]
    keys = [k for k, _ in bucket.uploads]
    assert "results/11111111-2222-3333-4444-555555555555/Ad - A1 (2026-09-30).mp4" in keys
    assert keys[-1].endswith("/Review Notes.md")
    result = json.loads([p[1] for s, p in log if s.startswith("update jobs set status")][0])
    assert result["ads"][0]["file_key"].startswith("results/") and result["raw_plan"] == {"ads": [{"name": "A"}]}
    assert ac.run_pipeline.kwargs["note"] == "push the live session" and ac.run_pipeline.kwargs["replan"] is False
    assert not (Path(cfg["work_dir"]) / job()["id"]).exists()        # work dir always cleaned up


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

    def run_pipeline(cfg, src, work, out_dir, client, **kw):
        seen["plan"] = json.loads((work / "plan.json").read_text())
        seen["only"] = kw["only"]
        raise ac.AdCutterError("stop here")
    monkeypatch.setattr(ac, "run_pipeline", run_pipeline)
    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 3600)
    jobs.run_job(lambda: FakeConn(log, parent), FakeBucket(),
                 job(kind="rebuild", parent_job="p", options={"only": [2]}), {"work_dir": str(tmp_path), "monthly_budget_usd": 1})
    assert seen == {"plan": {"ads": [{"name": "from parent"}]}, "only": [2]}


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


def test_migrations_are_found_in_order():
    names = [p.name for p in migrate.pending(set())]
    assert names and names == sorted(names) and names[0] == "001_worker.sql"
    assert migrate.pending(set(names)) == []
