"""Keeps the knowledge hub filled and linked, a little at a time while no ad is being made.

What goes in:
  1. the technique vocabulary (hub_seed/techniques.json): the spine every lesson and recipe links to;
  2. the editing skills (hub_seed/skills.jsonl.gz, built from the HyperFrames and ffmpeg skills by tools/build_hub_seed.py);
  3. links from each technique to the recipes that implement it (found by search, marked as auto-matched);
  4. the lessons Gemini wrote while studying videos (the library tables ref_sources / ref_notes), as `source` and `lesson`
     notes linked to the technique each lesson is about.

Everything is idempotent (notes are keyed by kind + slug), so a restart or a second worker repeats nothing harmful.
`Maintainer.step()` does one small unit of work and never raises an Exception.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
from pathlib import Path

import hub as hubmod
from hub import Hub

log = logging.getLogger("ad-cutter")
SEED_DIR = Path(__file__).resolve().parent / "hub_seed"
BATCH = 80                    # seed notes written per step
LIBRARY_EVERY = 1800          # seconds between looks for newly studied lessons
# which technique each editing lever of the old library prompt is about
LEVER_TECHNIQUE = {"hook": "cold-open-hook", "segment_order": "cold-open-hook", "length": "jump-cut-pacing",
                   "cut_points": "breath-cut-points", "pause_trim": "jump-cut-pacing", "headline": "kinetic-typography-title",
                   "callouts": "marker-callout-arrow", "captions": "word-by-word-captions", "cta": "strong-end-cta",
                   "angle": "problem-agitate-solution"}
LEVER_TAGS = {"hook": ["hook"], "segment_order": ["story", "hook"], "length": ["pacing"], "cut_points": ["cut"],
              "pause_trim": ["pacing", "cut"], "headline": ["typography", "hook"], "callouts": ["overlay"],
              "captions": ["captions"], "cta": ["cta"], "primary_text": ["ads"], "angle": ["story", "ads"]}


# ---------------------------------------------------------------- reading the seed files

def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else ""


def read_techniques(path: Path | None = None) -> list[dict]:
    path = path or SEED_DIR / "techniques.json"
    return json.loads(path.read_text(encoding="utf-8"))


def read_seed(path: Path | None = None) -> list[dict]:
    path = path or SEED_DIR / "skills.jsonl.gz"
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- the studied lessons

def lesson_notes(rows: list[tuple]) -> tuple[list[dict], list[dict]]:
    """(source notes, lesson notes) from rows of (video id, title, channel, seconds, tier, picked_by, note json): pure, so
    it is tested without a database. Each lesson carries a link to the exact moment (&t=) and the technique it is about."""
    sources: dict[str, dict] = {}
    lessons: list[dict] = []
    for vid, title, channel, seconds, tier, picked_by, note in rows:
        note = note if isinstance(note, dict) else json.loads(note or "{}")
        src = sources.setdefault(vid, {"kind": "source", "slug": f"yt-{vid}", "title": (title or vid)[:300], "tags": ["social"],
                                       "summary": [], "meta": {"platform": "youtube", "external_id": vid, "channel": channel or "",
                                                               "seconds": seconds, "tier": tier, "picked_by": picked_by or "",
                                                               "url": f"https://www.youtube.com/watch?v={vid}"}})
        if note.get("summary"):
            src["summary"].append(str(note["summary"]))
        for x in note.get("lessons") or []:
            principle = str(x.get("principle") or "").strip()
            if not principle:
                continue
            at = int(float(x.get("at_s") or 0))
            lever = str(x.get("lever") or "none")
            topic = str(x.get("topic") or "").strip()
            how = str(x.get("how_we_apply") or "").strip()
            lessons.append({"kind": "lesson", "slug": f"yt-{vid}-{at}", "title": (topic or principle[:60])[:200],
                            "body": principle + (f"\n\nHow we would apply it: {how}" if how else ""),
                            "tags": LEVER_TAGS.get(lever, []) + ([] if lever in LEVER_TAGS else ["tools"]),
                            "meta": {"at_s": at, "lever": lever, "video": vid, "channel": channel or "",
                                     "url": f"https://www.youtube.com/watch?v={vid}&t={at}s", "source": f"yt-{vid}"},
                            "technique": LEVER_TECHNIQUE.get(lever)})
    return list(sources.values()), lessons


def import_library(hub: Hub, connect) -> tuple[int, int]:
    """Copy the studied lessons into the hub (new ones, and updates to old ones). Returns (sources, lessons) written."""
    with connect() as c:
        found = c.execute(
            "select s.external_id, s.title, s.channel, s.seconds, s.tier, s.picked_by, n.window_start, n.note, n.created_at "
            "from ref_sources s join ref_notes n on n.source_id = s.id where s.platform = 'youtube' and n.reference_ok "
            "order by s.id, n.window_start, n.created_at desc").fetchall()
    latest: dict[tuple, tuple] = {}
    for r in found:                                   # the newest accepted note of each part
        latest.setdefault((r[0], r[6]), r[:6] + (r[7],))
    sources, lessons = lesson_notes(list(latest.values()))
    ids: dict[str, int] = {}
    for s in sources:
        ids[s["slug"]] = hub.upsert("source", s["slug"], s["title"], "\n\n".join(s["summary"]), s["tags"], s["meta"], origin="library")
    for l in lessons:
        lid = hub.upsert("lesson", l["slug"], l["title"], l["body"], l["tags"], l["meta"], origin="library")
        if l["meta"]["source"] in ids:
            hub.link(lid, ids[l["meta"]["source"]], "part_of")
        if l["technique"]:
            tech = hub.get("technique", l["technique"])
            if tech:
                hub.link(tech["id"], lid, "taught_by", "from the lesson's editing lever")
    return len(sources), len(lessons)


# ---------------------------------------------------------------- the maintainer

class Maintainer:
    def __init__(self, connect, hub: Hub | None = None, seed_dir: Path = SEED_DIR, clock=time.time, batch: int = BATCH):
        self.connect, self.hub = connect, hub or Hub(connect)
        self.seed_dir, self.now, self.batch = seed_dir, clock, batch
        self.next_at, self.checked, self.library_at = 0.0, False, 0.0

    def ready(self) -> bool:
        return self.now() >= self.next_at

    def step(self) -> str:
        try:
            return self._step()
        except Exception as err:                       # noqa: BLE001 - the hub must never stop the worker
            log.exception("hub: step failed")
            self.next_at = self.now() + 600
            return f"paused 10 min: {type(err).__name__}: {err}"

    def _step(self) -> str:
        if not self.checked:
            self.checked = True
            return f"self-test: {self.hub.selftest()}"
        state = self.hub.get_state("maintainer")
        tech_path, seed_path = self.seed_dir / "techniques.json", self.seed_dir / "skills.jsonl.gz"
        tech_hash, seed_hash = file_hash(tech_path), file_hash(seed_path)
        if state.get("techniques") != tech_hash:
            items = read_techniques(tech_path)
            for t in items:
                self.hub.upsert("technique", t["slug"], t["title"], t["body"], t.get("tags", []), {"find": t.get("find", "")}, origin="seed")
            self.hub.set_state("maintainer", {**state, "techniques": tech_hash, "links": ""})
            return f"{len(items)} techniques loaded"
        if seed_hash and state.get("seed") != seed_hash:
            notes = read_seed(seed_path)
            done = int(state.get("seed_done", 0)) if state.get("seed_target") == seed_hash else 0
            chunk = notes[done:done + self.batch]
            for n in chunk:
                self.hub.upsert(n["kind"], n["slug"], n["title"], n["body"], n.get("tags", []), n.get("meta", {}), origin="seed")
            done += len(chunk)
            if done >= len(notes):
                self.hub.set_state("maintainer", {**state, "seed": seed_hash, "seed_target": seed_hash, "seed_done": done, "links": ""})
                return f"skills loaded: {len(notes)} notes"
            self.hub.set_state("maintainer", {**state, "seed_target": seed_hash, "seed_done": done})
            return f"skills loading: {done} of {len(notes)}"
        want = f"{tech_hash}:{seed_hash}"
        if state.get("links") != want:
            made = self.link_recipes()
            self.hub.set_state("maintainer", {**state, "links": want})
            return f"linked recipes to techniques: {made} links"
        if self.now() - self.library_at > LIBRARY_EVERY:
            self.library_at = self.now()
            sources, lessons = import_library(self.hub, self.connect)
            return f"studied lessons in the hub: {sources} videos, {lessons} lessons"
        self.next_at = self.now() + 300
        return "hub up to date"

    def link_recipes(self, per_technique: int = 3) -> int:
        """Link each technique to the skill notes that best match its search words (auto-matched; a person can correct them)."""
        made = 0
        for t in read_techniques(self.seed_dir / "techniques.json"):
            tech = self.hub.get("technique", t["slug"])
            if not tech or not t.get("find"):
                continue
            for hit in self.hub.search(t["find"], kinds=["recipe", "skill"], limit=per_technique):
                self.hub.link(hit["id"], tech["id"], "implements", "auto-matched by search")
                made += 1
        return made
