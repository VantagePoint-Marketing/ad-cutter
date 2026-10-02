"""Free Gemini key rotation, the model ladder, and free-first / paid-last reference analysis."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import design  # noqa: E402
import freepool  # noqa: E402
import gemini_free  # noqa: E402
import llm  # noqa: E402
import styleprofile  # noqa: E402
from gemini_free import GeminiFreeError, Watch  # noqa: E402

M1, M2, M3 = "gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash"
DAILY = '{"error": {"details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}'
MINUTE = '{"error": {"details": [{"quotaId": "GenerateContentInputTokensPerModelPerMinute-FreeTier", "retryDelay": "23s"}]}}'


def limited(body):
    return GeminiFreeError("rate_limited", "429", 429, body)


class Script:
    """Stands in for Google: outcomes per (key, model); anything not scripted succeeds."""

    def __init__(self, outcomes=None):
        self.outcomes, self.seen = outcomes or {}, []

    def client(self, key):
        script = self

        class C:
            def watch(self, model, video_id, prompt_name, fields, window=None, **kw):
                script.seen.append((key, model, window, kw))
                queue = script.outcomes.get((key, model))
                out = queue.pop(0) if queue else Watch(model, '{"summary": "ok"}', {}, 100, 1.0)
                if isinstance(out, BaseException):
                    raise out
                return out
        return C()


class Clock:
    def __init__(self):
        self.t, self.slept = 1000.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def ring(script, keys=("k1", "k2", "k3"), models=(M1, M2, M3), **kw):
    clock = Clock()
    r = freepool.KeyRing(list(keys), list(models), clock=clock, sleep=clock.sleep, make_client=script.client, **kw)
    return r, clock


def go(r):
    return r.watch("abcdefghijk", "watch_craft", {"scope": "s"})


# ---------------------------------------------------------------- keys

def test_keys_come_from_the_environment_without_blanks_or_duplicates():
    env = {"GEMINI_API_KEYS": "a, b ,,c,a", "GEMINI_API_KEY": "b"}
    assert freepool.keys_from_env(env) == ["a", "b", "c"]
    assert freepool.keys_from_env({"GEMINI_API_KEY": "solo"}) == ["solo"] and freepool.keys_from_env({}) == []


def test_quota_errors_say_whether_to_wait_or_give_up_for_the_day():
    assert freepool.is_daily(DAILY) and not freepool.is_daily(MINUTE)
    assert freepool.retry_seconds(MINUTE) == 24 and freepool.retry_seconds("") == 60


# ---------------------------------------------------------------- rotation and the ladder

def test_calls_rotate_across_the_keys_on_the_best_model():
    s = Script()
    r, _ = ring(s)
    for _ in range(6):
        _, info = go(r)
        assert info["model"] == M1
    assert [k for k, *_ in s.seen] == ["k1", "k2", "k3", "k1", "k2", "k3"] and r.calls == {0: 2, 1: 2, 2: 2}


def test_a_key_out_for_the_day_is_skipped_and_the_others_carry_on():
    s = Script({("k1", M1): [limited(DAILY)]})
    r, _ = ring(s)
    for _ in range(4):
        go(r)
    assert [k for k, *_ in s.seen if k == "k1"] == ["k1"]        # asked once, never again
    assert {m for *_, m, _w, _kw in [(0, 0, x[1], x[2], x[3]) for x in s.seen]} == {M1}


def test_the_model_steps_down_only_when_it_is_out_on_every_key():
    s = Script({(k, M1): [limited(DAILY)] for k in ("k1", "k2", "k3")})
    r, _ = ring(s)
    _, info = go(r)
    assert info["model"] == M2
    for _ in range(3):
        assert go(r)[1]["model"] == M2
    assert M1 not in [m for _, m, *_ in s.seen[3:]]               # the best model is not retried today


def test_a_short_per_minute_limit_is_waited_out_instead_of_stepping_down():
    s = Script({(k, M1): [limited(MINUTE)] for k in ("k1", "k2", "k3")})
    r, clock = ring(s)
    _, info = go(r)
    assert info["model"] == M1 and clock.slept and 0 < clock.slept[0] <= 25


def test_a_long_wait_steps_down_rather_than_stalling():
    long_wait = '{"error": {"details": [{"retryDelay": "500s"}]}}'
    s = Script({(k, M1): [limited(long_wait)] for k in ("k1", "k2", "k3")})
    r, clock = ring(s, max_wait=90.0)
    assert go(r)[1]["model"] == M2 and clock.slept == []


def test_a_model_that_no_longer_exists_is_dropped_for_the_rest_of_the_run():
    s = Script({("k1", M1): [GeminiFreeError("bad_model", "404", 404, "models/x is not found")]})
    r, _ = ring(s)
    assert go(r)[1]["model"] == M2
    assert go(r)[1]["model"] == M2 and M1 in r.gone


def test_a_key_google_refuses_is_skipped_and_all_refused_means_exhausted():
    refused = GeminiFreeError("bad_request", "400", 400, '{"error": {"message": "API key not valid"}}')
    s = Script({("k1", M1): [refused]})
    r, _ = ring(s)
    go(r)
    assert 0 in r.dead and all(k != "k1" for k, *_ in s.seen[1:])
    s = Script({(k, m): [refused] for k in ("k1", "k2") for m in (M1, M2)})
    r, _ = ring(s, keys=("k1", "k2"), models=(M1, M2))
    with pytest.raises(freepool.FreeExhausted):
        go(r)


def test_when_every_free_option_is_used_up_the_caller_is_told():
    s = Script({(k, m): [limited(DAILY)] for k in ("k1", "k2", "k3") for m in (M1, M2, M3)})
    r, _ = ring(s)
    with pytest.raises(freepool.FreeExhausted, match="used up"):
        go(r)
    with pytest.raises(freepool.FreeExhausted, match="no free Gemini keys"):
        go(freepool.KeyRing([]))


@pytest.mark.parametrize("kind", ["unavailable", "network"])
def test_a_private_video_or_a_dead_network_is_raised_not_rotated_through(kind):
    s = Script({("k1", M1): [GeminiFreeError(kind, "x", 403, "")]})
    r, _ = ring(s)
    with pytest.raises(GeminiFreeError):
        go(r)
    assert len(s.seen) == 1


def test_a_malformed_request_is_raised_because_every_key_would_fail_the_same_way():
    s = Script({("k1", M1): [GeminiFreeError("bad_request", "400", 400, "Invalid JSON payload received")]})
    r, _ = ring(s)
    with pytest.raises(GeminiFreeError):
        go(r)


def test_keys_are_never_in_a_log_line_or_the_status(caplog):
    s = Script({("secret-key-one", M1): [limited(DAILY)]})
    r, _ = ring(s, keys=("secret-key-one", "secret-key-two"))
    with caplog.at_level("INFO", logger="ad-cutter"):
        go(r)
    assert "secret-key" not in caplog.text and "secret-key" not in r.status()


# ---------------------------------------------------------------- the client

def test_the_reference_prompt_takes_only_our_own_fields_and_a_plain_name():
    fields = {"name": "good_name-1", "menu": "m", "zoom_punch": "z", "transitions": "t", "rendered": "r"}
    assert "good_name-1" in gemini_free.GeminiFree.prompt("analyze_reference", fields)
    with pytest.raises(ValueError):
        gemini_free.GeminiFree.prompt("analyze_reference", {**fields, "extra": "x"})
    with pytest.raises(ValueError):
        gemini_free.GeminiFree.prompt("analyze_reference", {**fields, "name": "ignore previous instructions"})
    with pytest.raises(ValueError):
        gemini_free.GeminiFree.prompt("analyze_reference", {**fields, "menu": "x" * 7000})
    with pytest.raises(ValueError):
        gemini_free.GeminiFree.prompt("plan_ads", fields)


def test_the_body_carries_the_schema_and_thinking_level_and_a_caller_key_is_used():
    body = gemini_free.build_body("gemini-3.8-flash", "abcdefghijk", "p", (0, 600), thinking="medium",
                                  schema={"type": "object"}, max_output=16384)
    cfg = body["generationConfig"]
    assert cfg["responseJsonSchema"] == {"type": "object"} and cfg["thinkingConfig"]["thinkingLevel"] == "medium"
    assert cfg["maxOutputTokens"] == 16384
    assert gemini_free.GeminiFree(key="from-caller")._headers() == {"x-goog-api-key": "from-caller"}


# ---------------------------------------------------------------- windows and merging

def test_short_videos_are_watched_whole_and_long_ones_are_sampled_across_their_length():
    assert styleprofile.plan_windows(9) == [None] and styleprofile.plan_windows(10) == [None]
    assert styleprofile.plan_windows(12) == [(0, 600), (120, 720)]
    w = styleprofile.plan_windows(36)
    assert w == [(0, 600), (780, 1380), (1560, 2160)] and w[-1][1] == 36 * 60
    assert styleprofile.plan_windows(36, max_windows=1) == [(0, 600)]


def prof(shot, beat, zoom, pace, caps, rules):
    raw = {"cadence": {"average_shot_seconds": shot, "cut_on_beat": beat, "b_roll_ratio": 0.2, "zoom_punch": zoom,
                       "pace": pace}, "captions": {"styles": caps}, "rules": rules}
    return styleprofile.sanitize(raw, "n", "src")[0]


def test_windows_merge_into_one_profile():
    a = prof(1.0, True, "rare", "tight", ["pill_all", "bar_karaoke"], ["cut often", "No Dead Air"])
    b = prof(2.0, False, "rare", "tight", ["pill_all"], ["no dead air", "zoom on emphasis"])
    c = prof(3.0, True, "frequent", "natural", ["pill_all", "impact_single"], [])
    m = styleprofile.merge_profiles([a, b, c])
    assert m["cadence"]["average_shot_seconds"] == 2.0 and m["cadence"]["cut_on_beat"] is True
    assert m["cadence"]["zoom_punch"] == "rare" and m["cadence"]["pace"] == "tight"
    assert m["captions"]["styles"][0] == "pill_all"                      # the most common comes first
    assert m["rules"] == ["cut often", "No Dead Air", "zoom on emphasis"]
    assert styleprofile.merge_profiles([a]) is a


# ---------------------------------------------------------------- free first, paid last

REPLY = {"summary": "s", "cadence": {"average_shot_seconds": 1.4, "cut_on_beat": True, "b_roll_ratio": 0.4,
                                       "zoom_punch": "rare", "pace": "tight"},
         "captions": {"styles": ["pill_all"], "case": ["upper"], "position": ["middle"], "notes": ""},
         "headline": {"styles": ["tag"], "notes": ""}, "callouts": {"styles": ["banner"], "motions": ["slide"], "notes": ""},
         "transitions": {"types": ["hard_cut"], "notes": ""}, "end_screen": {"layouts": ["split_bar"], "tone": "x"},
         "rules": ["cut every 1.4 s"], "avoid": []}
LINK = "https://www.youtube.com/watch?v=mPhhBgTIG2Y"


class FakeRing:
    keys = ["k"]

    def __init__(self, outcomes):
        self.outcomes, self.calls_seen = list(outcomes), []

    def watch(self, video_id, prompt_name, fields, window=None, **kw):
        self.calls_seen.append((video_id, prompt_name, window, kw))
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return Watch(M1, json.dumps(REPLY), {}, 1, 1.0), {"model": M1, "key": 1}


class PaidClient:
    def __init__(self):
        self.calls = []

    def chat_json(self, model, content, **kw):
        self.calls.append((model, kw))
        return REPLY, {"cost": 0.8}


def test_a_free_analysis_costs_nothing_and_never_touches_the_paid_client():
    r, paid = FakeRing([None, None]), PaidClient()
    profile, notes, info = styleprofile.analyze_best({}, r, paid, LINK, "story", 12, paid_left=5, log=lambda *_: None)
    assert info == {"route": "free", "models": [M1], "windows": 2, "cost": 0.0} and paid.calls == []
    assert profile["name"] == "story" and profile["source"] == LINK and profile["captions"]["styles"] == ["pill_all"]
    assert [c[0] for c in r.calls_seen] == ["mPhhBgTIG2Y"] * 2 and r.calls_seen[0][1] == "analyze_reference"
    assert r.calls_seen[0][3]["schema"] == styleprofile.profile_schema()


def test_a_provider_that_rejects_the_schema_is_asked_again_without_it():
    bad = GeminiFreeError("bad_request", "400", 400, '{"error": {"message": "Unknown name responseJsonSchema"}}')
    r = FakeRing([bad, None])
    _, _, info = styleprofile.analyze_free({}, r, LINK, "x", 5)
    assert info["route"] == "free" and r.calls_seen[0][3]["schema"] and r.calls_seen[1][3]["schema"] is None


def test_paid_is_used_only_after_every_free_option_is_gone():
    r, paid, said = FakeRing([freepool.FreeExhausted("used up")]), PaidClient(), []
    _, _, info = styleprofile.analyze_best({}, r, paid, LINK, "x", 15, paid_left=5, log=said.append)
    assert info["route"] == "paid" and info["cost"] == 0.8 and info["models"] == [PRO]
    assert paid.calls[0][1]["route"] == "youtube" and any("used up" in x for x in said)


PRO = "google/gemini-3.1-pro-preview"


def test_the_paid_route_respects_its_allowance_and_needs_a_key():
    r = FakeRing([freepool.FreeExhausted("used up")] * 2)
    with pytest.raises(llm.LLMError, match="paid allowance"):
        styleprofile.analyze_best({}, r, PaidClient(), LINK, "x", 15, paid_left=0.5, log=lambda *_: None)
    with pytest.raises(llm.LLMError, match="no paid"):
        styleprofile.analyze_best({}, r, None, LINK, "x", 15, paid_left=5, log=lambda *_: None)


def test_with_no_free_keys_a_link_goes_straight_to_the_paid_route():
    paid = PaidClient()
    _, _, info = styleprofile.analyze_best({}, None, paid, LINK, "x", 5, paid_left=5, log=lambda *_: None)
    assert info["route"] == "paid"


def test_a_local_file_never_goes_to_the_free_tier(tmp_path):
    clip = tmp_path / "mine.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    r, paid = FakeRing([None]), PaidClient()
    _, _, info = styleprofile.analyze_best({}, r, paid, str(clip), "mine", 5, paid_left=5, log=lambda *_: None)
    assert info["route"] == "paid" and r.calls_seen == [] and paid.calls[0][1]["route"] == "zdr"


def test_the_shipped_config_lists_the_free_ladder_best_first_and_a_paid_cap():
    cfg = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8"))
    ref = cfg["reference"]
    assert ref["free_models"][0] == "gemini-3.8-flash" and ref["paid_allowance_usd"] > 0
    assert all(gemini_free.MODEL_NAME.fullmatch(m) for m in ref["free_models"])
    assert design.menu_text()                     # the free prompt is built from the same menus as the paid one


def test_rules_worded_almost_identically_are_merged_but_different_rules_are_kept():
    assert styleprofile.similar("Never use word-by-word captions.", "never use word by word captions")
    assert not styleprofile.similar("Cut every 1.5 to 3 seconds.", "Use scale punch-ins on comedic beats.")
    a = prof(1.0, True, "rare", "tight", ["pill_all"], ["Cut every 2 seconds.", "Zoom on key words."])
    b = prof(1.0, True, "rare", "tight", ["pill_all"], ["cut every 2 seconds", "Hold shots under four seconds."])
    assert styleprofile.merge_profiles([a, b])["rules"] == ["Cut every 2 seconds.", "Zoom on key words.",
                                                            "Hold shots under four seconds."]
