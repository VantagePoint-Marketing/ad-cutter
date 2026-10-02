"""What the planner is told from the knowledge hub: a short, bounded list of techniques and lessons relevant to this request.

`fetch(hub, request)` never raises and never returns more than MAX_CHARS, so a missing or empty hub only means the
planner gets less help, never that a job fails. The text goes into the prompt as hints, under a heading that says they
never override the request or the format rules; hub notes are our own words plus a pointer to where they came from.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("ad-cutter")
MAX_CHARS = 5500
EMPTY = "(Nothing relevant yet.)"
# what a plan always needs to think about, so there is useful help even for an empty or vague request
CORE_QUERY = "hook opening first seconds pacing cut"


def one_line(text: str, limit: int) -> str:
    flat = re.sub(r"[\x00-\x1f\x7f`{}]+|\s+", " ", str(text or "")).replace('"', "'").strip()
    return flat[:limit].rstrip() + ("…" if len(flat) > limit else "")


REF_CHARS = 1500


def fetch_references(hub, request: str = "", limit: int = 3) -> tuple[str, list[int]]:
    """(text, note ids) for the finished-ad check: a few reference examples (ads and short videos that are performing) the
    ad can be compared with. Never raises; ("", []) when there are none."""
    try:
        query = one_line(request, 300)
        seen: dict[int, dict] = {}
        for q in ([query] if query else []) + [CORE_QUERY]:
            for it in hub.search(q, kinds=["example"], limit=limit):
                seen.setdefault(it["id"], it)
        lines, ids, used = [], [], 0
        for it in list(seen.values())[:limit]:
            meta = it.get("meta") or {}
            days = meta.get("running_days")
            where = f"an ad that has run {days} days" if days else "a short video that performs well"
            line = f"- \"{one_line(it['title'], 80)}\" ({where}): {one_line(it['body'], 380)}"
            if used + len(line) + 1 > REF_CHARS:
                break
            lines.append(line)
            ids.append(it["id"])
            used += len(line) + 1
        return "\n".join(lines), ids
    except Exception as err:   # noqa: BLE001 - the check just has nothing to compare with
        log.warning("hub: could not read references (%s)", type(err).__name__)
        return "", []


def fetch(hub, request: str = "", max_chars: int = MAX_CHARS) -> str:
    try:
        query = one_line(request, 300)
        seen: set[int] = set()
        lines: list[str] = []

        def add(items, fmt):
            for it in items:
                if it["id"] not in seen:
                    seen.add(it["id"])
                    lines.append(fmt(it))

        techniques = hub.search(query, kinds=["technique"], limit=4) if query else []
        add(techniques, lambda t: f"- Technique, {t['title']}: {one_line(t['body'], 260)}")
        add(hub.search(CORE_QUERY, kinds=["technique"], limit=3),
            lambda t: f"- Technique, {t['title']}: {one_line(t['body'], 260)}")
        add(hub.search(query or CORE_QUERY, kinds=["rule"], limit=3), lambda r: f"- Rule, {r['title']}: {one_line(r['body'], 260)}")
        add(hub.search(query or CORE_QUERY, kinds=["lesson"], limit=4),
            lambda l: f"- Lesson from a video ({l['meta'].get('channel') or 'a studied video'}): {one_line(l['body'], 240)}")
        design_tags = ["typography", "motion", "captions", "graphics", "overlay", "color", "layout", "transition"]
        add(hub.search(query or CORE_QUERY, kinds=["technique", "recipe"], tags=design_tags, limit=4),
            lambda t: f"- Design craft, {t['title']}: {one_line(t['body'], 260)}")
        add(hub.search(query or CORE_QUERY, kinds=["example"], limit=3), lambda e: f"- Reference that is performing ({one_line(e['title'], 70)}): "
            f"{one_line(e['body'], 300)}" + (f" [an ad that has run {one_line(e['meta'].get('running_days'), 6)} days]" if (e.get("meta") or {}).get("running_days") else ""))
        out, used = [], 0
        for line in lines:
            if used + len(line) + 1 > max_chars:
                break
            out.append(line)
            used += len(line) + 1
        return "\n".join(out) or EMPTY
    except Exception as err:   # noqa: BLE001 - the planner just gets less help
        log.warning("hub: could not read knowledge for the plan (%s)", type(err).__name__)
        return EMPTY
