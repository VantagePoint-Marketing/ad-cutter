"""The reference library: list parsing, parts, note checks, the free-tier and YouTube clients, and the runner's
decisions with an in-memory store (the real SQL is checked against staging Postgres)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gemini_free  # noqa: E402
import library  # noqa: E402
import net  # noqa: E402
import youtube  # noqa: E402
from gemini_free import GeminiFreeError, Watch  # noqa: E402


# ---------------------------------------------------------------- the list and the parts

def test_foundation_list_has_robert_s_17_essential_videos_in_order():
    ids = library.read_foundation()
    assert len(ids) == 17 and ids[0] == "9TG1joKdSCY" and ids[-1] == "m8PAiKUVosk"
    assert "zr-0xNZy-kc" not in ids and "s8EzZB8U82c" not in ids          # optional and left-out lines stay out


def test_read_foundation_skips_comments_and_duplicates(tmp_path):
    f = tmp_path / "list.txt"
    f.write_text("# header\nhttps://www.youtube.com/watch?v=AAAAAAAAAAA  # inline note\n\n"
                 "https://youtu.be/BBBBBBBBBBB\n# https://www.youtube.com/watch?v=CCCCCCCCCCC\n"
                 "https://www.youtube.com/watch?v=AAAAAAAAAAA\nnot a link\n", encoding="utf-8")
    assert library.read_foundation(f) == ["AAAAAAAAAAA", "BBBBBBBBBBB"]


@pytest.mark.parametrize("seconds, expected", [
    (348, [(0, 348)]), (600, [(0, 600)]), (700, [(0, 700)]),
    (1260, [(0, 600), (600, 1260)]), (1500, [(0, 600), (600, 1200), (1200, 1500)]),
    (2184, [(0, 600), (600, 1200), (1200, 1800), (1800, 2184)]),
])
def test_windows_split_long_videos_and_never_leave_a_tiny_last_part(seconds, expected):
    assert library.windows(seconds, 600, 120) == expected


def test_scope_text_tells_gemini_which_part_and_how_to_count():
    assert library.scope_text((0, 348), 348, 1, 1) == "Watch the whole video (about 5:48)."
    text = library.scope_text((600, 1200), 2184, 2, 4)
    assert "part 2 of 4" in text and "from 10:00 to 20:00" in text and "0 = the moment this part begins" in text
    assert library.clock(3725) == "1:02:05"


# ---------------------------------------------------------------- note checks

def note(seconds=600, first="when I was younger", last="see you next time", lessons=None, **extra):
    return json.dumps({"watched": {"seconds": seconds, "first_words": first, "last_words": last},
                       "summary": "A video about pacing.", "lessons": lessons if lessons is not None else [
                           {"at_s": 41, "topic": "Pacing", "principle": "Pacing keeps attention.", "lever": "pause_trim",
                            "how_we_apply": "Trim pauses."},
                           {"at_s": 300, "topic": "Motivation", "principle": "Every cut needs a reason.",
                            "lever": "segment_order", "how_we_apply": "Order segments by cause."}], **extra})


def test_a_good_note_passes_and_lesson_times_become_whole_video_times():
    out, ok, problems = library.validate_note(note(), (600, 1200), video_tokens=600 * 90)
    assert ok and problems == []
    assert [x["at_s"] for x in out["lessons"]] == [641.0, 900.0]
    assert out["lessons"][0]["lever"] == "pause_trim" and out["window"] == [600, 1200]


def test_whole_video_times_are_recognised_and_converted():
    lessons = [{"at_s": t, "principle": f"lesson {t}", "lever": "hook"} for t in (650, 700, 1100)]
    out, ok, _ = library.validate_note(note(lessons=lessons), (600, 1200), video_tokens=600 * 90)
    assert ok and [x["at_s"] for x in out["lessons"]] == [650.0, 700.0, 1100.0]


def test_lessons_outside_the_part_are_dropped_and_too_many_rejects_the_note():
    lessons = [{"at_s": t, "principle": "p", "lever": "hook"} for t in (10, 20, 420, 508, 529)]
    out, ok, problems = library.validate_note(note(seconds=350, lessons=lessons), (0, 348), video_tokens=348 * 90)
    assert not ok and out["dropped"] == 3 and len(out["lessons"]) == 2
    assert problems == ["3 of 5 lessons had no text or a time outside this part"]
    out, ok, _ = library.validate_note(note(seconds=350, lessons=lessons[:4]), (0, 348), video_tokens=348 * 90)
    assert ok is False                                   # 2 of 4 dropped is still over 40%


@pytest.mark.parametrize("bad, expected", [
    (note(seconds=400), "reported 400.0 seconds"),
    (note(first=""), "no first or last words"),
    ("Sure! Here are the lessons.", "not JSON"),
    ("[1, 2]", "not JSON"),
])
def test_a_note_that_does_not_prove_it_was_watched_is_rejected(bad, expected):
    _, ok, problems = library.validate_note(bad, (0, 600), video_tokens=None)
    assert not ok and any(expected in p for p in problems)


def test_too_few_video_tokens_means_it_did_not_really_watch():
    _, ok, problems = library.validate_note(note(), (0, 600), video_tokens=600 * 20)
    assert not ok and any("20 video tokens per second" in p for p in problems)
    _, ok, problems = library.validate_note(note(), (0, 600), video_tokens=None)
    assert not ok and any("no video token count" in p for p in problems)


def test_unknown_levers_become_none_and_lessons_are_capped():
    many = [{"at_s": i, "principle": f"p{i}", "lever": "music"} for i in range(30)]
    out, ok, _ = library.validate_note(note(lessons=many), (0, 600), video_tokens=600 * 90)
    assert ok and len(out["lessons"]) == library.MAX_LESSONS and {x["lever"] for x in out["lessons"]} == {"none"}


# ---------------------------------------------------------------- the free-tier client

def test_the_request_only_carries_a_youtube_link_a_window_and_our_prompt():
    body = gemini_free.build_body("gemini-3.5-flash", "QR8LxximqWI", "PROMPT", window=(600, 1200))
    part, text = body["contents"][0]["parts"]
    assert part == {"file_data": {"file_uri": "https://www.youtube.com/watch?v=QR8LxximqWI"},
                    "video_metadata": {"start_offset": "600s", "end_offset": "1200s"}}
    assert text == {"text": "PROMPT"} and body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert "thinkingConfig" not in gemini_free.build_body("gemini-2.5-flash", "QR8LxximqWI", "P")["generationConfig"]
    for bad in ("../../x", "https://evil.example/v", "QR8Lxxim", ""):
        with pytest.raises(ValueError):
            gemini_free.build_body("gemini-3.5-flash", bad, "P")
    with pytest.raises(ValueError):
        gemini_free.GeminiFree.prompt("plan_ads", {"scope": "s"})     # only the watch prompts may be sent
    for fields in ({}, {"scope": "s", "extra": "free text"}, {"scope": 5}, {"scope": "x" * 501}):
        with pytest.raises(ValueError):
            gemini_free.GeminiFree.prompt("watch_craft", fields)      # and only with our one short field
    with pytest.raises(ValueError):
        gemini_free.watch_url("QR8LxximqWI\n")


def fake_request(reply=None, error=None):
    calls = []

    def request(method, url, headers=None, body=None, timeout=60):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if error:
            raise error
        return reply
    request.calls = calls
    return request


def test_watch_returns_the_text_and_the_video_tokens(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    reply = {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}, {"text": "{\"a\": 1}"}]}}],
             "usageMetadata": {"promptTokenCount": 1736, "promptTokensDetails": [
                 {"modality": "VIDEO", "tokenCount": 1676}, {"modality": "TEXT", "tokenCount": 60}]}}
    req = fake_request(reply)
    w = gemini_free.GeminiFree(request=req).watch("gemini-3.5-flash", "jNQXAC9IVRw", "watch_craft", {"scope": "S"})
    assert w.text == "{\"a\": 1}" and w.video_tokens == 1676 and w.model == "gemini-3.5-flash"
    call = req.calls[0]
    assert call["url"].endswith("/models/gemini-3.5-flash:generateContent") and "key" not in call["url"]
    assert call["headers"] == {"x-goog-api-key": "test-key"}
    assert "S" in call["body"]["contents"][0]["parts"][1]["text"]


@pytest.mark.parametrize("error, kind", [
    (net.HttpError(503, '{"error": {"status": "UNAVAILABLE", "message": "high demand"}}'), "busy"),
    (net.HttpError(429, "{}"), "rate_limited"),
    (net.HttpError(403, '{"error": {"status": "PERMISSION_DENIED"}}'), "unavailable"),
    (net.HttpError(400, '{"error": {"message": "Thinking level is not supported"}}'), "bad_request"),
    (net.HttpError(400, '{"error": {"status": "INVALID_ARGUMENT", "message": "API key expired."}}'), "bad_request"),
    (net.HttpError(404, '{"error": {"message": "models/gemini-9-flash is not found for API version v1beta"}}'),
     "bad_model"),
    (net.HttpError(404, '{"error": {"status": "NOT_FOUND"}}'), "unavailable"),
    (TimeoutError("timed out"), "network"),
])
def test_errors_are_sorted_into_kinds_the_runner_acts_on(monkeypatch, error, kind):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    with pytest.raises(GeminiFreeError) as info:
        gemini_free.GeminiFree(request=fake_request(error=error)).watch("gemini-3.5-flash", "jNQXAC9IVRw",
                                                                       "watch_craft", {"scope": ""})
    assert info.value.kind == kind


def test_a_reply_without_text_is_a_bad_reply(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    req = fake_request({"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]})
    with pytest.raises(GeminiFreeError, match="SAFETY") as info:
        gemini_free.GeminiFree(request=req).watch("gemini-3.5-flash", "jNQXAC9IVRw", "watch_craft", {"scope": ""})
    assert info.value.kind == "bad_reply"


# ---------------------------------------------------------------- the YouTube client

@pytest.mark.parametrize("value, seconds", [("PT19S", 19), ("PT5M48S", 348), ("PT1H12M13S", 4333), ("P1DT1M", 86460),
                                            ("PT0S", 0), ("", None), ("garbage", None)])
def test_iso_durations(value, seconds):
    assert youtube.iso_seconds(value) == (None if seconds is None else seconds) or (seconds == 0)


@pytest.mark.parametrize("text, vid", [
    ("https://www.youtube.com/watch?v=m8PAiKUVosk", "m8PAiKUVosk"), ("https://youtu.be/m8PAiKUVosk?t=3", "m8PAiKUVosk"),
    ("https://www.youtube.com/shorts/m8PAiKUVosk", "m8PAiKUVosk"), ("m8PAiKUVosk", "m8PAiKUVosk"),
    ("https://evil.example/watch?v=m8PAiKUVosk", None), ("https://www.youtube.com/watch?v=short", None)])
def test_video_ids_from_links(text, vid):
    assert youtube.video_id(text) == vid


def test_videos_sends_the_key_in_a_header_and_maps_the_fields(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-key")
    req = fake_request({"items": [{"id": "QR8LxximqWI", "snippet": {"title": "5 MORE Editing Mistakes",
                                   "channelTitle": "HillierSmith", "publishedAt": "2021-03-31T00:00:00Z",
                                   "liveBroadcastContent": "none"},
                                   "contentDetails": {"duration": "PT5M48S"}, "statistics": {"viewCount": "1850362"},
                                   "status": {"privacyStatus": "public", "uploadStatus": "processed"}}]})
    got = youtube.videos(["QR8LxximqWI", "bad id"], request=req)
    assert got == {"QR8LxximqWI": {"title": "5 MORE Editing Mistakes", "channel": "HillierSmith", "seconds": 348,
                                   "views": 1850362, "published_at": "2021-03-31T00:00:00Z", "public": True,
                                   "processed": True, "live": False}}
    call = req.calls[0]
    assert call["headers"] == {"X-Goog-Api-Key": "yt-key"} and "yt-key" not in call["url"]
    assert "id=QR8LxximqWI&" in call["url"]
    with pytest.raises(ValueError):
        youtube.videos(["AAAAAAAAAAA"] * 51, request=req)


# ---------------------------------------------------------------- the runner

class FakeStore:
    def __init__(self, sources=None):
        self.sources = {s["id"]: {"tier": 1, "attempts": 0, "status": "new", "reason": None, "stale": False, **s}
                        for s in (sources or [])}
        self.notes, self.quota, self.released, self.upserts = [], {}, [], []

    def sweep_stale(self):
        pass

    def youtube_rows(self, tier):
        return {s["external_id"]: {"id": s["id"], "status": s["status"], "reason": s["reason"], "stale": s["stale"]}
                for s in self.sources.values() if s["tier"] == tier}

    def upsert_youtube(self, tier, picked_by, vid, meta, status, reason):
        self.upserts.append((vid, status, reason))
        existing = next((s for s in self.sources.values() if s["external_id"] == vid), None)
        if existing:
            if status in ("failed", "skipped"):
                existing.update(status=status, reason=reason)
            elif existing["status"] == "skipped":
                existing.update(status="new", reason=None)
            existing.update(seconds=(meta or {}).get("seconds"), stale=False)
        else:
            sid = max(self.sources, default=0) + 1
            self.sources[sid] = {"id": sid, "tier": tier, "external_id": vid, "title": (meta or {}).get("title", ""),
                                 "seconds": (meta or {}).get("seconds"), "attempts": 0, "status": status,
                                 "reason": reason, "stale": False}

    def set_status(self, source_id, status, reason):
        self.sources[source_id].update(status=status, reason=reason)

    def claim(self, worker):
        for s in sorted(self.sources.values(), key=lambda s: (s["tier"], s["attempts"], s["id"])):
            if s["status"] == "new" and s.get("seconds"):
                s["status"] = "working"
                return {k: s[k] for k in ("id", "tier", "external_id", "title", "seconds", "attempts")}
        return None

    def ok_windows(self, source_id):
        return {n["window"][0] for n in self.notes if n["source_id"] == source_id and n["ok"]}

    def add_note(self, source_id, model, window, note, ok, problems, video_tokens):
        self.notes.append({"source_id": source_id, "model": model, "window": window, "note": note, "ok": ok,
                           "problems": problems})

    def release(self, source_id, status, reason=None, failed_attempt=False, reset_attempts=False):
        s = self.sources[source_id]
        s.update(status=status, reason=reason,
                 attempts=0 if reset_attempts else s["attempts"] + (1 if failed_attempt else 0))
        self.released.append((source_id, status))

    def quota_used(self, api):
        return self.quota.get(api, 0.0)

    def quota_add(self, api, amount):
        self.quota[api] = self.quota.get(api, 0.0) + amount


class FakeClient:
    """Answers watch calls from a script: each entry is an exception to raise or a note to return."""

    def __init__(self, script, key_works=True):
        self.script, self.calls, self.key_works = list(script), [], key_works

    def watch(self, model, video_id, prompt_name, fields, window=None):
        self.calls.append({"model": model, "video": video_id, "window": window, "scope": fields["scope"]})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        start, end = window or (0, item.get("_seconds", 348))
        return Watch(model, note(seconds=end - start), {}, (end - start) * 90, 42.0)

    def key_ok(self):
        if not self.key_works:
            raise RuntimeError("API key not valid")
        return "ok"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def runner(store, client, clock=None, **cfg):
    r = library.Runner(None, {"library": {"watch_models": ["m-a", "m-b", "m-c"], **cfg}}, client=client, store=store,
                       youtube_videos=lambda ids: {}, foundation=Path("unused"), clock_fn=clock or Clock())
    r.synced_at = float("inf")              # no list sync unless a test asks for one
    return r


def busy(model="m"):
    return GeminiFreeError("busy", f"{model}: HTTP 503 UNAVAILABLE", 503)


def test_a_short_video_is_studied_in_one_step():
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "5 MORE Editing Mistakes", "seconds": 348}])
    client = FakeClient([{"_seconds": 348}])
    r = runner(store, client)
    assert r.step().startswith("studied part 1/1 of '5 MORE Editing Mistakes' with m-a")
    assert store.sources[1]["status"] == "done" and store.notes[0]["ok"]
    assert client.calls[0]["window"] is None and "whole video" in client.calls[0]["scope"]
    assert store.quota[library.VIDEO_SECONDS] == 348


def test_a_long_video_is_studied_one_part_per_step():
    store = FakeStore([{"id": 1, "external_id": "IROKEjmIIlM", "title": "When Editing Ruins Your Video", "seconds": 2184}])
    client = FakeClient([{}, {}, {}, {}])
    r = runner(store, client)
    for n in range(1, 5):
        assert r.step().startswith(f"studied part {n}/4")
        assert store.sources[1]["status"] == ("done" if n == 4 else "new")
    assert [c["window"] for c in client.calls] == [(0, 600), (600, 1200), (1200, 1800), (1800, 2184)]
    assert store.quota[library.VIDEO_SECONDS] == 2184


def test_a_busy_model_falls_back_to_the_next():
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])
    client = FakeClient([busy("m-a"), {"_seconds": 348}])
    assert "with m-b" in runner(store, client).step()


def test_when_every_model_is_busy_the_video_waits_and_the_library_pauses():
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])
    clock = Clock()
    r = runner(store, FakeClient([busy(), busy(), busy()]), clock)
    assert r.step().startswith("paused 10 min: Gemini is busy or unreachable")
    assert store.sources[1]["status"] == "new" and store.sources[1]["attempts"] == 0 and not r.ready()
    assert library.VIDEO_SECONDS not in store.quota
    clock.t += 601
    assert r.ready()


def test_rate_limits_pause_longer_each_time():
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])
    clock = Clock()
    limited = GeminiFreeError("rate_limited", "HTTP 429", 429)
    client = FakeClient([limited] * 9)                  # free-tier limits are per model: every model is tried
    r = runner(store, client, clock)
    assert r.step().startswith("paused 30 min") and len(client.calls) == 3
    clock.t += 1801
    assert r.step().startswith("paused 30 min")
    clock.t += 1801
    assert r.step().startswith("paused 360 min") and store.sources[1]["status"] == "new"


def test_an_unwatchable_video_fails_but_a_broken_key_only_pauses():
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])
    gone = GeminiFreeError("unavailable", "HTTP 403 PERMISSION_DENIED", 403)
    client = FakeClient([gone] * 3, key_works=True)
    r = runner(store, client)
    assert "cannot be watched" in r.step() and store.sources[1]["status"] == "failed"
    assert len(client.calls) == 3                       # every model was asked before the video was blamed
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])
    r = runner(store, FakeClient([gone] * 3, key_works=False))
    assert "the Gemini key does not work" in r.step() and store.sources[1]["status"] == "new"


def test_an_expired_key_reported_as_400_pauses_and_costs_no_attempt():
    store = FakeStore([{"id": i, "external_id": f"{i:0>11}", "title": "t", "seconds": 348} for i in (1, 2, 3)])
    expired = GeminiFreeError("bad_request", "HTTP 400 INVALID_ARGUMENT: API key expired.", 400)
    clock = Clock()
    r = runner(store, FakeClient([expired] * 9, key_works=False), clock)
    for _ in range(3):
        assert "the Gemini key does not work" in r.step()
        clock.t += 3601
    assert all(s["status"] == "new" and s["attempts"] == 0 for s in store.sources.values())


def test_a_retired_model_name_is_skipped_and_a_wholly_wrong_list_pauses():
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])
    gone_model = GeminiFreeError("bad_model", "HTTP 404 models/m-a is not found", 404)
    assert "with m-b" in runner(store, FakeClient([gone_model, {"_seconds": 348}])).step()
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])
    msg = runner(store, FakeClient([gone_model] * 3)).step()
    assert msg.startswith("paused 60 min: no usable Gemini model") and store.sources[1]["attempts"] == 0


def test_a_timeout_stops_trying_more_models():
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])
    client = FakeClient([GeminiFreeError("network", "m-a: TimeoutError: timed out")])
    assert runner(store, client).step().startswith("paused 10 min") and len(client.calls) == 1


def test_three_different_videos_failing_in_a_row_trips_the_breaker():
    store = FakeStore([{"id": i, "external_id": f"{i:0>11}", "title": f"v{i}", "seconds": 348} for i in (1, 2, 3, 4)])
    refused = GeminiFreeError("bad_request", "HTTP 400 User location is not supported", 400)
    clock = Clock()
    r = runner(store, FakeClient([refused] * 15), clock)              # 5 steps x 3 models
    assert "no model could watch it this time" in r.step()            # video 1: one attempt
    assert "no model could watch it this time" in r.step()            # video 2 (fewest attempts first): one attempt
    assert "3 different videos failed in a row" in r.step()           # video 3: pause, no attempt
    assert [store.sources[i]["attempts"] for i in (1, 2, 3, 4)] == [1, 1, 0, 0]
    assert all(s["status"] == "new" for s in store.sources.values())
    clock.t += 3601                                                   # after the pause the count starts again:
    assert "no model could watch it this time" in r.step()            # video 3 gets its attempt
    assert "no model could watch it this time" in r.step()            # and video 4 is reached
    assert [store.sources[i]["attempts"] for i in (1, 2, 3, 4)] == [1, 1, 1, 1]


def test_a_run_of_genuinely_unwatchable_videos_never_stalls_the_library():
    store = FakeStore([{"id": i, "external_id": f"{i:0>11}", "title": f"v{i}", "seconds": 348} for i in (1, 2, 3, 4)])
    gone = GeminiFreeError("unavailable", "HTTP 403 PERMISSION_DENIED", 403)
    clock = Clock()
    # videos 1 and 2 fail; video 3 trips the breaker (an hour's pause), then fails on its own; video 4 is studied
    client = FakeClient([gone] * 12 + [{"_seconds": 348}])
    r = runner(store, client, clock)
    for _ in range(5):
        r.step()
        clock.t += 3601
    assert [store.sources[i]["status"] for i in (1, 2, 3, 4)] == ["failed", "failed", "failed", "done"]


def test_a_good_part_resets_the_attempt_count():
    store = FakeStore([{"id": 1, "external_id": "IROKEjmIIlM", "title": "t", "seconds": 2184, "attempts": 2}])
    runner(store, FakeClient([{}])).step()
    assert store.sources[1]["attempts"] == 0 and store.sources[1]["status"] == "new"


def test_an_empty_reply_counts_against_the_free_video_hours():
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])
    empty = GeminiFreeError("bad_reply", "m: no text came back (MAX_TOKENS)")
    runner(store, FakeClient([empty] * 3)).step()
    assert store.quota[library.VIDEO_SECONDS] == 348 and store.sources[1]["attempts"] == 1


def test_a_failed_sync_does_not_stop_already_listed_videos_being_studied():
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])

    def broken(ids):
        raise net.HttpError(403, '{"error": {"message": "quotaExceeded"}}')
    lst = Path(__file__).resolve().parents[1] / "library" / "foundation.txt"
    clock = Clock()
    r = runner(store, FakeClient([{"_seconds": 348}]), clock)
    r.synced_at, r.youtube_videos, r.foundation = 0.0, broken, lst
    assert r.step().startswith("studied part 1/1")
    assert clock.t - r.synced_at == pytest.approx(5 * 3600)           # tries the sync again in an hour


def test_rejected_notes_retry_with_another_model_then_give_up():
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])

    class LazyClient(FakeClient):
        def watch(self, model, video_id, prompt_name, fields, window=None):
            self.calls.append({"model": model})
            return Watch(model, note(seconds=30), {}, 348 * 90, 5.0)      # claims 30 s of a 348 s video

    client = LazyClient([])
    r = runner(store, client)
    for expected in ("new", "new", "failed"):
        assert r.step().startswith("note rejected") and store.sources[1]["status"] == expected
    assert [c["model"] for c in client.calls] == ["m-a", "m-b", "m-c"]
    assert "failed the watch checks 3 times" in store.sources[1]["reason"]
    assert len(store.notes) == 3 and not any(n["ok"] for n in store.notes)
    assert store.quota[library.VIDEO_SECONDS] == 3 * 348          # Google counted every watch


def test_the_daily_free_video_limit_is_respected():
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])
    store.quota[library.VIDEO_SECONDS] = 7 * 3600 - 100
    client = FakeClient([])
    assert "free video are used up" in runner(store, client).step()
    assert client.calls == [] and store.sources[1]["status"] == "new"


def test_nothing_to_study_pauses_for_half_an_hour():
    assert runner(FakeStore(), FakeClient([])).step() == "paused 30 min: nothing left to study"


def test_a_shutdown_puts_the_video_back_and_is_not_swallowed():
    class Shutdown(BaseException):
        pass
    store = FakeStore([{"id": 1, "external_id": "QR8LxximqWI", "title": "t", "seconds": 348}])
    with pytest.raises(Shutdown):
        runner(store, FakeClient([Shutdown()])).step()
    assert store.sources[1]["status"] == "new"


def test_sync_adds_new_videos_skips_bad_ones_and_retires_removed_ones(tmp_path):
    lst = tmp_path / "foundation.txt"
    lst.write_text("AAAAAAAAAAA\nBBBBBBBBBBB\nCCCCCCCCCCC\nDDDDDDDDDDD\n", encoding="utf-8")
    store = FakeStore([{"id": 9, "external_id": "ZZZZZZZZZZZ", "title": "old", "seconds": 60, "status": "done"}])
    meta = {"AAAAAAAAAAA": {"title": "A", "seconds": 600, "public": True, "processed": True, "live": False},
            "BBBBBBBBBBB": {"title": "B", "seconds": 120 * 60, "public": True, "processed": True, "live": False},
            "CCCCCCCCCCC": {"title": "C", "seconds": 60, "public": False, "processed": True, "live": False}}
    asked = []

    def videos(ids):
        asked.append(list(ids))
        return {k: v for k, v in meta.items() if k in ids}
    r = runner(store, FakeClient([]))
    r.foundation, r.youtube_videos = lst, videos
    assert r.sync_foundation() == "foundation list: 4 videos, metadata fetched for 4"
    assert asked == [["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC", "DDDDDDDDDDD"]]
    assert {v: (s, why) for v, s, why in store.upserts} == {
        "AAAAAAAAAAA": ("new", None), "BBBBBBBBBBB": ("skipped", "longer than 90 minutes"),
        "CCCCCCCCCCC": ("skipped", "not a public, finished video"),
        "DDDDDDDDDDD": ("skipped", "not on YouTube (private, removed or a wrong link)")}
    assert store.sources[9]["status"] == "skipped" and store.sources[9]["reason"] == library.REMOVED
    assert store.quota[library.YT_UNITS] == 1
    store.upserts.clear()
    asked.clear()
    r.sync_foundation()                         # only the skipped ones are looked at again (they may be back)
    assert asked == [["BBBBBBBBBBB", "CCCCCCCCCCC", "DDDDDDDDDDD"]]
    meta["DDDDDDDDDDD"] = {"title": "D", "seconds": 300, "public": True, "processed": True, "live": False}
    r.sync_foundation()
    assert next(s for s in store.sources.values() if s["external_id"] == "DDDDDDDDDDD")["status"] == "new"


def test_sync_stops_at_the_daily_youtube_quota(tmp_path):
    lst = tmp_path / "foundation.txt"
    lst.write_text("AAAAAAAAAAA\n", encoding="utf-8")
    store = FakeStore()
    store.quota[library.YT_UNITS] = 2000
    r = runner(store, FakeClient([]))
    r.foundation = lst
    assert "quota for today used up" in r.sync_foundation() and store.upserts == []


def test_the_library_is_off_without_the_switch_and_both_keys(monkeypatch):
    for k in ("LIBRARY_ENABLED", "GEMINI_API_KEY", "YOUTUBE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert library.why_off() == "LIBRARY_ENABLED is not 1"
    monkeypatch.setenv("LIBRARY_ENABLED", "1")
    assert library.why_off() == "GEMINI_API_KEY and YOUTUBE_API_KEY not set"
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("YOUTUBE_API_KEY", "y")
    assert library.enabled()


def test_migration_003_exists_after_002():
    import migrate
    names = [p.name for p in migrate.pending(set())]
    assert names[:3] == ["001_worker.sql", "002_web.sql", "003_library.sql"]
