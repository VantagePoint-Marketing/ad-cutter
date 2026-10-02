"""The self-learning step: the agent works through what it wants to learn, one small step at a time.

A goal (table kb_goals) is a plain sentence plus up to a few search phrases and a kind:
  tutorial  - find explainers on editing, motion, captions, ffmpeg, HyperFrames or ad craft, watch them, file the techniques;
  reference - find short videos that are performing well in the team's niche, watch them, file how they are made and why
              they work, so finished ads can be compared with what is working now.

One `Learner.step()` does ONE thing: run a search (100 YouTube units), or watch one part of one video (a minute or two, on
Google's free tier, public videos only: nothing is downloaded). It runs only while no ad job is waiting, and only when
LEARNING_ENABLED=1 and both keys are set. Nothing about a client's footage can reach YouTube or the free tier: search
phrases and the `focus` sentence come from goals (our own text), and gemini_free.py only ever sends a video link plus an
allow-listed prompt.

Limits (config.json "learn"): YouTube search units a day, 15 minutes for a tutorial, 3 minutes for a reference, a few videos
per goal; the free-tier watch-seconds budget is shared with the library (library.py).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import gemini_free
import hub_learn
import library
import youtube
from gemini_free import GeminiFree, GeminiFreeError
from hub import Hub

log = logging.getLogger("ad-cutter")
LEARN_UNITS = "youtube-units-learn"
MAX_VIDEO_ATTEMPTS = 2
DEFAULTS = {"daily_units": 1200, "max_tutorial_minutes": 15, "max_reference_seconds": 180, "tutorials_per_goal": 2,
            "references_per_goal": 3, "searches_per_goal": 2, "min_tutorial_views": 2000, "min_reference_views": 50_000,
            "reference_days": 365, "stale_minutes": 20}
STORE_FIELDS = "id, goal, kind, queries, attempts, result"


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("learn") or {})}


def why_off() -> str | None:
    if os.environ.get("LEARNING_ENABLED", "").strip() != "1":
        return "LEARNING_ENABLED is not 1"
    missing = [k for k in (gemini_free.KEY_ENV, youtube.KEY_ENV) if not os.environ.get(k, "").strip()]
    return f"{' and '.join(missing)} not set" if missing else None


def enabled() -> bool:
    return why_off() is None


def focus_text(goal: str) -> str:
    """The sentence the watching prompt carries: the goal, cut to the characters gemini_free accepts."""
    clean = "".join(c if c.isalnum() or c in " ,.'():;/&+-" else " " for c in goal)
    return " ".join(clean.split())[:200] or "video editing craft"


def pick_candidates(kind: str, ids: list[str], meta: dict[str, dict], known: set[str], s: dict, want: int) -> list[dict]:
    """The videos worth watching, in the order YouTube ranked them: public, finished, not live, the right length and
    popularity for the kind of goal, and not already in the hub."""
    out = []
    for vid in ids:
        m = meta.get(vid)
        if not m or vid in known or not m["public"] or not m["processed"] or m["live"] or not m["seconds"]:
            continue
        if kind == "tutorial":
            ok = 240 <= m["seconds"] <= s["max_tutorial_minutes"] * 60 and m["views"] >= s["min_tutorial_views"]
        else:
            ok = 5 <= m["seconds"] <= s["max_reference_seconds"] and m["views"] >= s["min_reference_views"]
        if ok:
            out.append({"id": vid, "title": m["title"], "channel": m["channel"], "seconds": m["seconds"], "views": m["views"],
                        "published_at": m["published_at"], "parts_done": [], "attempts": 0, "status": "new", "reason": None})
        if len(out) >= want:
            break
    return out


class Store:
    """Every SQL statement the learner runs besides the hub's own."""

    def __init__(self, connect):
        self.connect = connect

    def sweep_stale(self, minutes: int) -> None:
        with self.connect() as c:
            c.execute("update kb_goals set status = 'open', locked_by = null, heartbeat_at = null, updated_at = now() "
                      "where status = 'working' and heartbeat_at < now() - make_interval(mins => %s)", (minutes,))

    def claim(self, worker: str) -> dict | None:
        with self.connect() as c:
            row = c.execute(
                f"update kb_goals set status = 'working', locked_by = %s, heartbeat_at = now(), updated_at = now() "
                f"where id = (select id from kb_goals where status = 'open' order by attempts, id for update skip locked limit 1) "
                f"returning {STORE_FIELDS}", (worker,)).fetchone()
        if not row:
            return None
        goal = dict(zip(("id", "goal", "kind", "queries", "attempts", "result"), row))
        goal["queries"] = list(goal["queries"] or [])
        goal["result"] = goal["result"] if isinstance(goal["result"], dict) else json.loads(goal["result"] or "{}")
        return goal

    def release(self, goal_id: int, status: str, result: dict, reason: str | None = None, failed_attempt: bool = False) -> None:
        with self.connect() as c:
            c.execute("update kb_goals set status = %s, result = %s, reason = %s, attempts = attempts + %s, locked_by = null, "
                      "heartbeat_at = null, updated_at = now() where id = %s",
                      (status, json.dumps(result), reason, 1 if failed_attempt else 0, goal_id))

    def known_video(self, vid: str) -> bool:
        with self.connect() as c:
            row = c.execute("select 1 from kb_items where (kind = 'source' and slug = %s) or (kind = 'example' and slug = %s) "
                            "union all select 1 from ref_sources where platform = 'youtube' and external_id = %s limit 1",
                            (f"yt-{vid}", f"ref-yt-{vid}", vid)).fetchone()
        return bool(row)

    def quota_used(self, api: str) -> float:
        with self.connect() as c:
            row = c.execute(f"select used from api_quota where api = %s and period = {library.PACIFIC_DAY}", (api,)).fetchone()
        return float(row[0]) if row else 0.0

    def quota_add(self, api: str, amount: float) -> None:
        with self.connect() as c:
            c.execute(f"insert into api_quota (api, period, used) values (%s, {library.PACIFIC_DAY}, %s) "
                      "on conflict (api, period) do update set used = api_quota.used + excluded.used", (api, amount))


