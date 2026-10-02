"""What the agent learns from a video, checked and filed into the knowledge hub.

Two kinds of watching (prompts/watch_learn.md and prompts/watch_reference.md):
  tutorial  -> techniques and lessons: each technique becomes a `recipe` note (how to do it, with a link to the moment in
               the video) that implements a technique note; a technique the hub does not know yet starts as a DRAFT and
               becomes active when a second, different video teaches it (so one video cannot define the vocabulary);
  reference -> an `example` note describing how a well-performing short video is made and why it works, linked to the
               techniques it uses.

The same "prove you really watched" checks as the library apply (reported length, first and last words, enough video
tokens). Nothing is stored but our own summary of what was seen, with a link to the video and a time.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import hub as hubmod
import library
import llm
from hub import Hub

MAX_TECHNIQUES, MAX_LESSONS, MAX_BORROW = 12, 20, 6
ACTIVE_AFTER_SOURCES = 2                      # a new technique goes live when this many different videos teach it
_NUM = library._num


def _text(value, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+|\s+", " ", str(value or "")).strip()[:limit]


def _tags(value) -> list[str]:
    """Only tags from the agreed list (a fixed vocabulary keeps notes and searches in step)."""
    return [t for t in hubmod.clean_tags(value if isinstance(value, list) else []) if t in hubmod.TAGS][:5]


def _steps(value) -> str:
    items = value if isinstance(value, list) else str(value or "").split("|")
    return " | ".join(_text(x, 220) for x in items if _text(x, 220))[:900]


def check_proof(raw: dict, window: tuple[int, int], video_tokens: int | None) -> list[str]:
    """The library's proof that the part was really watched; returns the problems found (none = proven)."""
    start, end = window
    length = max(1, end - start)
    problems = []
    watched = raw.get("watched") if isinstance(raw.get("watched"), dict) else {}
    seen = _NUM(watched.get("seconds"))
    if seen is None or abs(seen - length) > max(library.SECONDS_TOLERANCE * length, 10):
        problems.append(f"it reported {seen if seen is not None else 'no'} seconds watched; this part is {length}")
    if not str(watched.get("first_words") or "").strip() or not str(watched.get("last_words") or "").strip():
        problems.append("it gave no first or last words")
    if video_tokens is None:
        problems.append("Google reported no video token count, so the watch cannot be proven")
    elif video_tokens / length < library.MIN_TOKENS_PER_SECOND:
        problems.append(f"only {video_tokens / length:.0f} video tokens per second (at least {library.MIN_TOKENS_PER_SECOND})")
    return problems


def _times(items: list[dict], window: tuple[int, int]) -> int:
    """Seconds into the whole video for a part's relative times: 0 when Gemini counted from the part's start (asked for),
    the part's start when it clearly used whole-video times instead."""
    start, end = window
    length = max(1, end - start)
    values = [_NUM(x.get("at_s")) for x in items]
    relative = sum(1 for t in values if t is not None and 0 <= t <= length * 1.05)
    absolute = sum(1 for t in values if t is not None and start <= t <= end + length * 0.05) if start else 0
    return start if absolute > relative else 0


def validate_tutorial(text: str, window: tuple[int, int], video_tokens: int | None) -> tuple[dict, bool, list[str]]:
    """(cleaned note, usable, problems) for one part of a tutorial."""
    try:
        raw = llm.extract_json(text)
    except ValueError:
        return {"raw": text[:1500]}, False, ["the reply was not JSON"]
    if not isinstance(raw, dict):
        return {"raw": text[:1500]}, False, ["the reply was not a JSON object"]
    problems = check_proof(raw, window, video_tokens)
    start, end = window
    length = max(1, end - start)
    techs_in = [x for x in raw.get("techniques", []) if isinstance(x, dict)] if isinstance(raw.get("techniques"), list) else []
    lessons_in = [x for x in raw.get("lessons", []) if isinstance(x, dict)] if isinstance(raw.get("lessons"), list) else []
    offset = _times(techs_in + lessons_in, window)
    techniques, lessons, dropped = [], [], 0
    for x in techs_in[:MAX_TECHNIQUES]:
        t, name, what = _NUM(x.get("at_s")), _text(x.get("name"), 80), _text(x.get("what"), 500)
        if t is None or not name or not what or not 0 <= t - offset <= length * 1.05:
            dropped += 1
            continue
        techniques.append({"at_s": round(start + min(t - offset, length), 1), "name": name, "what": what,
                           "when_to_use": _text(x.get("when_to_use"), 300), "how": _steps(x.get("how")), "tags": _tags(x.get("tags"))})
    for x in lessons_in[:MAX_LESSONS]:
        t, principle = _NUM(x.get("at_s")), _text(x.get("principle"), 400)
        if t is None or not principle or not 0 <= t - offset <= length * 1.05:
            dropped += 1
            continue
        lessons.append({"at_s": round(start + min(t - offset, length), 1), "topic": _text(x.get("topic"), 60),
                        "principle": principle, "tags": _tags(x.get("tags"))})
    given = len(techs_in[:MAX_TECHNIQUES]) + len(lessons_in[:MAX_LESSONS])
    if given and dropped / given > library.DROPPED_TOLERANCE:
        problems.append(f"{dropped} of {given} items had no text or a time outside this part")
    note = {"window": [start, end], "summary": _text(raw.get("summary"), 600), "techniques": techniques, "lessons": lessons,
            "dropped": dropped}
    return note, not problems, problems


