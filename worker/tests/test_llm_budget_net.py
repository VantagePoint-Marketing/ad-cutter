"""Gemini client, spending guard and outbound allowlist. No real network calls: a fake transport stands in."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import budget  # noqa: E402
import llm  # noqa: E402
import net  # noqa: E402

MODEL = "google/gemini-3.1-pro-preview"


# ---------------------------------------------------------------- allowlist

@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc", "https://youtu.be/abc", "https://rr3---sn-a.googlevideo.com/videoplayback",
    "https://i.ytimg.com/vi/abc/hq.jpg", "https://evil.example.com/x", "http://openrouter.ai/api/v1/key",
    "https://openrouter.ai.evil.com/api", "file:///etc/passwd",
])
def test_blocked_urls(url):
    with pytest.raises(net.BlockedHost):
        net.check_url(url)


@pytest.mark.parametrize("url", ["https://openrouter.ai/api/v1/chat/completions",
                                 "https://www.googleapis.com/youtube/v3/videos?id=x",
                                 "https://public.api.foreplay.co/api/usage"])
def test_allowed_urls(url):
    assert net.check_url(url) == url


def test_youtube_stays_blocked_even_if_allowlisted(monkeypatch):
    monkeypatch.setattr(net, "ALLOWED_HOSTS", net.ALLOWED_HOSTS | {"www.youtube.com"})
    with pytest.raises(net.BlockedHost):
        net.check_url("https://www.youtube.com/watch?v=abc")


# ---------------------------------------------------------------- key handling

def test_read_key_uses_env_only(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_VA_KEY", "sk-or-test")
    assert llm.read_key("TEST_VA_KEY") == "sk-or-test"


def test_read_key_missing_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("TEST_VA_MISSING", raising=False)
    with pytest.raises(llm.LLMError, match="environment variable"):
        llm.read_key("TEST_VA_MISSING")


# ---------------------------------------------------------------- budget

def test_estimate_cost_and_long_prompt_surcharge():
    assert llm.estimate_cost(MODEL, 100_000, 16_000) == pytest.approx(0.2 + 0.192)
    assert llm.estimate_cost(MODEL, 250_000, 0) == pytest.approx(1.0)
    with pytest.raises(llm.LLMError):
        llm.estimate_cost("some/unknown-model", 1, 1)


def test_check_room():
    budget.check_room(90, 5, 5, 100)
    with pytest.raises(budget.BudgetExceeded):
        budget.check_room(90, 5, 5.01, 100)


def test_local_ledger_records_and_refuses_over_cap(tmp_path):
    led = budget.LocalLedger(tmp_path / "l.jsonl", monthly_cap=1.0)
    r = led.reserve(0.4, "a")
    led.settle(r, 0.35, MODEL)
    assert led.spent() == pytest.approx(0.35)
    r2 = led.reserve(0.6, "b")
    led.settle(r2, None, MODEL)            # unknown cost counts as the worst case
    assert led.spent() == pytest.approx(0.95)
    with pytest.raises(budget.BudgetExceeded):
        led.reserve(0.1, "c")
    other = json.loads((tmp_path / "l.jsonl").read_text().splitlines()[0])
    assert other["label"] == "a" and other["estimate"] == 0.4


# ---------------------------------------------------------------- client with a fake transport

class FakeTransport:
    def __init__(self, remaining=50.0, replies=()):
        self.remaining, self.replies, self.posts = remaining, list(replies), []

    def __call__(self, method, url, headers=None, body=None, timeout=60):
        net.check_url(url)
        assert headers["Authorization"] == "Bearer sk-or-test"
        if method == "GET":
            return {"data": {"limit_remaining": self.remaining}}
        self.posts.append(body)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def ok(cost=0.12, content='{"ads": []}'):
    return {"choices": [{"message": {"content": content}}], "usage": {"cost": cost, "prompt_tokens": 1000}}


@pytest.fixture
def client_factory(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_VA_KEY", "sk-or-test")

    def make(transport, cap=100.0):
        led = budget.LocalLedger(tmp_path / "ledger.jsonl", cap)
        return llm.OpenRouter("TEST_VA_KEY", led, request=transport, sleep=lambda s: None), led
    return make


def call(client):
    return client.chat_json(MODEL, [{"type": "text", "text": "hi"}], route="zdr", est_input_tokens=50_000,
                            max_tokens=16_000, label="t")


def test_successful_call_records_real_cost_and_uses_zdr(client_factory):
    t = FakeTransport(replies=[ok(0.12)])
    client, led = client_factory(t)
    plan, usage = call(client)
    assert plan == {"ads": []} and usage["cost"] == 0.12
    assert t.posts[0]["provider"] == {"data_collection": "deny", "zdr": True}
    assert led.spent() == pytest.approx(0.12)


def test_refuses_before_calling_when_key_limit_is_low(client_factory):
    t = FakeTransport(remaining=0.05, replies=[ok()])
    client, led = client_factory(t)
    with pytest.raises(budget.BudgetExceeded, match="OpenRouter key"):
        call(client)
    assert t.posts == [] and led.spent() == 0


def test_refuses_before_calling_when_ledger_is_full(client_factory):
    t = FakeTransport(replies=[ok()])
    client, _ = client_factory(t, cap=0.10)
    with pytest.raises(budget.BudgetExceeded, match="budget"):
        call(client)
    assert t.posts == []


def test_retries_server_errors_but_not_client_errors(client_factory):
    t = FakeTransport(replies=[net.HttpError(502, "bad gateway"), ok(0.2)])
    client, led = client_factory(t)
    call(client)
    # the 502 attempt may have been billed upstream, so it counts at the worst case
    assert len(t.posts) == 2 and led.spent() == pytest.approx(0.2 + llm.estimate_cost(MODEL, 50_000, 16_000))
    t2 = FakeTransport(replies=[net.HttpError(400, "bad request"), ok()])
    client2, _ = client_factory(t2)
    with pytest.raises(llm.LLMError, match="400"):
        call(client2)
    assert len(t2.posts) == 1


def test_timeout_is_not_retried_and_counts_worst_case(client_factory):
    t = FakeTransport(replies=[TimeoutError("slow"), ok()])
    client, led = client_factory(t)
    with pytest.raises(llm.LLMError, match="timed out"):
        call(client)
    assert len(t.posts) == 1
    assert led.spent() == pytest.approx(llm.estimate_cost(MODEL, 50_000, 16_000))


def test_malformed_responses_are_retried_and_their_cost_recorded(client_factory):
    bad = {"choices": [{"message": None}], "usage": {"cost": 0.05}}
    t = FakeTransport(replies=[bad, ok(0.1, "not json at all"), ok(0.12)])
    client, led = client_factory(t)
    plan, _ = call(client)
    assert plan == {"ads": []} and len(t.posts) == 3
    assert led.spent() == pytest.approx(0.05 + 0.1 + 0.12)


def test_interrupted_call_is_still_recorded(client_factory):
    t = FakeTransport(replies=[KeyboardInterrupt()])
    client, led = client_factory(t)
    with pytest.raises(KeyboardInterrupt):
        call(client)
    lines = (led.path.read_text().splitlines())
    assert len(lines) == 1 and json.loads(lines[0])["note"] == "interrupted"
    assert led.spent() == pytest.approx(llm.estimate_cost(MODEL, 50_000, 16_000))   # in-flight: worst case


class BrokenLedger(budget.LocalLedger):
    def settle(self, *a, **k):
        raise OSError("disk full")


def test_ledger_failure_never_hides_the_real_error(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_VA_KEY", "sk-or-test")
    t = FakeTransport(replies=[net.HttpError(400, "bad request")])
    client = llm.OpenRouter("TEST_VA_KEY", BrokenLedger(tmp_path / "l.jsonl", 100), request=t, sleep=lambda s: None)
    with pytest.raises(llm.LLMError, match="400"):
        call(client)


def test_ledger_failure_after_success_is_raised(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_VA_KEY", "sk-or-test")
    t = FakeTransport(replies=[ok()])
    client = llm.OpenRouter("TEST_VA_KEY", BrokenLedger(tmp_path / "l.jsonl", 100), request=t, sleep=lambda s: None)
    with pytest.raises(OSError, match="disk full"):
        call(client)


def test_room_is_checked_for_every_possible_attempt(client_factory):
    one = llm.estimate_cost(MODEL, 50_000, 16_000)
    t = FakeTransport(remaining=one * 2, replies=[ok()])     # enough for one attempt, not for three
    client, _ = client_factory(t)
    with pytest.raises(budget.BudgetExceeded):
        call(client)
    assert t.posts == []


def test_redirects_are_refused_before_following():
    h = net._NoRedirects()
    with pytest.raises(net.BlockedHost, match="redirect"):
        h.redirect_request(None, None, 302, "Found", {}, "https://evil.example.com/steal")


def test_userinfo_trick_is_blocked():
    with pytest.raises(net.BlockedHost):
        net.check_url("https://openrouter.ai@evil.com/api")


def test_unknown_route_rejected(client_factory):
    client, _ = client_factory(FakeTransport(replies=[ok()]))
    with pytest.raises(llm.LLMError, match="route"):
        client.chat_json(MODEL, [], route="anything", est_input_tokens=1)
