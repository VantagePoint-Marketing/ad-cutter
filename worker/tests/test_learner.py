"""The self-learning step: YouTube search, the watching prompts, the checks on what Gemini reports, filing into the hub, and
the goal queue. YouTube, Gemini and the database are stood in for; nothing here calls out."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gemini_free  # noqa: E402
import hub_learn  # noqa: E402
import learner  # noqa: E402
import library  # noqa: E402
import youtube  # noqa: E402
from gemini_free import GeminiFreeError  # noqa: E402


# ---------------------------------------------------------------- YouTube search

def test_search_sends_the_key_in_a_header_and_returns_clean_ids_in_order():
    seen = {}

    def request(method, url, headers=None, timeout=None, body=None):
        seen.update(method=method, url=url, headers=headers)
        return {"items": [{"id": {"videoId": "AAAAAAAAAAA"}}, {"id": {"videoId": "bad id"}}, {"id": {"kind": "channel"}},
                          {"id": {"videoId": "AAAAAAAAAAA"}}, {"id": {"videoId": "BBBBBBBBBBB"}}]}
    ids = youtube.search("gsap  text\nanimation", duration="medium", order="viewCount", published_after="2026-01-01T00:00:00Z",
                         max_results=99, request=request, key_env="PATH")
    assert ids == ["AAAAAAAAAAA", "BBBBBBBBBBB"]
    assert seen["method"] == "GET" and "key=" not in seen["url"] and "X-Goog-Api-Key" in seen["headers"]
    url = seen["url"]
    assert "q=gsap+text+animation" in url and "videoDuration=medium" in url and "order=viewCount" in url
    assert "maxResults=25" in url and "publishedAfter=2026-01-01T00%3A00%3A00Z" in url and "type=video" in url


def test_search_refuses_odd_parameters_and_an_empty_query_costs_nothing():
    called = []
    assert youtube.search("   ", request=lambda *a, **k: called.append(1)) == [] and not called
    with pytest.raises(ValueError):
        youtube.search("x", duration="forever", request=lambda *a, **k: {})
    with pytest.raises(ValueError):
        youtube.search("x", order="random", request=lambda *a, **k: {})
    assert youtube.SEARCH_UNITS == 100


# ---------------------------------------------------------------- the watching prompts

def test_the_learning_prompts_take_a_scope_and_a_plain_focus_and_nothing_else():
    fields = {"scope": "Watch the whole video (about 5:00).", "focus": "Animated captions for reels (and shorts)."}
    for name in ("watch_learn", "watch_reference"):
        text = gemini_free.GeminiFree.prompt(name, fields)
        assert "Animated captions for reels" in text and "Watch the whole video" in text
        assert "{focus}" not in text and "{scope}" not in text and '"watched": {' in text      # filled, and the JSON example survives
    assert gemini_free.GeminiFree.prompt("watch_craft", {"scope": "x"})
    for bad in ({"scope": "x"}, {"scope": "x", "focus": "a", "extra": "b"}, {"scope": "x", "focus": "<script>alert(1)</script>"},
                {"scope": "x", "focus": "a" * 201}, {"scope": "x", "focus": 5}, {"scope": "x" * 501, "focus": "ok"},
                {"scope": "x", "focus": "line one\nline two"}):
        with pytest.raises(ValueError):
            gemini_free.GeminiFree.prompt("watch_learn", bad)
    with pytest.raises(ValueError):
        gemini_free.GeminiFree.prompt("watch_craft", {"scope": "x", "focus": "a"})      # the old prompt never takes a focus
    with pytest.raises(ValueError):
        gemini_free.GeminiFree.prompt("plan_ads", fields)                                # no other prompt can go to the free tier


def test_the_goal_becomes_a_safe_focus_sentence():
    assert learner.focus_text("GSAP text & card animation: timelines, easing") == "GSAP text & card animation: timelines, easing"
    assert learner.focus_text("evil <b>bold</b>\n{brace} `tick`") == "evil b bold /b brace tick"
    assert len(learner.focus_text("x" * 500)) == 200 and learner.focus_text("<<<>>>") == "video editing craft"
    gemini_free.GeminiFree.prompt("watch_learn", {"scope": "s", "focus": learner.focus_text("anything <at> all {here}")})


# ---------------------------------------------------------------- checking what Gemini reports

def tutorial_reply(**over):
    note = {"watched": {"seconds": 300, "first_words": "welcome to this lesson", "last_words": "see you next time"},
            "summary": "How to animate captions.",
            "techniques": [{"at_s": 40, "name": "Caption pop", "what": "Words scale in.", "when_to_use": "On key words.",
                            "how": ["Split text | Scale 1.15 to 1 with back.out", "Highlight the key word"], "tags": ["captions", "motion", "nonsense"]},
                           {"at_s": 9999, "name": "Out of range", "what": "x"}],
            "lessons": [{"at_s": 100, "topic": "Timing", "principle": "Match the beat.", "tags": ["pacing"]}, "junk"]}
    note.update(over)
    return json.dumps(note)


def test_a_tutorial_note_is_kept_only_if_it_proves_the_watch_and_is_cleaned():
    note, ok, problems = hub_learn.validate_tutorial(tutorial_reply(), (0, 300), 300 * 90)
    assert ok and problems == []
    assert [t["name"] for t in note["techniques"]] == ["Caption pop"] and note["dropped"] == 1
    t = note["techniques"][0]
    assert t["tags"] == ["captions", "motion"] and t["how"] == "Split text | Scale 1.15 to 1 with back.out | Highlight the key word"
    assert note["lessons"][0]["at_s"] == 100.0
    _, ok, problems = hub_learn.validate_tutorial(tutorial_reply(), (0, 300), 300 * 5)
    assert not ok and "video tokens per second" in problems[0]
    _, ok, problems = hub_learn.validate_tutorial(tutorial_reply(watched={"seconds": 40, "first_words": "", "last_words": "x"}), (0, 300), 27000)
    assert not ok and len(problems) == 2
    _, ok, problems = hub_learn.validate_tutorial("not json at all", (0, 300), 27000)
    assert not ok and problems == ["the reply was not JSON"]


def test_part_times_are_made_whole_video_times_whichever_way_gemini_counted():
    relative = tutorial_reply(watched={"seconds": 600, "first_words": "a", "last_words": "b"})
    note, ok, _ = hub_learn.validate_tutorial(relative, (600, 1200), 600 * 90)
    assert ok and note["techniques"][0]["at_s"] == 640.0                         # 40 s into the part -> 640 s into the video
    whole = tutorial_reply(watched={"seconds": 600, "first_words": "a", "last_words": "b"},
                           techniques=[{"at_s": 640, "name": "Caption pop", "what": "Words scale in."}],
                           lessons=[{"at_s": 700, "principle": "Match the beat."}])
    note, ok, _ = hub_learn.validate_tutorial(whole, (600, 1200), 600 * 90)
    assert ok and note["techniques"][0]["at_s"] == 640.0 and note["lessons"][0]["at_s"] == 700.0


def test_too_many_dropped_items_reject_the_note():
    bad = tutorial_reply(techniques=[{"at_s": 5000, "name": "a", "what": "b"}] * 3, lessons=[{"at_s": 10, "principle": "ok"}])
    _, ok, problems = hub_learn.validate_tutorial(bad, (0, 300), 27000)
    assert not ok and "had no text or a time outside" in problems[0]


def reference_reply(**over):
    note = {"watched": {"seconds": 45, "first_words": "stop scrolling", "last_words": "link in bio"}, "summary": "A trader explains lag.",
            "format": "talking head", "hook": {"at_s": 0.5, "what": "Asks a question", "why_it_works": "Curiosity"},
            "pacing": {"cuts_per_10s": 4, "average_shot_seconds": 2.5, "feel": "fast"},
            "captions": {"present": True, "style": "Yellow, centre-low, pop on each word"},
            "graphics": ["Stat card at 12 s"], "sound": {"music": "quiet bed", "effects": "few", "voice": "clean"},
            "cta": "Asks to tap the link", "palette": ["yellow", "navy"], "works_because": ["Fast hook"],
            "borrow": [{"name": "Caption pop", "what": "Words scale in.", "tags": ["captions"]}, {"name": "", "what": "x"}],
            "do_not_copy": ["Says guaranteed profit"]}
    note.update(over)
    return json.dumps(note)


def test_a_reference_note_needs_proof_and_a_hook_and_is_cleaned():
    note, ok, problems = hub_learn.validate_reference(reference_reply(), (0, 45), 45 * 90)
    assert ok and problems == [] and [b["name"] for b in note["borrow"]] == ["Caption pop"]
    assert note["pacing"]["cuts_per_10s"] == 4.0 and note["do_not_copy"] == ["Says guaranteed profit"]
    _, ok, problems = hub_learn.validate_reference(reference_reply(hook={}), (0, 45), 45 * 90)
    assert not ok and any("did not describe the hook" in p for p in problems)
    _, ok, _ = hub_learn.validate_reference(reference_reply(), (0, 45), None)
    assert not ok
    note, ok, _ = hub_learn.validate_reference(reference_reply(borrow=[{"name": f"n{i}", "what": "w"} for i in range(20)]), (0, 45), 45 * 90)
    assert len(note["borrow"]) == hub_learn.MAX_BORROW


# ---------------------------------------------------------------- filing into the hub

class FakeHub:
    def __init__(self):
        self.items, self.links, self.goals, self.next = {}, [], [], 1

    def get(self, kind, slug):
        it = self.items.get((kind, slug))
        return json.loads(json.dumps(it)) if it else None

    def upsert(self, kind, slug, title, body="", tags=(), meta=None, origin="seed", status="active", confidence=None):
        it = self.items.get((kind, slug))
        item_id = it["id"] if it else self.next
        self.next += 0 if it else 1
        self.items[(kind, slug)] = {"id": item_id, "kind": kind, "slug": slug, "title": title, "body": body, "tags": list(tags),
                                    "meta": dict(meta or {}), "origin": origin, "status": status}
        return item_id

    def search(self, query, kinds=None, tags=None, limit=8):
        words = {w for w in query.lower().replace("-", " ").split() if len(w) > 2}
        hits = [dict(v) for v in self.items.values() if v["status"] == "active" and v["kind"] in (kinds or [v["kind"]])
                and words & set((v["title"] + " " + v["slug"]).lower().replace("-", " ").split())]
        return hits[:limit]

    def link(self, a, b, rel, note=""):
        self.links.append((a, b, rel))

    def add_goal(self, goal, kind="tutorial", queries=None, asked_by="agent"):
        self.goals.append((goal, kind, queries, asked_by))
        return len(self.goals)


def tutorial_note():
    note, ok, _ = hub_learn.validate_tutorial(tutorial_reply(), (0, 300), 300 * 90)
    assert ok
    return note


META = {"title": "Caption tutorial", "channel": "Chan", "seconds": 300, "views": 12345, "published_at": "2026-05-01T00:00:00Z"}


def test_a_tutorial_files_a_source_a_recipe_per_technique_and_lessons_with_links():
    hub = FakeHub()
    assert hub_learn.ingest_tutorial(hub, "VIDEOAAAAAA", META, tutorial_note(), "Animated captions") == (1, 1)
    src, rec, tech = hub.items[("source", "yt-VIDEOAAAAAA")], hub.items[("recipe", "yt-VIDEOAAAAAA-40-caption-pop")], hub.items[("technique", "caption-pop")]
    assert src["origin"] == "youtube" and src["meta"]["url"] == "https://www.youtube.com/watch?v=VIDEOAAAAAA" and "captions" in src["tags"]
    assert rec["meta"]["url"].endswith("&t=40s") and "How: Split text" in rec["body"] and "0:40" in rec["body"]
    assert tech["status"] == "draft" and tech["meta"]["sources"] == ["VIDEOAAAAAA"]               # one video does not define the vocabulary
    assert (rec["id"], tech["id"], "implements") in hub.links and (rec["id"], src["id"], "part_of") in hub.links
    assert hub.items[("lesson", "yt-VIDEOAAAAAA-100")]["meta"]["url"].endswith("&t=100s")


def test_a_draft_technique_goes_live_when_a_second_different_video_teaches_it():
    hub = FakeHub()
    hub_learn.ingest_tutorial(hub, "VIDEOAAAAAA", META, tutorial_note(), "g")
    hub_learn.ingest_tutorial(hub, "VIDEOAAAAAA", META, tutorial_note(), "g")                       # the same video again counts once
    assert hub.items[("technique", "caption-pop")]["status"] == "draft"
    hub_learn.ingest_tutorial(hub, "VIDEOBBBBBB", {**META, "title": "Another"}, tutorial_note(), "g")
    t = hub.items[("technique", "caption-pop")]
    assert t["status"] == "active" and t["meta"]["sources"] == ["VIDEOAAAAAA", "VIDEOBBBBBB"] and t["origin"] == "youtube"


def test_a_technique_known_under_another_name_is_reused_not_duplicated():
    hub = FakeHub()
    seeded = hub.upsert("technique", "punch-in-zoom", "Punch-in on the key word", "Scale up.", ["motion"], origin="seed")
    note = tutorial_note()
    note["techniques"][0]["name"] = "Punch in zoom"
    hub_learn.ingest_tutorial(hub, "VIDEOAAAAAA", META, note, "g")
    assert ("technique", "punch-in-zoom") in hub.items and not any(k[0] == "technique" and k[1] == "punch-in-zoom-2" for k in hub.items)
    assert sum(1 for k in hub.items if k[0] == "technique") == 1                                     # no new technique was made
    rec = hub.items[("recipe", "yt-VIDEOAAAAAA-40-punch-in-zoom")]
    assert (rec["id"], seeded, "implements") in hub.links
    assert hub_learn.similar_technique(hub, "Totally unrelated thing") is None


def test_a_reference_becomes_an_example_with_its_style_fingerprint_linked_to_techniques():
    hub = FakeHub()
    note, ok, _ = hub_learn.validate_reference(reference_reply(), (0, 45), 45 * 90)
    eid = hub_learn.ingest_reference(hub, "REFEREN_CE1", {"title": "Trader short", "channel": "T", "views": 900000,
                                                           "published_at": "2026-08-01T00:00:00Z", "seconds": 45}, note, "finance shorts")
    ex = hub.items[("example", "ref-yt-REFEREN_CE1")]
    assert ex["id"] == eid and "performance" in ex["tags"] and ex["meta"]["views"] == 900000
    assert ex["meta"]["fingerprint"]["pacing"]["cuts_per_10s"] == 4.0 and ex["meta"]["fingerprint"]["palette"] == ["yellow", "navy"]
    assert "**Hook**" in ex["body"] and "Do not copy" in ex["body"] and "Says guaranteed profit" in ex["body"]
    assert (eid, hub.items[("technique", "caption-pop")]["id"], "example_of") in hub.links


# ---------------------------------------------------------------- choosing videos

S = {**learner.DEFAULTS}


def m(seconds=600, views=50_000, **kw):
    return {"title": "T", "channel": "C", "seconds": seconds, "views": views, "published_at": "2026-01-01T00:00:00Z",
            "public": True, "processed": True, "live": False, **kw}


def test_only_suitable_videos_are_chosen_in_the_order_youtube_ranked_them():
    meta = {"a": m(), "b": m(seconds=100), "c": m(views=10), "d": m(public=False), "e": m(live=True), "f": m(seconds=2000),
            "g": m(), "h": m(seconds=None), "i": m()}
    picked = learner.pick_candidates("tutorial", ["a", "b", "c", "d", "e", "f", "known", "g", "h", "i", "missing"], meta, {"known"}, S, 2)
    assert [c["id"] for c in picked] == ["a", "g"] and picked[0]["status"] == "new" and picked[0]["parts_done"] == []
    refs = learner.pick_candidates("reference", ["a", "b", "c"], {"a": m(seconds=45, views=900_000), "b": m(seconds=45, views=100),
                                                                  "c": m(seconds=400, views=900_000)}, set(), S, 3)
    assert [c["id"] for c in refs] == ["a"]


# ---------------------------------------------------------------- the goal queue, end to end

class Store:
    def __init__(self, goals):
        self.goals, self.units, self.known, self.released, self.swept = goals, {}, set(), [], 0

    def sweep_stale(self, minutes):
        self.swept += 1

    def claim(self, worker):
        for g in self.goals:
            if g["status"] == "open":
                g["status"] = "working"
                return {k: json.loads(json.dumps(g[k])) for k in ("id", "goal", "kind", "queries", "attempts", "result")}
        return None

    def release(self, goal_id, status, result, reason=None, failed_attempt=False):
        g = next(x for x in self.goals if x["id"] == goal_id)
        g.update(status=status, result=json.loads(json.dumps(result)), reason=reason)
        self.released.append((goal_id, status))

    def known_video(self, vid):
        return vid in self.known

    def quota_used(self, api):
        return self.units.get(api, 0.0)

    def quota_add(self, api, amount):
        self.units[api] = self.units.get(api, 0.0) + amount


class Client:
    def __init__(self, replies):
        self.replies, self.calls, self.key_error = list(replies), [], None

    def watch(self, model, vid, prompt, fields, window=None):
        self.calls.append((model, vid, prompt, fields, window))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(model=model, text=reply[0], video_tokens=reply[1], seconds=12.0)

    def key_ok(self):
        if self.key_error:
            raise self.key_error


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def goal(kind="tutorial", **kw):
    return {"id": 1, "goal": "Animated captions", "kind": kind, "queries": ["q one"], "attempts": 0, "result": {}, "status": "open", **kw}


def make(goals, client, hub=None, found=("VIDEOAAAAAA",), meta=None, search_calls=None):
    store, clock = Store(goals), Clock()
    results = list(found)

    def search(query, **kw):
        (search_calls if search_calls is not None else []).append((query, kw))
        return results

    def videos(ids):
        return {i: (meta or {}).get(i, m(seconds=300, views=9000, title="Caption tutorial")) for i in ids}
    return learner.Learner(lambda: None, {}, hub=hub or FakeHub(), client=client, store=store, youtube_search=search,
                           youtube_videos=videos, clock=clock), store, clock


def test_a_tutorial_goal_is_searched_watched_filed_and_finished():
    hub, calls = FakeHub(), []
    client = Client([(tutorial_reply(), 300 * 90)])
    lrn, store, _ = make([goal()], client, hub, search_calls=calls)
    said = lrn.step()
    assert said.startswith("searched 'q one'") and "1 worth watching" in said
    assert calls[0][1]["duration"] == "medium" and calls[0][1]["order"] == "relevance"
    assert store.units[learner.LEARN_UNITS] == 101 and store.units[library.YT_UNITS] == 101           # a search and one metadata call
    said = lrn.step()
    assert said.startswith("studied part 1/1") and "1 techniques, 1 lessons" in said
    model, vid, prompt, fields, window = client.calls[0]
    assert (vid, prompt, window) == ("VIDEOAAAAAA", "watch_learn", None) and fields["focus"] == "Animated captions"
    assert store.units[library.VIDEO_SECONDS] == 300
    said = lrn.step()
    assert said.startswith("1 video(s) studied: 1 recipes, 1 lessons, 0 examples") and store.goals[0]["status"] == "done"
    assert ("source", "yt-VIDEOAAAAAA") in hub.items
    assert lrn.step() == "paused 30 min: nothing to learn right now"                                    # the queue is empty


def test_a_reference_goal_uses_the_reference_search_and_prompt_and_files_an_example():
    hub, calls = FakeHub(), []
    meta = {"REFEREN_CE1": m(seconds=45, views=900_000, title="Trader short")}
    client = Client([(reference_reply(), 45 * 90)])
    lrn, store, _ = make([goal("reference")], client, hub, found=("REFEREN_CE1",), meta=meta, search_calls=calls)
    lrn.step()
    assert calls[0][1]["duration"] == "short" and calls[0][1]["order"] == "viewCount" and calls[0][1]["published_after"]
    lrn.step()
    assert client.calls[0][2] == "watch_reference" and ("example", "ref-yt-REFEREN_CE1") in hub.items
    lrn.step()
    assert store.goals[0]["status"] == "done" and store.goals[0]["result"]["added"]["examples"] == 1


def test_a_long_tutorial_is_watched_one_part_per_step():
    hub = FakeHub()
    meta = {"LONGVIDEO01": m(seconds=900, views=9000, title="Long")}
    reply = tutorial_reply(watched={"seconds": 600, "first_words": "a", "last_words": "b"})
    reply2 = tutorial_reply(watched={"seconds": 300, "first_words": "c", "last_words": "d"}, techniques=[], lessons=[])
    client = Client([(reply, 600 * 90), (reply2, 300 * 90)])
    lrn, store, _ = make([goal()], client, hub, found=("LONGVIDEO01",), meta=meta)
    lrn.step()
    assert "part 1/2" in lrn.step() and client.calls[0][4] == (0, 600)
    assert "part 2/2" in lrn.step() and client.calls[1][4] == (600, 900)
    assert lrn.step().startswith("1 video(s) studied") and store.goals[0]["status"] == "done"


def test_videos_already_known_are_not_chosen_and_a_goal_with_nothing_suitable_ends_cleanly():
    lrn, store, _ = make([goal(queries=["only one"])], Client([]), found=("VIDEOAAAAAA",))
    store.known.add("VIDEOAAAAAA")
    assert "0 worth watching" in lrn.step()
    assert lrn.step() == "no suitable videos found" and store.goals[0]["status"] == "done"


def test_busy_gemini_pauses_without_costing_the_video_an_attempt():
    client = Client([GeminiFreeError("busy", "503")] * 3)
    lrn, store, clock = make([goal()], client)
    lrn.step()
    said = lrn.step()
    assert said.startswith("paused 10 min: Gemini is busy") and not lrn.ready()
    cand = store.goals[0]["result"]["candidates"][0]
    assert cand["attempts"] == 0 and cand["status"] == "new" and store.goals[0]["status"] == "open"
    clock.t += 601
    assert lrn.ready()


def test_a_note_that_fails_the_checks_twice_gives_up_on_that_video_only():
    client = Client([(tutorial_reply(), 10)] * 2)
    lrn, store, _ = make([goal(queries=["only"])], client)
    lrn.step()
    assert "note rejected" in lrn.step() and store.goals[0]["result"]["candidates"][0]["attempts"] == 1
    assert "note rejected" in lrn.step()
    cand = store.goals[0]["result"]["candidates"][0]
    assert cand["status"] == "failed" and "watch checks" in cand["reason"]
    assert lrn.step() == "no suitable videos found" or store.goals[0]["status"] == "done"


def test_a_bad_key_pauses_and_the_search_budget_is_respected():
    client = Client([GeminiFreeError("bad_request", "400")] * 3)               # every model refuses, then the key is checked
    client.key_error = RuntimeError("API key not valid")
    lrn, store, _ = make([goal()], client)
    lrn.step()
    assert "the Gemini key does not work" in lrn.step() and store.goals[0]["result"]["candidates"][0]["attempts"] == 0
    store2 = make([goal()], Client([]))
    lrn2, st2, _ = store2
    st2.units[learner.LEARN_UNITS] = 1150
    said = lrn2.step()
    assert said.startswith("paused 60 min: today's YouTube search units are used up") and st2.goals[0]["result"].get("queries_done") in (None, [])


def test_the_free_video_budget_is_shared_with_the_library():
    lrn, store, _ = make([goal()], Client([]))
    lrn.step()
    store.units[library.VIDEO_SECONDS] = 7 * 3600 - 10
    assert "hours of free video are used up" in lrn.step()


def test_a_shutdown_puts_the_goal_back_in_the_queue():
    class Stop(BaseException):
        pass
    lrn, store, _ = make([goal()], Client([]))
    lrn.search = lambda *a, **k: (_ for _ in ()).throw(Stop())
    with pytest.raises(Stop):
        lrn.step()
    assert store.goals[0]["status"] == "open"


def test_an_unexpected_error_pauses_the_learner_and_never_raises():
    lrn, store, _ = make([goal()], Client([]))
    lrn.search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    assert lrn.step().startswith("paused 10 min: RuntimeError") and store.goals[0]["status"] == "open"


def test_the_learner_runs_only_when_asked_and_both_keys_exist(monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    assert "LEARNING_ENABLED" in learner.why_off()
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert "GEMINI_API_KEY" in learner.why_off()
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    assert learner.why_off() is None and learner.enabled()


def test_the_seed_goals_are_valid_and_use_plain_search_phrases():
    goals = json.loads((Path(learner.__file__).parent / "hub_seed" / "goals.json").read_text(encoding="utf-8"))
    assert len(goals) >= 15 and {g["kind"] for g in goals} == {"tutorial", "reference"}
    assert len({(g["kind"], g["goal"].lower()) for g in goals}) == len(goals)
    for g in goals:
        assert g["queries"] and all(0 < len(q) <= 120 for q in g["queries"])
        gemini_free.GeminiFree.prompt("watch_learn", {"scope": "s", "focus": learner.focus_text(g["goal"])})
