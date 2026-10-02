"""Reference analysis -> a reusable style profile (the "what good looks like" tier).

Gemini Pro with deep reasoning watches a reference video once (a public YouTube link, or a small local file) and
writes a style profile: how fast it cuts, how its captions, headlines, callouts and end screens look and move, and
how it handles transitions. The profile is expressed in this renderer's own vocabulary (design.py), so the footage
planner can use it directly. Profiles live in brain/profiles/*.json and go into every planning prompt; the cheaper
footage model then plans each ad inside that grammar and still varies the look ad to ad.

Only validated profiles are saved: names outside the vocabulary are dropped, numbers are clamped.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import design
import llm

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "brain" / "profiles"
MAX_LOCAL_BYTES = 60 * 1024 * 1024
MAX_PROMPT_CHARS = 8000
ZOOM_PUNCH = ("none", "rare", "every_4_6_seconds", "frequent")
TRANSITIONS = ("hard_cut", "zoom_punch", "whip_pan", "speed_ramp", "dip_to_black", "match_cut", "j_cut", "l_cut")
RENDERED_TRANSITIONS = ("hard_cut", "zoom_punch")      # the renderer only does these; the rest are guidance
YOUTUBE = re.compile(r"^https://(?:www\.youtube\.com/watch\?(?:[\w=&%-]*&)?v=|youtu\.be/)([A-Za-z0-9_-]{11})(?:[&?][\w=&%.-]*)?$")


def normalize_youtube(source) -> str | None:
    """The canonical link for a YouTube video (any share form), or None if it is not one. Share links carry a
    tracking id (`?si=...`) that identifies who shared it; it is dropped before the link goes anywhere."""
    m = YOUTUBE.match(source) if isinstance(source, str) else None
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


def parse_list(text: str, default_minutes: float = 15.0) -> list[tuple[str, float, str]]:
    """Lines of `link [minutes] [name]` (blank lines and # comments ignored) -> (canonical link, minutes, name)."""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        url = normalize_youtube(parts[0])
        if not url:
            raise llm.LLMError(f"not a YouTube video link: {parts[0][:80]}")
        try:
            minutes = float(parts[1]) if len(parts) > 1 else default_minutes
        except ValueError as err:
            raise llm.LLMError(f"minutes must be a number in: {line[:80]}") from err
        out.append((url, minutes, parts[2] if len(parts) > 2 else "ref_" + url[-11:]))
    return out


def _names(raw, menu) -> list[str]:
    return [x for x in (raw if isinstance(raw, list) else []) if isinstance(x, str) and x in menu]


def _text(v, limit: int) -> str:
    return " ".join(v.split())[:limit] if isinstance(v, str) else ""


def _num(v, lo: float, hi: float, default: float) -> float:
    try:
        return round(min(hi, max(lo, float(v))), 3)
    except (TypeError, ValueError):
        return default


def sanitize(raw, name: str, source: str) -> tuple[dict, list[str]]:
    """Turn the analyst's reply into a safe profile. Never raises."""
    notes: list[str] = []
    raw = raw if isinstance(raw, dict) else {}

    def sub(key: str) -> dict:
        v = raw.get(key)
        return v if isinstance(v, dict) else {}

    cad, cap, head, call, trans, end = (sub(k) for k in ("cadence", "captions", "headline", "callouts",
                                                         "transitions", "end_screen"))
    pace = cad.get("pace")
    zoom = cad.get("zoom_punch")
    prof = {
        "name": re.sub(r"[^\w.-]+", "_", name)[:60] or "profile", "source": source[:200],
        "summary": _text(raw.get("summary"), 400),
        "cadence": {"average_shot_seconds": _num(cad.get("average_shot_seconds"), 0.2, 30, 2.0),
                    "cut_on_beat": bool(cad.get("cut_on_beat")), "b_roll_ratio": _num(cad.get("b_roll_ratio"), 0, 1, 0),
                    "zoom_punch": zoom if zoom in ZOOM_PUNCH else "none",
                    "pace": pace if pace in design.PACING else "natural"},
        "captions": {"styles": _names(cap.get("styles"), design.CAPTION_STYLES),
                     "case": _names(cap.get("case"), design.CAPTION_CASES),
                     "position": _names(cap.get("position"), design.CAPTION_POSITIONS),
                     "notes": _text(cap.get("notes"), 240)},
        "headline": {"styles": _names(head.get("styles"), design.HEADLINE_STYLES), "notes": _text(head.get("notes"), 240)},
        "callouts": {"styles": _names(call.get("styles"), design.CALLOUT_STYLES),
                     "motions": _names(call.get("motions"), design.MOTIONS), "notes": _text(call.get("notes"), 240)},
        "transitions": {"types": _names(trans.get("types"), TRANSITIONS), "notes": _text(trans.get("notes"), 240)},
        "end_screen": {"layouts": _names(end.get("layouts"), design.END_LAYOUTS), "tone": _text(end.get("tone"), 160)},
        "rules": [t for t in (_text(x, 160) for x in (raw.get("rules") if isinstance(raw.get("rules"), list) else []))
                  if t][:12],
        "avoid": [t for t in (_text(x, 160) for x in (raw.get("avoid") if isinstance(raw.get("avoid"), list) else []))
                  if t][:8],
    }
    if not raw:
        notes.append("The analyst returned no profile.")
    if not prof["captions"]["styles"] and not prof["callouts"]["styles"] and not prof["headline"]["styles"]:
        notes.append("The profile names no caption, headline or callout style this renderer can draw, so it adds "
                     "only cadence and rules.")
    unrendered = [t for t in prof["transitions"]["types"] if t not in RENDERED_TRANSITIONS]
    if unrendered:
        notes.append(f"Transitions the renderer cannot draw yet (kept as guidance): {', '.join(unrendered)}.")
    return prof, notes


def profile_schema() -> dict:
    """Schema for the analyst's reply, built from the renderer's vocabulary."""
    def names(menu):
        return {"type": "array", "items": {"type": "string", "enum": list(menu)}}

    string = {"type": "string"}
    return {"type": "object", "properties": {
        "summary": string,
        "cadence": {"type": "object", "properties": {
            "average_shot_seconds": {"type": "number"}, "cut_on_beat": {"type": "boolean"},
            "b_roll_ratio": {"type": "number"}, "zoom_punch": {"type": "string", "enum": list(ZOOM_PUNCH)},
            "pace": {"type": "string", "enum": list(design.PACING)}},
            "required": ["average_shot_seconds", "cut_on_beat", "b_roll_ratio", "zoom_punch", "pace"]},
        "captions": {"type": "object", "properties": {"styles": names(design.CAPTION_STYLES),
                                                      "case": names(design.CAPTION_CASES),
                                                      "position": names(design.CAPTION_POSITIONS), "notes": string},
                     "required": ["styles", "case", "position", "notes"]},
        "headline": {"type": "object", "properties": {"styles": names(design.HEADLINE_STYLES), "notes": string},
                     "required": ["styles", "notes"]},
        "callouts": {"type": "object", "properties": {"styles": names(design.CALLOUT_STYLES),
                                                      "motions": names(design.MOTIONS), "notes": string},
                     "required": ["styles", "motions", "notes"]},
        "transitions": {"type": "object", "properties": {"types": names(TRANSITIONS), "notes": string},
                        "required": ["types", "notes"]},
        "end_screen": {"type": "object", "properties": {"layouts": names(design.END_LAYOUTS), "tone": string},
                       "required": ["layouts", "tone"]},
        "rules": {"type": "array", "items": string}, "avoid": {"type": "array", "items": string}},
        "required": ["summary", "cadence", "captions", "headline", "callouts", "transitions", "end_screen", "rules",
                     "avoid"]}


def build_prompt(name: str) -> str:
    return (HERE / "prompts" / "analyze_reference.md").read_text(encoding="utf-8").format(
        name=name, menu=design.menu_text(), zoom_punch=", ".join(ZOOM_PUNCH),
        transitions=", ".join(TRANSITIONS), rendered=", ".join(RENDERED_TRANSITIONS))


def estimate(cfg: dict, name: str, minutes: float) -> float:
    """Worst-case cost of one analysis, before spending anything."""
    model, _ = llm.model_for(cfg, "reference")
    return llm.estimate_cost(model, llm.video_tokens(minutes * 60) + len(build_prompt(name)) // 3, 12000)


def analyze(cfg: dict, client: llm.OpenRouter, source: str | Path, name: str, minutes: float = 15.0):
    """Watch one reference and return (profile, notes, usage). `source` is a public YouTube link or a small local
    video file. YouTube links go over the one route that accepts them (not zero-retention: public videos only);
    local files go zero-retention."""
    model, reasoning = llm.model_for(cfg, "reference")
    prompt = build_prompt(name)
    link = normalize_youtube(source)
    if link:
        video, route, seconds, label = {"url": link}, "youtube", minutes * 60, link
    else:
        path = Path(source)
        if not path.is_file():
            raise llm.LLMError(f"reference not found: {source} (give a youtube.com/watch?v=... link or a video file)")
        if path.stat().st_size > MAX_LOCAL_BYTES:
            raise llm.LLMError(f"{path.name} is {path.stat().st_size / 1e6:.0f} MB; make a copy under "
                               f"{MAX_LOCAL_BYTES // 2**20} MB first")
        video = {"url": "data:video/mp4;base64," + base64.b64encode(path.read_bytes()).decode("ascii")}
        route, seconds, label = "zdr", minutes * 60, path.name
    content = [{"type": "video_url", "video_url": video}, {"type": "text", "text": prompt}]
    raw, usage = client.chat_json(model, content, route=route, label=f"reference {name}",
                                  est_input_tokens=llm.video_tokens(seconds) + len(prompt) // 3, max_tokens=12000,
                                  reasoning=reasoning, attempts=1, timeout=900, schema=profile_schema(),
                                  schema_name="style_profile")
    profile, notes = sanitize(raw, name, label)
    return profile, notes, usage


def save(profile: dict, directory: Path = PROFILE_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{profile['name']}.json"
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return path


def _render(p: dict) -> str:
    c, cap, head, call, tr, end = (p["cadence"], p["captions"], p["headline"], p["callouts"], p["transitions"],
                                   p["end_screen"])

    def lst(xs):
        return ", ".join(xs) if xs else "any"

    lines = [f"### {p['name']}" + (f" (from {p['source']})" if p["source"] else ""), p["summary"],
             f"- Cadence: average shot {c['average_shot_seconds']} s, cuts on the beat: "
             f"{'yes' if c['cut_on_beat'] else 'no'}, B-roll {round(c['b_roll_ratio'] * 100)}%, zoom punches: "
             f"{c['zoom_punch'].replace('_', ' ')}, pace: {c['pace']}",
             f"- Captions: {lst(cap['styles'])}; case {lst(cap['case'])}; position {lst(cap['position'])}. {cap['notes']}",
             f"- Headlines: {lst(head['styles'])}. {head['notes']}",
             f"- Callouts: {lst(call['styles'])}; motion {lst(call['motions'])}. {call['notes']}",
             f"- Transitions: {lst(tr['types'])}. {tr['notes']}",
             f"- End screens: {lst(end['layouts'])}. {end['tone']}"]
    if p["rules"]:
        lines.append("- Rules: " + " | ".join(p["rules"]))
    if p["avoid"]:
        lines.append("- Avoid: " + " | ".join(p["avoid"]))
    return "\n".join(x for x in lines if x.strip())


def load_all(directory: Path = PROFILE_DIR, limit: int = MAX_PROMPT_CHARS) -> str:
    """Every saved profile, written for the planning prompt."""
    out = []
    for f in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            p, _ = sanitize(json.loads(f.read_text(encoding="utf-8")), f.stem, "")
            raw = json.loads(f.read_text(encoding="utf-8"))
            p["source"] = _text(raw.get("source"), 200) if isinstance(raw, dict) else ""
            out.append(_render(p))
        except (OSError, ValueError):
            continue
    return "\n\n".join(out)[:limit] if out else "(No style profiles yet.)"
