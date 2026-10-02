"""What the agent learns from ads that are performing in the market (Foreplay), checked and filed into the knowledge hub.

Foreplay gives each ad's words and metadata, not its pictures, so what is filed is how the ad persuades (its hook, structure,
devices, ending) and what the team could borrow, in our own words, plus how long the ad has been running and whether it is
still live, which is the only performance signal available. Each ad becomes an `example` note (`ref-fp-<id>`) linked to the
techniques it borrows. A technique an ad suggests starts as a draft and goes live when two different ADVERTISERS show it
(one brand running ten look-alike ads counts once). The ad's full text is never stored.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import hub as hubmod
import hub_learn
from hub import Hub
from hub_learn import _tags, _text

MAX_BORROW = 4
TONES = {"calm", "confident", "urgent", "friendly", "technical"}


FENCES = re.compile(r">{3,}|<{3,}")


def slug(ad_id: str) -> str:
    """The note's slug exactly as the hub stores it (lower-case, dashes), so a lookup finds what ingest wrote."""
    return hubmod.slugify(f"ref-fp-{ad_id}", 120)


def prompt_ads(ads: list[dict]) -> list[dict]:
    """The ads as the analysis prompt sees them: only the fields it needs, with anything that could close the prompt's data
    block neutralised (the text is other companies' words, never instructions)."""
    keep = ("id", "headline", "description", "cta", "words", "running_days", "live", "video_seconds", "platforms")
    out = []
    for ad in ads:
        row = {k: ad.get(k) for k in keep if ad.get(k) not in (None, "", [])}
        out.append({k: (FENCES.sub(" ", v) if isinstance(v, str) else v) for k, v in row.items()})
    return out


def usable(ad: dict, min_days: int) -> bool:
    """Worth analysing: a video that has run long enough and has words or a headline to study."""
    days = ad.get("running_days")
    return (ad.get("format") in ("video", "") and (days is None or days >= min_days)
            and bool(ad.get("words") or ad.get("headline") or ad.get("description")))


def validate(raw, sent: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    """({ad id: cleaned analysis} for the ads we sent that came back with a hook, problems). Unknown ids are ignored."""
    problems: list[str] = []
    items = raw.get("ads") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return {}, ["the reply had no ads list"]
    out: dict[str, dict] = {}

    def strings(x, key, limit, n):
        v = x.get(key)
        return [_text(s, limit) for s in (v if isinstance(v, list) else [])[:n] if _text(s, limit)]
    for x in items:
        if not isinstance(x, dict) or _text(x.get("id"), 60) not in sent:
            continue
        ad_id = _text(x["id"], 60)
        if not _text(x.get("hook"), 300):
            problems.append(f"ad {ad_id} had no hook")
            continue
        borrow = [{"name": _text(b["name"], 80), "what": _text(b["what"], 300), "tags": _tags(b.get("tags"))}
                  for b in (x.get("borrow") if isinstance(x.get("borrow"), list) else [])[:MAX_BORROW]
                  if isinstance(b, dict) and _text(b.get("name"), 80) and _text(b.get("what"), 300)]
        tone = _text(x.get("tone"), 20).lower()
        out[ad_id] = {"hook": _text(x.get("hook"), 300), "structure": strings(x, "structure", 80, 8),
                      "devices": strings(x, "devices", 40, 6), "tone": tone if tone in TONES else "",
                      "cta": _text(x.get("cta"), 300), "works_because": strings(x, "works_because", 200, 3), "borrow": borrow,
                      "do_not_copy": strings(x, "do_not_copy", 200, 5)}
    return out, problems


def body(ad: dict, note: dict) -> str:
    days = ad.get("running_days")
    lines = [f"**Performance signal:** {'still live, ' if ad.get('live') else 'ended, '}"
             f"{'running ' + str(days) + ' days' if days is not None else 'running time unknown'} (a long run suggests it earns its spend).",
             f"**Hook:** {note['hook']}"]
    if note["structure"]:
        lines.append("**Structure:** " + " → ".join(note["structure"]))
    if note["devices"]:
        lines.append("**Devices:** " + ", ".join(note["devices"]) + (f" · tone: {note['tone']}" if note["tone"] else ""))
    if note["cta"]:
        lines.append(f"**Ends with:** {note['cta']}")
    if note["works_because"]:
        lines += ["", "**Why it works:**", *[f"- {w}" for w in note["works_because"]]]
    if note["do_not_copy"]:
        lines += ["", "**Do not copy (risky for a financial advertiser):**", *[f"- {d}" for d in note["do_not_copy"]]]
    return "\n".join(lines).strip()


def ingest(hub: Hub, ad: dict, note: dict, goal: str) -> int:
    """File one ad's analysis as an example note and link it to the techniques it borrows. Returns the note's id."""
    tags = sorted({t for b in note["borrow"] for t in b["tags"]} | {"ads", "performance", "meta"})[:8]
    title = _text(ad.get("headline") or ad.get("name") or ad["id"], 120)
    eid = hub.upsert("example", slug(ad["id"]), title, body(ad, note), tags,
                     {"platform": "foreplay", "external_id": ad["id"], "url": ad.get("url", ""), "brand": ad.get("brand_id", ""),
                      "running_days": ad.get("running_days"), "live": bool(ad.get("live")), "seconds": ad.get("video_seconds"),
                      "format": ad.get("format", ""), "devices": note["devices"], "tone": note["tone"], "learned_for": goal[:200],
                      "checked_at": datetime.now(timezone.utc).isoformat()}, origin="foreplay")
    who = f"fp-{ad.get('brand_id') or ad['id']}"
    for b in note["borrow"]:
        tid = hub_learn._technique_for(hub, b["name"], b["what"], b["tags"], who, "foreplay")
        hub.link(eid, tid, "example_of", b["what"][:200])
    return eid
