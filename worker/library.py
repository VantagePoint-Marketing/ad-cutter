"""The reference library: Gemini studies videos and writes the notes the playbook will be built from.

Tier 1 is Robert's list on the craft of editing (library/foundation.txt). Gemini watches each video by its public
YouTube link on Google's free tier (gemini_free.py): nothing is downloaded and nothing of ours is sent. Long videos
are watched in parts of about ten minutes. A part's note is kept only if it proves the part was really watched:
the length Gemini reports, its first and last words, and enough video tokens. Lessons whose time falls outside the
part are dropped, and a note that loses too many of them is not used.

The worker calls Runner.step() only while no editing job is waiting; one step watches at most one part (a minute
or two). Limits: 7 hours of video a day on the free tier (Google allows 8), 2,000 YouTube quota units a day.
Library work is off unless LIBRARY_ENABLED=1 and both keys (GEMINI_API_KEY, YOUTUBE_API_KEY) are set.

    python tools/jobctl.py library        (inside the container) what has been studied so far
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import gemini_free
import llm
import youtube
from gemini_free import GeminiFree, GeminiFreeError

log = logging.getLogger("ad-cutter")
HERE = Path(__file__).resolve().parent
FOUNDATION = HERE / "library" / "foundation.txt"

LEVERS = {"hook", "segment_order", "length", "cut_points", "pause_trim", "headline", "callouts", "captions", "cta",
          "primary_text", "angle", "none"}
VIDEO_SECONDS, YT_UNITS = "gemini-free-video-seconds", "youtube-units"
PACIFIC_DAY = "to_char(now() at time zone 'America/Los_Angeles', 'YYYY-MM-DD')"
REMOVED = "removed from the foundation list"
MAX_ATTEMPTS = 3              # watches whose notes fail the checks before a video is given up
MIN_TOKENS_PER_SECOND = 50    # Gemini 3.x Flash counts about 90 per second of video, 2.5 Flash about 290
MAX_LESSONS = 25
SECONDS_TOLERANCE = 0.15
DROPPED_TOLERANCE = 0.4

DEFAULTS = {"watch_models": ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-2.5-flash"], "window_seconds": 600,
            "min_last_window_seconds": 120, "daily_video_hours": 7, "max_video_minutes": 90,
            "youtube_daily_units": 2000, "resync_hours": 6}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("library") or {})}


def why_off() -> str | None:
    """None when the library may run, otherwise the reason it is off."""
    if os.environ.get("LIBRARY_ENABLED", "").strip() != "1":
        return "LIBRARY_ENABLED is not 1"
    missing = [k for k in (gemini_free.KEY_ENV, youtube.KEY_ENV) if not os.environ.get(k, "").strip()]
    return f"{' and '.join(missing)} not set" if missing else None


def enabled() -> bool:
    return why_off() is None


# ---------------------------------------------------------------- pure helpers

def read_foundation(path: Path = FOUNDATION) -> list[str]:
    """Video ids in the order listed. Blank lines and lines starting with # are skipped; text after a # is a comment."""
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        vid = youtube.video_id(line.split()[0])
        if vid and vid not in ids:
            ids.append(vid)
    return ids


def windows(seconds: int, size: int = 600, min_last: int = 120) -> list[tuple[int, int]]:
    """Parts of about `size` seconds; a short remainder is added to the last part instead of standing alone."""
    seconds = int(seconds)
    out, start = [], 0
    while seconds - start > size + min_last:
        out.append((start, start + size))
        start += size
    out.append((start, seconds))
    return out


def clock(seconds: float) -> str:
    seconds = round(seconds)
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def scope_text(window: tuple[int, int], total: int, part: int, parts: int) -> str:
    if parts == 1:
        return f"Watch the whole video (about {clock(total)})."
    start, end = window
    return (f"You are watching part {part} of {parts} of this video: from {clock(start)} to {clock(end)} of the whole "
            f"video. Report `seconds`, `first_words`, `last_words` and every `at_s` for this part only, counted "
            f"from the start of this part (0 = the moment this part begins).")