def validate_reference(text: str, window: tuple[int, int], video_tokens: int | None) -> tuple[dict, bool, list[str]]:
    try:
        raw = llm.extract_json(text)
    except ValueError:
        return {"raw": text[:1500]}, False, ["the reply was not JSON"]
    if not isinstance(raw, dict):
        return {"raw": text[:1500]}, False, ["the reply was not a JSON object"]
    problems = check_proof(raw, window, video_tokens)
    hook = raw.get("hook") if isinstance(raw.get("hook"), dict) else {}
    pacing = raw.get("pacing") if isinstance(raw.get("pacing"), dict) else {}
    caps = raw.get("captions") if isinstance(raw.get("captions"), dict) else {}
    sound = raw.get("sound") if isinstance(raw.get("sound"), dict) else {}
    if not _text(hook.get("what"), 300):
        problems.append("it did not describe the hook")
    borrow = []
    for x in (raw.get("borrow") if isinstance(raw.get("borrow"), list) else [])[:MAX_BORROW]:
        if isinstance(x, dict) and _text(x.get("name"), 80) and _text(x.get("what"), 300):
            borrow.append({"name": _text(x["name"], 80), "what": _text(x["what"], 300), "tags": _tags(x.get("tags"))})

    def strings(key, limit, n):
        return [_text(s, limit) for s in (raw.get(key) if isinstance(raw.get(key), list) else [])[:n] if _text(s, limit)]
    note = {"window": list(window), "summary": _text(raw.get("summary"), 500), "format": _text(raw.get("format"), 40),
            "hook": {"at_s": _NUM(hook.get("at_s")), "what": _text(hook.get("what"), 300), "why_it_works": _text(hook.get("why_it_works"), 300)},
            "pacing": {"cuts_per_10s": _NUM(pacing.get("cuts_per_10s")), "average_shot_seconds": _NUM(pacing.get("average_shot_seconds")),
                       "feel": _text(pacing.get("feel"), 20)},
            "captions": {"present": bool(caps.get("present")), "style": _text(caps.get("style"), 300)},
            "graphics": strings("graphics", 200, 8), "cta": _text(raw.get("cta"), 300),
            "sound": {"music": _text(sound.get("music"), 30), "effects": _text(sound.get("effects"), 30), "voice": _text(sound.get("voice"), 30)},
            "palette": strings("palette", 30, 4), "works_because": strings("works_because", 200, 3), "borrow": borrow,
            "do_not_copy": strings("do_not_copy", 200, 5)}
    return note, not problems, problems


# ---------------------------------------------------------------- filing the knowledge

STOP = {"the", "a", "an", "of", "on", "in", "for", "to", "and", "with", "your", "from", "at", "key"}


def _tokens(text: str) -> set[str]:
    return {t for t in hubmod.slugify(text, 120).split("-") if t and t not in STOP and len(t) > 1}


def similar_technique(hub: Hub, name: str) -> dict | None:
    """An ACTIVE technique that is plainly the same thing under another name ("punch-in zoom" and "Punch-in on the key
    word"): the search finds candidates, and one is accepted only when most of the name's words appear in it."""
    wanted = _tokens(name)
    if not wanted:
        return None
    for hit in hub.search(name, kinds=["technique"], limit=5):
        have = _tokens(hit["title"]) | _tokens(hit["slug"])
        if len(wanted & have) / len(wanted | have) >= 0.5 or wanted <= have:
            return hit
    return None


def _technique_for(hub: Hub, name: str, what: str, tags: list[str], vid: str, origin: str) -> int:
    """The technique note this name refers to: an existing one (any status), or a new draft. Each different video that teaches
    a draft is counted; the second one makes it active."""
    slug = hubmod.slugify(name, 70)
    existing = hub.get("technique", slug)
    if not existing:
        known = similar_technique(hub, name)
        if known:
            return known["id"]
        return hub.upsert("technique", slug, name, what, tags, {"sources": [vid]}, origin=origin, status="draft")
    sources = list(existing["meta"].get("sources") or [])
    if vid not in sources and existing["status"] == "draft":
        sources.append(vid)
        status = "active" if len(sources) >= ACTIVE_AFTER_SOURCES else "draft"
        hub.upsert("technique", slug, existing["title"], existing["body"], sorted(set(existing["tags"]) | set(tags)),
                   {**existing["meta"], "sources": sources}, origin=existing["origin"], status=status)
    return existing["id"]


