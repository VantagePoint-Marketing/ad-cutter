"""The AI models and effort levels a person may pick (models.py) and how a job's choice reaches the planner."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ad_cutter as ac  # noqa: E402
import jobs  # noqa: E402
import llm  # noqa: E402
import models  # noqa: E402
from test_jobs import FakeBucket, FakeConn, JOB, cancellable, env, fake_run, job  # noqa: E402,F401


def test_every_offered_model_has_a_price_and_the_default_is_the_planner_the_config_names():
    for key, m in models.MODELS.items():
        assert m["openrouter"] in llm.PRICES and llm.PRICES[m["openrouter"]] == m["price"]
        assert llm.estimate_cost(m["openrouter"], 10_000, 1_000) > 0
    cfg = ac.load_config(Path(__file__).resolve().parents[1] / "config.json")
    assert cfg["plan_model"] == models.MODELS[models.DEFAULT_MODEL]["openrouter"]


def test_a_model_that_is_not_in_the_list_has_no_price_and_is_refused():
    with pytest.raises(llm.LLMError, match="no price on file"):
        llm.estimate_cost("some/other-model", 1000, 1000)


@pytest.mark.parametrize("model, effort, expect", [
    ("efficient", "high", ("efficient", "google/gemini-3.8-flash", "high")),
    ("cheapest", None, ("cheapest", "google/gemini-3.1-flash-lite", "medium")),
    (None, "low", ("best", "google/gemini-3.1-pro-preview", "low")),
    ("google/gemini-3.1-pro-preview", "MEDIUM", ("best", "google/gemini-3.1-pro-preview", "medium")),    # not a key: default
    ("gpt-5", "extreme", ("best", "google/gemini-3.1-pro-preview", "medium")),
    (7, ["x"], ("best", "google/gemini-3.1-pro-preview", "medium")),
])
def test_resolve_only_ever_returns_a_listed_model_and_effort(model, effort, expect):
    assert models.resolve(model, effort) == expect


def test_costs_rise_with_effort_and_ads_and_fall_with_cheaper_models():
    for key in models.MODELS:
        low, mid, high = (models.estimate_cost(key, e) for e in ("low", "medium", "high"))
        assert 0 < low < mid < high
        assert models.estimate_cost(key, "medium", 1) < models.estimate_cost(key, "medium", 5)
    assert models.estimate_cost("cheapest", "medium") < models.estimate_cost("efficient", "medium") < models.estimate_cost("best", "medium")


def test_the_catalog_for_the_page_never_shows_openrouter_ids():
    text = str(models.catalog())
    assert "openrouter" not in text and "google/" not in text
    assert [m["key"] for m in models.catalog()["models"]] == list(models.MODELS)


def test_every_listed_model_is_zero_retention_ready_route():
    assert llm.ROUTES["zdr"] == {"data_collection": "deny", "zdr": True}      # raw footage only ever goes this way


def test_the_planner_gets_the_effort_as_its_reasoning_level():
    seen = {}

    class Client:
        def chat_json(self, model, content, **kw):
            seen.update(model=model, **kw)
            return {"ads": []}, {"cost": 0}
    proxy = Path(__file__)                                                    # any readable file stands in for the video
    ac.call_gemini({"plan_model": "google/gemini-3.8-flash", "plan_effort": "high"}, Client(), "prompt", proxy, 30.0)
    assert seen["model"] == "google/gemini-3.8-flash" and seen["reasoning"] == "high" and seen["route"] == "zdr"
    ac.call_gemini({"plan_model": "google/gemini-3.1-pro-preview"}, Client(), "prompt", proxy, 30.0)
    assert seen["reasoning"] == "medium"                                      # nothing chosen: the old default


def test_the_job_runs_with_the_ai_and_effort_it_picked(monkeypatch, env):
    log, cfg, bucket, conn = env
    cfg = {**cfg, "plan_model": "google/gemini-3.1-pro-preview"}
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    assert jobs.run_job(conn, bucket, job(options={"brief": "x", "model": "efficient", "effort": "low"}), cfg) == "ready"
    used = ac.run_pipeline.args_cfg
    assert used["plan_model"] == "google/gemini-3.8-flash" and used["plan_effort"] == "low"
    assert cfg["plan_model"] == "google/gemini-3.1-pro-preview"               # the worker's own settings are not changed
    result = [p for s, p in log if s.startswith("update jobs set status")][0][1]
    assert '"model_name": "Gemini 3.8 Flash"' in result and '"effort": "low"' in result


def test_a_job_that_picked_nothing_keeps_the_workers_configured_model(monkeypatch, env):
    log, cfg, bucket, conn = env
    cfg = {**cfg, "plan_model": "google/gemini-3.1-pro-preview"}
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    jobs.run_job(conn, bucket, job(options={"brief": "x"}), cfg)
    used = ac.run_pipeline.args_cfg
    assert used["plan_model"] == "google/gemini-3.1-pro-preview" and "plan_effort" not in used


def test_a_forged_model_in_a_job_never_reaches_the_planner(monkeypatch, env):
    log, cfg, bucket, conn = env
    cfg = {**cfg, "plan_model": "google/gemini-3.1-pro-preview"}
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    jobs.run_job(conn, bucket, job(options={"brief": "x", "model": "anthropic/claude-opus-5", "effort": "max"}), cfg)
    used = ac.run_pipeline.args_cfg
    assert used["plan_model"] == "google/gemini-3.1-pro-preview" and used["plan_effort"] == "medium"
