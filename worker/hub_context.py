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