def _source(hub: Hub, vid: str, meta: dict, summary: str, tags: list[str], goal: str, origin: str = "youtube") -> int:
    return hub.upsert("source", f"yt-{vid}", meta.get("title") or vid, summary, tags,
                      {"platform": "youtube", "external_id": vid, "channel": meta.get("channel", ""), "seconds": meta.get("seconds"),
                       "views": meta.get("views"), "published_at": meta.get("published_at"), "url": f"https://www.youtube.com/watch?v={vid}",
                       "learned_for": goal[:200], "checked_at": datetime.now(timezone.utc).isoformat()}, origin=origin)


def ingest_tutorial(hub: Hub, vid: str, meta: dict, note: dict, goal: str) -> tuple[int, int]:
    """File one part's techniques and lessons. Returns (recipes, lessons) written."""
    tags = sorted({t for x in note["techniques"] + note["lessons"] for t in x["tags"]})[:8]
    src = _source(hub, vid, meta, note.get("summary", ""), tags or ["tools"], goal)
    recipes = lessons = 0
    for x in note["techniques"]:
        tid = _technique_for(hub, x["name"], f"{x['what']} {x['when_to_use']}".strip(), x["tags"], vid, "youtube")
        at = int(x["at_s"])
        body = f"{x['what']}\n\nWhen to use it: {x['when_to_use']}\n\nHow: {x['how']}\n\nTaught at {at // 60}:{at % 60:02d} in the video."
        rid = hub.upsert("recipe", f"yt-{vid}-{at}-{hubmod.slugify(x['name'], 40)}", f"{x['name']} (from a video)", body, x["tags"],
                         {"url": f"https://www.youtube.com/watch?v={vid}&t={at}s", "at_s": at, "video": vid, "channel": meta.get("channel", ""),
                          "licence": "our own notes on a public video"}, origin="youtube")
        hub.link(rid, tid, "implements", "taught in a studied video")
        hub.link(rid, src, "part_of")
        recipes += 1
    for x in note["lessons"]:
        at = int(x["at_s"])
        lid = hub.upsert("lesson", f"yt-{vid}-{at}", x["topic"] or x["principle"][:60], x["principle"], x["tags"],
                         {"at_s": at, "video": vid, "channel": meta.get("channel", ""), "url": f"https://www.youtube.com/watch?v={vid}&t={at}s",
                          "source": f"yt-{vid}"}, origin="youtube")
        hub.link(lid, src, "part_of")
        lessons += 1
    return recipes, lessons


def example_body(note: dict) -> str:
    h, p, c, s = note["hook"], note["pacing"], note["captions"], note["sound"]
    lines = [note["summary"], "", f"**Format:** {note['format']}",
             f"**Hook** (at {h['at_s'] if h['at_s'] is not None else '?'} s): {h['what']} Why it works: {h['why_it_works']}",
             f"**Pacing:** about {p['cuts_per_10s'] if p['cuts_per_10s'] is not None else '?'} cuts per 10 s, shots of about "
             f"{p['average_shot_seconds'] if p['average_shot_seconds'] is not None else '?'} s, {p['feel']}.",
             f"**Captions:** {c['style'] if c['present'] else 'none'}", f"**Sound:** music {s['music']}, effects {s['effects']}, voice {s['voice']}",
             f"**Ends with:** {note['cta']}"]
    if note["graphics"]:
        lines += ["", "**Graphics and effects:**", *[f"- {g}" for g in note["graphics"]]]
    if note["works_because"]:
        lines += ["", "**Why it works:**", *[f"- {w}" for w in note["works_because"]]]
    if note["do_not_copy"]:
        lines += ["", "**Do not copy (risky for a financial advertiser):**", *[f"- {d}" for d in note["do_not_copy"]]]
    return "\n".join(x for x in lines if x is not None).strip()


def ingest_reference(hub: Hub, vid: str, meta: dict, note: dict, goal: str, origin: str = "youtube") -> int:
    """File a reference example and link it to the techniques it borrows from. Returns the example's id."""
    tags = sorted({t for b in note["borrow"] for t in b["tags"]} | {"performance", "social"})[:8]
    fingerprint = {"format": note["format"], "pacing": note["pacing"], "captions": note["captions"], "palette": note["palette"],
                   "music": note["sound"]["music"], "effects": note["sound"]["effects"]}
    eid = hub.upsert("example", f"ref-yt-{vid}", meta.get("title") or vid, example_body(note), tags,
                     {"platform": "youtube", "url": f"https://www.youtube.com/watch?v={vid}", "channel": meta.get("channel", ""),
                      "views": meta.get("views"), "published_at": meta.get("published_at"), "seconds": meta.get("seconds"),
                      "fingerprint": fingerprint, "learned_for": goal[:200], "checked_at": datetime.now(timezone.utc).isoformat()},
                     origin=origin)
    for b in note["borrow"]:
        tid = _technique_for(hub, b["name"], b["what"], b["tags"], vid, origin)
        hub.link(eid, tid, "example_of", b["what"][:200])
    return eid