def _num(value) -> float | None:
    try:
        return float(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def validate_note(text: str, window: tuple[int, int], video_tokens: int | None) -> tuple[dict, bool, list[str]]:
    """Check one part's note; returns the cleaned note (lesson times made absolute), whether it is usable, and why
    not. Gemini reports times from the start of the part; if it clearly used whole-video times instead, those are
    accepted too."""
    start, end = window
    length = max(1, end - start)
    try:
        raw = llm.extract_json(text)
    except ValueError:
        return {"window": [start, end], "raw": text[:2000]}, False, ["the reply was not JSON"]
    if not isinstance(raw, dict):
        return {"window": [start, end], "raw": text[:2000]}, False, ["the reply was not a JSON object"]
    problems = []
    watched = raw.get("watched") if isinstance(raw.get("watched"), dict) else {}
    seen = _num(watched.get("seconds"))
    if seen is None or abs(seen - length) > max(SECONDS_TOLERANCE * length, 10):
        problems.append(f"it reported {seen if seen is not None else 'no'} seconds watched; this part is {length}")
    first = str(watched.get("first_words") or "").strip()
    last = str(watched.get("last_words") or "").strip()
    if not first or not last:
        problems.append("it gave no first or last words")
    if video_tokens is None:
        problems.append("Google reported no video token count, so the watch cannot be proven")
    elif video_tokens / length < MIN_TOKENS_PER_SECOND:
        problems.append(f"only {video_tokens / length:.0f} video tokens per second (at least {MIN_TOKENS_PER_SECOND})")
    given = [x for x in (raw.get("lessons") if isinstance(raw.get("lessons"), list) else []) if isinstance(x, dict)]
    times = [_num(x.get("at_s")) for x in given]
    relative = sum(1 for t in times if t is not None and 0 <= t <= length * 1.05)
    absolute = sum(1 for t in times if t is not None and start <= t <= end + length * 0.05) if start else 0
    offset = start if absolute > relative else 0
    lessons, dropped = [], 0
    for x, t in zip(given, times):
        principle = str(x.get("principle") or "").strip()
        if t is None or not principle or not 0 <= t - offset <= length * 1.05:
            dropped += 1
            continue
        lever = str(x.get("lever") or "none").strip().lower()
        lessons.append({"at_s": round(start + min(t - offset, length), 1), "topic": str(x.get("topic") or "").strip()[:60],
                        "principle": principle[:400], "lever": lever if lever in LEVERS else "none",
                        "how_we_apply": str(x.get("how_we_apply") or "").strip()[:400]})
    if given and dropped / len(given) > DROPPED_TOLERANCE:
        problems.append(f"{dropped} of {len(given)} lessons had no text or a time outside this part")
    note = {"window": [start, end], "watched": {"seconds": seen, "first_words": first[:200], "last_words": last[:200]},
            "summary": str(raw.get("summary") or "").strip()[:600], "lessons": lessons[:MAX_LESSONS], "dropped": dropped}
    return note, not problems, problems


# ---------------------------------------------------------------- database

class Store:
    """Every SQL statement the library runs. `connect` returns a new autocommit psycopg connection."""

    def __init__(self, connect):
        self.connect = connect

    def sweep_stale(self) -> None:
        with self.connect() as c:
            c.execute("update ref_sources set status = 'new', locked_by = null, heartbeat_at = null, updated_at = now() "
                      "where status = 'working' and heartbeat_at < now() - interval '20 minutes'")

    def youtube_rows(self, tier: int) -> dict[str, dict]:
        with self.connect() as c:
            found = c.execute("select id, external_id, status, reason, coalesce(meta_at < now() - interval '25 days', "
                              "true) from ref_sources where platform = 'youtube' and tier = %s", (tier,)).fetchall()
        return {r[1]: {"id": r[0], "status": r[2], "reason": r[3], "stale": bool(r[4])} for r in found}

    def upsert_youtube(self, tier: int, picked_by: str, vid: str, meta: dict | None, status: str,
                       reason: str | None) -> None:
        """New videos arrive with `status`. On an existing row, a 'failed'/'skipped' verdict from fresh metadata
        replaces its status; otherwise a skipped row comes back as 'new' and any other status is kept."""
        meta = meta or {}
        metrics = {k: meta.get(k) for k in ("views", "published_at") if meta.get(k) is not None}
        with self.connect() as c:
            c.execute(
                "insert into ref_sources (tier, platform, external_id, url, title, channel, seconds, metrics, picked_by, "
                "status, reason, meta_at) values (%s, 'youtube', %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
                "on conflict (platform, external_id) do update set "
                "title = coalesce(nullif(excluded.title, ''), ref_sources.title), "
                "channel = coalesce(nullif(excluded.channel, ''), ref_sources.channel), "
                "seconds = coalesce(excluded.seconds, ref_sources.seconds), "
                "metrics = case when excluded.metrics = '{}'::jsonb then ref_sources.metrics else excluded.metrics end, "
                "meta_at = now(), updated_at = now(), "
                "status = case when excluded.status in ('failed', 'skipped') then excluded.status "
                "              when ref_sources.status = 'skipped' then 'new' else ref_sources.status end, "
                "reason = case when excluded.status in ('failed', 'skipped') then excluded.reason "
                "              when ref_sources.status = 'skipped' then null else ref_sources.reason end",
                (tier, vid, gemini_free.watch_url(vid), meta.get("title", ""), meta.get("channel", ""),
                 meta.get("seconds"), json.dumps(metrics), picked_by, status, reason))

    def set_status(self, source_id: int, status: str, reason: str | None) -> None:
        with self.connect() as c:
            c.execute("update ref_sources set status = %s, reason = %s, updated_at = now() where id = %s",
                      (status, reason, source_id))

    def claim(self, worker: str) -> dict | None:
        """The next video: tier first, then the fewest failed watches, so a stubborn video never blocks the rest."""
        with self.connect() as c:
            row = c.execute(
                "update ref_sources set status = 'working', locked_by = %s, heartbeat_at = now(), updated_at = now() "
                "where id = (select id from ref_sources where status = 'new' and platform = 'youtube' "
                "            and seconds is not null order by tier, attempts, id for update skip locked limit 1) "
                "returning id, tier, external_id, title, seconds, attempts", (worker,)).fetchone()
        keys = ("id", "tier", "external_id", "title", "seconds", "attempts")
        return dict(zip(keys, row)) if row else None

    def ok_windows(self, source_id: int) -> set[int]:
        with self.connect() as c:
            found = c.execute("select distinct window_start from ref_notes where source_id = %s and reference_ok",
                              (source_id,)).fetchall()
        return {int(r[0]) for r in found}

    def add_note(self, source_id: int, model: str, window: tuple[int, int], note: dict, ok: bool, problems: str,
                 video_tokens: int | None) -> None:
        with self.connect() as c:
            c.execute("insert into ref_notes (source_id, model, window_start, window_end, note, reference_ok, problems, "
                      "video_tokens) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                      (source_id, model, window[0], window[1], json.dumps(note), ok, problems[:1000], video_tokens))

    def release(self, source_id: int, status: str, reason: str | None = None, failed_attempt: bool = False,
                reset_attempts: bool = False) -> None:
        with self.connect() as c:
            c.execute("update ref_sources set status = %s, reason = %s, locked_by = null, heartbeat_at = null, "
                      "updated_at = now(), attempts = case when %s then 0 else attempts + %s end where id = %s",
                      (status, reason, reset_attempts, 1 if failed_attempt else 0, source_id))

    def quota_used(self, api: str) -> float:
        with self.connect() as c:
            row = c.execute(f"select used from api_quota where api = %s and period = {PACIFIC_DAY}", (api,)).fetchone()
        return float(row[0]) if row else 0.0

    def quota_add(self, api: str, amount: float) -> None:
        with self.connect() as c:
            c.execute(f"insert into api_quota (api, period, used) values (%s, {PACIFIC_DAY}, %s) "
                      "on conflict (api, period) do update set used = api_quota.used + excluded.used", (api, amount))


# ---------------------------------------------------------------- the runner

class Runner:
    def __init__(self, connect, cfg: dict, worker_id: str = "library", client: GeminiFree | None = None,
                 store: Store | None = None, youtube_videos=youtube.videos, foundation: Path = FOUNDATION,
                 clock_fn=time.time):
        self.s = settings(cfg)
        self.store = store or Store(connect)
        self.client = client or GeminiFree()
        self.youtube_videos, self.foundation, self.now = youtube_videos, foundation, clock_fn
        self.worker_id = worker_id
        self.paused_until, self.pause_reason, self.synced_at, self.rate_strikes = 0.0, "", 0.0, 0
        self.recent_failures: list[int] = []      # videos that failed in a row, for the circuit breaker

    def ready(self) -> bool:
        return self.now() >= self.paused_until

    def pause(self, seconds: float, reason: str) -> str:
        self.paused_until, self.pause_reason = self.now() + seconds, reason
        return f"paused {seconds / 60:.0f} min: {reason}"

    # ------------------------------------------------ tier 1: Robert's list

    def sync_foundation(self) -> str:
        ids = read_foundation(self.foundation)
        rows = self.store.youtube_rows(tier=1)
        for vid, row in rows.items():
            if vid not in ids and row["status"] != "skipped":
                self.store.set_status(row["id"], "skipped", REMOVED)
        # new, stale (YouTube's 30-day rule) or skipped (it may be back: re-added, public again, a transient miss)
        need = [v for v in ids if v not in rows or rows[v]["stale"] or rows[v]["status"] == "skipped"]
        fetched = 0
        for i in range(0, len(need), 50):
            if self.store.quota_used(YT_UNITS) + 1 > self.s["youtube_daily_units"]:
                return f"YouTube quota for today used up; {len(need) - i} videos wait for metadata"
            batch = need[i:i + 50]
            meta = self.youtube_videos(batch)
            self.store.quota_add(YT_UNITS, 1)
            for vid in batch:
                m = meta.get(vid)
                if not m:                          # skipped, not failed: the next sync looks again
                    status, reason = "skipped", "not on YouTube (private, removed or a wrong link)"
                elif not m["public"] or not m["processed"] or m["live"]:
                    status, reason = "skipped", "not a public, finished video"
                elif not m["seconds"]:
                    status, reason = "skipped", "YouTube gives no length for it"
                elif m["seconds"] > self.s["max_video_minutes"] * 60:
                    status, reason = "skipped", f"longer than {self.s['max_video_minutes']} minutes"
                else:
                    status, reason = "new", None
                self.store.upsert_youtube(1, "foundation", vid, m, status, reason)
                fetched += 1
        return f"foundation list: {len(ids)} videos, metadata fetched for {fetched}"

    # ------------------------------------------------ one step

    def step(self) -> str:
        """Watch at most one part of one video. Never raises an Exception (it pauses instead); a shutdown signal
        (a BaseException) puts the video back and propagates."""
        if self.now() - self.synced_at > self.s["resync_hours"] * 3600:
            try:
                log.info("library: %s", self.sync_foundation())
                self.synced_at = self.now()
            except Exception:                          # noqa: BLE001 - study what is already listed; sync in an hour
                log.exception("library: could not sync the foundation list")
                self.synced_at = self.now() - self.s["resync_hours"] * 3600 + 3600
        try:
            self.store.sweep_stale()
            src = self.store.claim(self.worker_id)
        except Exception as err:                       # noqa: BLE001 - a database blip: try later
            log.exception("library: could not pick the next video")
            return self.pause(600, f"{type(err).__name__}: {err}")
        if not src:
            return self.pause(1800, "nothing left to study")
        try:
            return self._watch_next_part(src)
        except BaseException as err:
            try:
                self.store.release(src["id"], "new")
            except Exception:                          # noqa: BLE001 - never hide the original error
                log.exception("library: could not put video %s back", src["id"])
            if not isinstance(err, Exception):
                raise
            log.exception("library: unexpected error on %s", src["external_id"])
            return self.pause(600, f"{type(err).__name__}: {err}")

    def _watch_next_part(self, src: dict) -> str:
        parts = windows(src["seconds"], self.s["window_seconds"], self.s["min_last_window_seconds"])
        done = self.store.ok_windows(src["id"])
        todo = [w for w in parts if w[0] not in done]
        title = src["title"][:60] or src["external_id"]
        if not todo:
            self.store.release(src["id"], "done", reset_attempts=True)
            return f"'{title}' already studied"
        window = todo[0]
        length = window[1] - window[0]
        if self.store.quota_used(VIDEO_SECONDS) + length > self.s["daily_video_hours"] * 3600:
            self.store.release(src["id"], "new")
            return self.pause(3600, f"today's {self.s['daily_video_hours']} hours of free video are used up")
        part_no = parts.index(window) + 1
        fields = {"scope": scope_text(window, src["seconds"], part_no, len(parts))}
        models = self.s["watch_models"]
        start = src["attempts"] % len(models)          # a retry starts with a different model
        result, errors = None, []
        for model in models[start:] + models[:start]:
            try:
                result = self.client.watch(model, src["external_id"], "watch_craft", fields,
                                           window=window if len(parts) > 1 else None)
                break
            except GeminiFreeError as err:
                errors.append(err)
                if err.kind == "network":              # a timeout or broken connection: don't hold the worker longer
                    break
        if result is None:
            return self._failed_call(src, title, length, errors)
        self.rate_strikes, self.recent_failures = 0, []
        self.store.quota_add(VIDEO_SECONDS, length)    # Google counted it, whatever the note's quality
        note, ok, problems = validate_note(result.text, window, result.video_tokens)
        self.store.add_note(src["id"], result.model, window, note, ok, "; ".join(problems), result.video_tokens)
        what = f"part {part_no}/{len(parts)} of '{title}' with {result.model} in {result.seconds:.0f}s"
        if ok:
            self.store.release(src["id"], "done" if len(todo) == 1 else "new", reset_attempts=True)
            return f"studied {what}: {len(note['lessons'])} lessons"
        if src["attempts"] + 1 >= MAX_ATTEMPTS:
            self.store.release(src["id"], "failed", f"its notes failed the watch checks {MAX_ATTEMPTS} times: "
                               f"{'; '.join(problems)}"[:500], failed_attempt=True)
        else:
            self.store.release(src["id"], "new", failed_attempt=True)
        return f"note rejected for {what}: {'; '.join(problems)}"

    def _failed_call(self, src: dict, title: str, length: int, errors: list[GeminiFreeError]) -> str:
        """No model returned a reply. Transient and configuration problems pause the library and never count
        against the video; only a problem that is plausibly this video's costs it an attempt."""
        last, kinds = errors[-1], {e.kind for e in errors}

        def wait(seconds: float, why: str) -> str:
            self.store.release(src["id"], "new")
            return self.pause(seconds, why)

        transient = {"busy", "network"}
        if kinds <= transient:
            return wait(600, f"Gemini is busy or unreachable ({last})")
        if kinds <= transient | {"rate_limited"}:
            self.rate_strikes += 1
            return wait(6 * 3600 if self.rate_strikes >= 3 else 1800, f"free-tier limit reached ({last})")
        if kinds <= transient | {"rate_limited", "bad_model"}:
            return wait(3600, f"no usable Gemini model; check library.watch_models in config.json ({last})")
        if "bad_reply" in kinds:
            self.store.quota_add(VIDEO_SECONDS, length)  # Google may have watched it before the reply went wrong
        # What is left (refused requests, 403/404, empty replies) may be the key rather than the video. An invalid,
        # revoked or expired key comes back as a 400, so check the key before blaming the video.
        try:
            self.client.key_ok()
        except Exception as err:                       # noqa: BLE001 - the key, not the video, is the problem
            return wait(3600, f"the Gemini key does not work: {err}")
        # Different videos failing one after another points at something systemic (a region block, an API change):
        # the third pauses for an hour instead of costing another video an attempt. The count then starts again, so
        # a genuinely unwatchable video is still dealt with after the pause and the library never stalls on it.
        # A systemic fault therefore costs at most two attempts an hour (undo with `jobctl.py library-retry failed`).
        if src["id"] not in self.recent_failures:
            self.recent_failures.append(src["id"])
        if len(self.recent_failures) >= 3:
            self.recent_failures = []
            return wait(3600, f"3 different videos failed in a row; pausing rather than failing more ({last})")
        if kinds == {"unavailable"}:                   # every model refused this video, and the key works
            self.store.release(src["id"], "failed", f"Gemini cannot watch it: {last}"[:500])
            return f"'{title}' cannot be watched: {last}"
        if src["attempts"] + 1 >= MAX_ATTEMPTS:
            self.store.release(src["id"], "failed", f"no model could watch it: {last}"[:500], failed_attempt=True)
        else:
            self.store.release(src["id"], "new", failed_attempt=True)
        return f"'{title}': no model could watch it this time ({'; '.join(str(e) for e in errors)[:300]})"