class Learner:
    def __init__(self, connect, cfg: dict, hub: Hub | None = None, worker_id: str = "learner", client: GeminiFree | None = None,
                 store: Store | None = None, youtube_search=youtube.search, youtube_videos=youtube.videos, clock=time.time):
        self.s, self.lib = settings(cfg), library.settings(cfg)
        self.hub, self.store = hub or Hub(connect), store or Store(connect)
        self.client = client or GeminiFree()
        self.search, self.videos, self.now, self.worker_id = youtube_search, youtube_videos, clock, worker_id
        self.paused_until, self.rate_strikes = 0.0, 0

    def ready(self) -> bool:
        return self.now() >= self.paused_until

    def pause(self, seconds: float, reason: str) -> str:
        self.paused_until = self.now() + seconds
        return f"paused {seconds / 60:.0f} min: {reason}"

    # ------------------------------------------------ one step

    def step(self) -> str:
        """Do one thing. Never raises an Exception (it pauses instead); a shutdown signal puts the goal back and propagates."""
        try:
            self.store.sweep_stale(self.s["stale_minutes"])
            goal = self.store.claim(self.worker_id)
        except Exception as err:                       # noqa: BLE001 - a database blip: try later
            log.exception("learner: could not pick the next goal")
            return self.pause(600, f"{type(err).__name__}: {err}")
        if not goal:
            return self.pause(1800, "nothing to learn right now")
        result = goal["result"]
        try:
            said, status, reason = self._work(goal, result)
            self.store.release(goal["id"], status, result, reason)
            return said
        except BaseException as err:
            try:
                self.store.release(goal["id"], "open", result)
            except Exception:                          # noqa: BLE001 - never hide the original error
                log.exception("learner: could not put goal %s back", goal["id"])
            if not isinstance(err, Exception):
                raise
            log.exception("learner: unexpected error on goal %s", goal["id"])
            return self.pause(600, f"{type(err).__name__}: {err}")

    def _work(self, goal: dict, result: dict) -> tuple[str, str, str | None]:
        """(what was done, the goal's new status, a reason). Mutates `result` (it is saved by the caller)."""
        result.setdefault("queries_done", [])
        result.setdefault("candidates", [])
        result.setdefault("added", {"recipes": 0, "lessons": 0, "examples": 0})
        want = self.s["tutorials_per_goal"] if goal["kind"] == "tutorial" else self.s["references_per_goal"]
        waiting = [c for c in result["candidates"] if c["status"] == "new"]
        searches_left = self.s["searches_per_goal"] - len(result["queries_done"])
        queries = [q for q in goal["queries"] if q not in result["queries_done"]]
        found = len(result["candidates"])
        if not waiting and found < want and queries and searches_left > 0:
            return self._search(goal, result, queries[0], want)
        if waiting:
            return self._watch(goal, result, waiting[0])
        a = result["added"]
        summary = f"{found} video(s) studied: {a['recipes']} recipes, {a['lessons']} lessons, {a['examples']} examples"
        if not found:
            return "no suitable videos found", "done", "the searches found nothing suitable"
        return summary, "done", None

    # ------------------------------------------------ searching

    def _search(self, goal: dict, result: dict, query: str, want: int) -> tuple[str, str, str | None]:
        if (self.store.quota_used(LEARN_UNITS) + youtube.SEARCH_UNITS + 1 > self.s["daily_units"]
                or self.store.quota_used(library.YT_UNITS) + youtube.SEARCH_UNITS + 1 > self.lib["youtube_daily_units"]):
            return self.pause(3600, "today's YouTube search units are used up"), "open", None
        tutorial = goal["kind"] == "tutorial"
        after = None if tutorial else (datetime.now(timezone.utc) - timedelta(days=self.s["reference_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        ids = self.search(query, duration="medium" if tutorial else "short", order="relevance" if tutorial else "viewCount",
                          published_after=after, max_results=15)
        self.store.quota_add(LEARN_UNITS, youtube.SEARCH_UNITS)
        self.store.quota_add(library.YT_UNITS, youtube.SEARCH_UNITS)
        result["queries_done"].append(query)
        meta = self.videos(ids[:50]) if ids else {}
        if ids:
            self.store.quota_add(LEARN_UNITS, 1)
            self.store.quota_add(library.YT_UNITS, 1)
        known = {v for v in ids if self.store.known_video(v)} | {c["id"] for c in result["candidates"]}
        chosen = pick_candidates(goal["kind"], ids, meta, known, self.s, want - len(result["candidates"]))
        result["candidates"] += chosen
        return f"searched '{query[:50]}': {len(ids)} results, {len(chosen)} worth watching", "open", None

    # ------------------------------------------------ watching

    def _watch(self, goal: dict, result: dict, cand: dict) -> tuple[str, str, str | None]:
        tutorial = goal["kind"] == "tutorial"
        parts = library.windows(cand["seconds"], self.lib["window_seconds"], self.lib["min_last_window_seconds"]) if tutorial \
            else [(0, cand["seconds"])]
        todo = [w for w in parts if w[0] not in cand["parts_done"]]
        if not todo:
            cand["status"] = "done"
            return f"'{cand['title'][:50]}' already studied", "open", None
        window = todo[0]
        length = window[1] - window[0]
        if self.store.quota_used(library.VIDEO_SECONDS) + length > self.lib["daily_video_hours"] * 3600:
            return self.pause(3600, f"today's {self.lib['daily_video_hours']} hours of free video are used up"), "open", None
        part_no = parts.index(window) + 1
        scope = library.scope_text(window, cand["seconds"], part_no, len(parts)) if len(parts) > 1 else \
            f"Watch the whole video (about {library.clock(cand['seconds'])})."
        fields = {"scope": scope, "focus": focus_text(goal["goal"])}
        name = "watch_learn" if tutorial else "watch_reference"
        models = self.lib["watch_models"]
        start = cand["attempts"] % len(models)
        got, errors = None, []
        for model in models[start:] + models[:start]:
            try:
                got = self.client.watch(model, cand["id"], name, fields, window=window if len(parts) > 1 else None)
                break
            except GeminiFreeError as err:
                errors.append(err)
                if err.kind == "network":
                    break
        if got is None:
            return self._failed_call(cand, errors)
        self.rate_strikes = 0
        self.store.quota_add(library.VIDEO_SECONDS, length)
        check = hub_learn.validate_tutorial if tutorial else hub_learn.validate_reference
        note, ok, problems = check(got.text, window, got.video_tokens)
        what = f"part {part_no}/{len(parts)} of '{cand['title'][:50]}' with {got.model} in {got.seconds:.0f}s"
        if not ok:
            cand["attempts"] += 1
            if cand["attempts"] >= MAX_VIDEO_ATTEMPTS:
                cand["status"], cand["reason"] = "failed", f"its notes failed the watch checks: {'; '.join(problems)}"[:300]
            return f"note rejected for {what}: {'; '.join(problems)}", "open", None
        meta = {"title": cand["title"], "channel": cand["channel"], "seconds": cand["seconds"], "views": cand["views"],
                "published_at": cand["published_at"]}
        added = result["added"]
        if tutorial:
            r, l = hub_learn.ingest_tutorial(self.hub, cand["id"], meta, note, goal["goal"])
            added["recipes"] += r
            added["lessons"] += l
            said = f"studied {what}: {r} techniques, {l} lessons"
        else:
            hub_learn.ingest_reference(self.hub, cand["id"], meta, note, goal["goal"])
            added["examples"] += 1
            said = f"studied {what}: a reference example with {len(note['borrow'])} borrowable ideas"
        cand["parts_done"].append(window[0])
        cand["attempts"] = 0
        if len(cand["parts_done"]) >= len(parts):
            cand["status"] = "done"
        return said, "open", None

    def _failed_call(self, cand: dict, errors: list[GeminiFreeError]) -> tuple[str, str, str | None]:
        """No model answered. Busy, network and limit problems pause the learner and never count against the video; a key that
        does not work pauses it; anything left counts as an attempt against this video."""
        last, kinds = errors[-1], {e.kind for e in errors}
        transient = {"busy", "network"}
        if kinds <= transient:
            return self.pause(600, f"Gemini is busy or unreachable ({last})"), "open", None
        if kinds <= transient | {"rate_limited"}:
            self.rate_strikes += 1
            return self.pause(6 * 3600 if self.rate_strikes >= 3 else 1800, f"free-tier limit reached ({last})"), "open", None
        if kinds <= transient | {"rate_limited", "bad_model"}:
            return self.pause(3600, f"no usable Gemini model; check library.watch_models in config.json ({last})"), "open", None
        try:
            self.client.key_ok()
        except Exception as err:                       # noqa: BLE001 - the key, not the video, is the problem
            return self.pause(3600, f"the Gemini key does not work: {err}"), "open", None
        cand["attempts"] += 1
        if cand["attempts"] >= MAX_VIDEO_ATTEMPTS or kinds == {"unavailable"}:
            cand["status"], cand["reason"] = "failed", f"Gemini could not watch it: {last}"[:300]
        return f"'{cand['title'][:50]}': no model could watch it ({'; '.join(str(e) for e in errors)[:200]})", "open", None
