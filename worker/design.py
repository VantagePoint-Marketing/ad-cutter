"""The look of one ad, chosen by the planner and rendered here.

Nothing in the overlays is fixed. For every ad Gemini picks a design from the menus below: editing pace, caption
style, headline style, callout style and placement, motion, punch-in zooms, colours, and the end screen with its own
call-to-action text. The model only picks names and short text; this module turns them into HTML, CSS and GSAP, so
no model-written markup or script ever reaches the render browser. Anything missing or invalid falls back to a
safe default and is reported in the notes.
"""
from __future__ import annotations

import html
import json
import re

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")

# name -> what the planner is told it does. The validator and the prompt menu both read these, so they cannot drift.
PACING = {
    "tight": "pauses cut hard; energetic, for punchy or high-energy talk",
    "natural": "light trimming; the speaker's own rhythm",
    "breathing": "keeps more air around pauses; calm, serious or emotional talk",
}
PACING_VALUES = {"tight": (0.35, 0.06), "natural": (0.5, 0.12), "breathing": (0.8, 0.28)}   # (pause_min, pause_keep) s

CAPTION_STYLES = {
    "bold_outline": "huge heavy white words with a black outline, active word in the accent colour",
    "clean_shadow": "smaller, calmer words with a soft shadow and no outline; for serious or premium topics",
    "pill_all": "words sit in a dark rounded pill; active word in the accent colour",
    "impact_single": "one giant word at a time; for fast, punchy or comedic delivery",
    "bar_karaoke": "full-width dark bar behind the words, active word in the accent colour",
}
CAPTION_POSITIONS = {"low": 1075, "middle": 830, "high": 610}
CAPTION_CASES = ("upper", "sentence", "lower")
CAPTION_SIZES = {"small": (64, 24), "medium": (80, 17), "large": (100, 13)}   # (font px, max characters per group)

HEADLINE_STYLES = {
    "white_box": "white rounded box, dark text",
    "ribbon": "slightly tilted accent-colour ribbon",
    "big_stack": "very large uppercase outlined words with no box",
    "underline": "plain bold text with an accent underline",
    "tag": "small left-aligned label with an accent edge",
    "none": "no headline at all (when the footage speaks for itself)",
}
HEADLINE_POSITIONS = {"top": 280, "upper": 470}

CALLOUT_STYLES = {
    "marker_box": "hand-drawn marker box with an arrow",
    "sticky_note": "tilted sticky note in the accent colour",
    "pill_label": "bold uppercase label in a rounded pill",
    "scribble": "outlined words with a hand-drawn underline",
    "banner": "full-width band across the frame",
}
CALLOUT_SIDES = ("left", "right", "center")

MOTIONS = {
    "pop": "scales in with a bounce",
    "slide": "slides in from the side",
    "drop": "drops in from above",
    "wipe": "wipes open",
    "fade": "plain fade",
}

END_LAYOUTS = {
    "centered_card": "dimmed frame, centred line and button",
    "bottom_sheet": "a sheet rises from the bottom while the video stays visible",
    "big_question": "one huge line over a dimmed frame with a small button",
    "split_bar": "an accent bar across the top and a button at the bottom",
    "minimal_line": "a single understated line near the bottom, no dimming",
}

DEFAULT_PALETTE = {"accent": "#FFE11A", "accent2": "#E3261C", "ink": "#111111", "paper": "#FFFFFF"}
MAX_PUNCH_INS = 10
END_SECONDS = (1.5, 4.0)
CALLOUT_BANDS = (540, 700, 860, 1000, 1130)     # candidate top positions (px) for callouts, inside Meta's safe zone


def menu_text() -> str:
    """The choices, written for the planner prompt."""
    def block(title: str, menu: dict[str, str]) -> str:
        return f"- {title}:\n" + "\n".join(f"    - `{k}`: {v}" for k, v in menu.items())

    return "\n".join([
        block("`pacing`", PACING), block("`captions.style`", CAPTION_STYLES),
        f"- `captions.position`: {', '.join(CAPTION_POSITIONS)}. `captions.case`: {', '.join(CAPTION_CASES)}. "
        f"`captions.size`: {', '.join(CAPTION_SIZES)}. `captions.words_per_group`: 1 to 4.",
        block("`headline.style`", HEADLINE_STYLES), f"- `headline.position`: {', '.join(HEADLINE_POSITIONS)}.",
        block("`callouts.style`", CALLOUT_STYLES),
        f"- each callout's `side`: {', '.join(CALLOUT_SIDES)}.",
        block("`motion` (for `headline_motion`, `callout_motion`, `end_screen.motion`)", MOTIONS),
        block("`end_screen.layout`", END_LAYOUTS),
    ])


# ---------------------------------------------------------------- colour helpers

def _lum(h: str) -> float:
    def ch(i: int) -> float:
        v = int(h[1 + 2 * i:3 + 2 * i], 16) / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(0) + 0.7152 * ch(1) + 0.0722 * ch(2)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def on(bg: str) -> str:
    """Readable text colour for a solid background."""
    return "#111111" if contrast(bg, "#111111") >= contrast(bg, "#FFFFFF") else "#FFFFFF"


# ---------------------------------------------------------------- validation

def _pick(raw: dict, key: str, menu, default: str, notes: list[str], label: str) -> str:
    v = raw.get(key) if isinstance(raw, dict) else None
    if isinstance(v, str) and v in menu:
        return v
    if v is not None:
        notes.append(f"{label}: '{str(v)[:30]}' is not a known option; used '{default}'.")
    return default


def _text(v, limit: int) -> str:
    return " ".join(v.split())[:limit] if isinstance(v, str) else ""


def sanitize(raw, fallback_cta: tuple[str, str], label: str = "Design") -> tuple[dict, list[str]]:
    """Turn the planner's `design` object into a complete, safe design. Never raises."""
    notes: list[str] = []
    raw = raw if isinstance(raw, dict) else {}
    if not raw:
        notes.append(f"{label}: the plan had no design; used the default look.")

    def sub(key: str) -> dict:
        v = raw.get(key)
        return v if isinstance(v, dict) else {}

    pal_raw = sub("palette")
    pal = {k: (pal_raw[k].upper() if isinstance(pal_raw.get(k), str) and HEX.match(pal_raw[k]) else d)
           for k, d in DEFAULT_PALETTE.items()}
    if contrast(pal["paper"], pal["ink"]) < 7:        # captions are paper text outlined in ink; they must read
        pal["paper"], pal["ink"] = DEFAULT_PALETTE["paper"], DEFAULT_PALETTE["ink"]
        notes.append(f"{label}: palette text and outline colours were too close; used white on black.")

    cap, head, call, end = sub("captions"), sub("headline"), sub("callouts"), sub("end_screen")
    style = _pick(cap, "style", CAPTION_STYLES, "bold_outline", notes, f"{label} caption style")
    try:
        wpg = int(cap.get("words_per_group", 3))
    except (TypeError, ValueError):
        wpg = 3
    wpg = 1 if style == "impact_single" else min(4, max(1, wpg))
    d = {
        "mood": _text(raw.get("mood"), 80),
        "palette": pal,
        "pacing": _pick(raw, "pacing", PACING, "natural", notes, f"{label} pacing"),
        "captions": {
            "style": style,
            "position": _pick(cap, "position", CAPTION_POSITIONS, "low", notes, f"{label} caption position"),
            "case": _pick(cap, "case", CAPTION_CASES, "upper", notes, f"{label} caption case"),
            "size": _pick(cap, "size", CAPTION_SIZES, "medium", notes, f"{label} caption size"),
            "words_per_group": wpg,
        },
        "headline": {"style": _pick(head, "style", HEADLINE_STYLES, "white_box", notes, f"{label} headline style"),
                     "position": _pick(head, "position", HEADLINE_POSITIONS, "top", notes,
                                       f"{label} headline position")},
        "headline_motion": _pick(raw, "headline_motion", MOTIONS, "fade", notes, f"{label} headline motion"),
        "callouts": {"style": _pick(call, "style", CALLOUT_STYLES, "marker_box", notes, f"{label} callout style")},
        "callout_motion": _pick(raw, "callout_motion", MOTIONS, "pop", notes, f"{label} callout motion"),
        "punch_ins": [],
    }
    for p in (raw.get("punch_ins") if isinstance(raw.get("punch_ins"), list) else [])[:MAX_PUNCH_INS]:
        try:
            z = min(1.25, max(1.04, float(p["zoom"])))
            if isinstance(p["from"], int) and isinstance(p["to"], int) and 0 <= p["from"] <= p["to"]:
                d["punch_ins"].append({"from": p["from"], "to": p["to"], "zoom": round(z, 3)})
        except (KeyError, TypeError, ValueError):
            notes.append(f"{label}: dropped a malformed punch-in.")
    line, button = _text(end.get("line"), 70), _text(end.get("button"), 28)
    if not line or not button:
        line, button = fallback_cta
        notes.append(f"{label}: the plan had no end-screen text; used the brand's default call to action.")
    try:
        secs = float(end.get("seconds", 3.0))
    except (TypeError, ValueError):
        secs = 3.0
    d["end_screen"] = {"layout": _pick(end, "layout", END_LAYOUTS, "centered_card", notes, f"{label} end layout"),
                       "motion": _pick(end, "motion", MOTIONS, "pop", notes, f"{label} end motion"),
                       "line": line, "button": button, "seconds": round(min(END_SECONDS[1], max(END_SECONDS[0], secs)), 2)}
    return d, notes


def signature(d: dict) -> str:
    """A short fingerprint of a look, used to keep ads (and later jobs) from repeating each other."""
    return "/".join([d["captions"]["style"], d["headline"]["style"], d["callouts"]["style"],
                     d["end_screen"]["layout"], d["palette"]["accent"]])


def pace(d: dict) -> tuple[float, float]:
    return PACING_VALUES[d["pacing"]]


def caption_limits(d: dict) -> tuple[int, int]:
    """(max words per caption group, max characters per group)."""
    c = d["captions"]
    px, chars = CAPTION_SIZES[c["size"]]
    return c["words_per_group"], (9 if c["style"] == "impact_single" else chars)


# ---------------------------------------------------------------- CSS

def css(d: dict) -> str:
    p, c = d["palette"], d["captions"]
    px = {"impact_single": 150}.get(c["style"], CAPTION_SIZES[c["size"]][0])
    case = {"upper": "uppercase", "lower": "lowercase", "sentence": "none"}[c["case"]]
    on_acc, on_acc2 = on(p["accent"]), on(p["accent2"])
    out = [f":root {{ --accent: {p['accent']}; --accent2: {p['accent2']}; --ink: {p['ink']}; --paper: {p['paper']}; "
           f"--on-accent: {on_acc}; --on-accent2: {on_acc2}; }}",
           f".cap {{ top: {CAPTION_POSITIONS[c['position']]}px; font-size: {px}px; text-transform: {case}; }}"]
    out.append({
        "bold_outline": ".w .b, .w .y { -webkit-text-stroke: 12px var(--ink); paint-order: stroke fill; "
                        "text-shadow: 0 6px 18px rgba(0,0,0,.5); }",
        "clean_shadow": ".cap { font-weight: 800; } .w .b, .w .y { text-shadow: 0 3px 14px rgba(0,0,0,.75); }",
        "pill_all": ".cap { left: 70px; right: 70px; background: color-mix(in srgb, var(--ink) 80%, transparent); "
                    "border-radius: 34px; padding: 22px 18px; } .w .b, .w .y { text-shadow: none; }",
        "impact_single": ".w .b, .w .y { -webkit-text-stroke: 16px var(--ink); paint-order: stroke fill; "
                         "text-shadow: 0 8px 22px rgba(0,0,0,.55); } .cap { font-weight: 900; }",
        "bar_karaoke": ".cap { left: 0; right: 0; background: color-mix(in srgb, var(--ink) 84%, transparent); "
                       "padding: 26px 0; } .w .b, .w .y { text-shadow: none; }",
    }[c["style"]])
    out.append({
        "white_box": "#headline .box { background: var(--paper); color: var(--ink); font-weight: 800; font-size: 50px; "
                     "padding: 22px 30px; border-radius: 18px; box-shadow: 0 8px 28px rgba(0,0,0,.35); }",
        "ribbon": "#headline .box { background: var(--accent); color: var(--on-accent); font-weight: 900; "
                  "font-size: 52px; text-transform: uppercase; padding: 20px 34px; border-radius: 8px; "
                  "transform: rotate(-2deg); box-shadow: 0 8px 24px rgba(0,0,0,.4); }",
        "big_stack": "#headline .box { color: var(--paper); font-weight: 900; font-size: 92px; line-height: 1.02; "
                     "text-transform: uppercase; -webkit-text-stroke: 10px var(--ink); paint-order: stroke fill; "
                     "text-shadow: 0 8px 22px rgba(0,0,0,.5); }",
        "underline": "#headline .box { color: var(--paper); font-weight: 900; font-size: 58px; "
                     "text-shadow: 0 4px 16px rgba(0,0,0,.8); border-bottom: 12px solid var(--accent); "
                     "padding: 6px 6px 12px; }",
        "tag": "#headline { justify-content: flex-start; } #headline .box { background: var(--ink); color: var(--paper); "
               "font-weight: 800; font-size: 40px; text-transform: uppercase; letter-spacing: .06em; "
               "padding: 16px 26px; border-left: 16px solid var(--accent); text-align: left; }",
        "none": "#headline { display: none; }",
    }[d["headline"]["style"]])
    out.append(f"#headline {{ top: {HEADLINE_POSITIONS[d['headline']['position']]}px; }}")
    out.append({
        "marker_box": ".co-box { font-family: 'Permanent Marker', cursive; font-size: 56px; line-height: 1.1; "
                      "color: var(--ink); background: color-mix(in srgb, var(--paper) 94%, transparent); "
                      "padding: 14px 28px; border: 6px solid var(--accent2); "
                      "border-radius: 255px 18px 225px 18px / 18px 225px 18px 255px; box-shadow: 0 8px 24px "
                      "rgba(0,0,0,.35); } .co .arrow path { fill: none; stroke: var(--accent2); stroke-width: 10; "
                      "stroke-linecap: round; stroke-linejoin: round; }",
        "sticky_note": ".co-box { font-family: 'Permanent Marker', cursive; font-size: 52px; line-height: 1.12; "
                       "color: var(--on-accent); background: var(--accent); padding: 30px 34px; "
                       "box-shadow: 0 14px 30px rgba(0,0,0,.4); }",
        "pill_label": ".co-box { font-weight: 900; font-size: 48px; text-transform: uppercase; letter-spacing: .02em; "
                      "color: var(--on-accent2); background: var(--accent2); padding: 16px 38px; border-radius: 999px; "
                      "box-shadow: 0 8px 22px rgba(0,0,0,.4); }",
        "scribble": ".co-box { font-weight: 900; font-size: 60px; line-height: 1.08; color: var(--paper); "
                    "-webkit-text-stroke: 9px var(--ink); paint-order: stroke fill; "
                    "text-shadow: 0 6px 18px rgba(0,0,0,.5); } .co .scr path { fill: none; stroke: var(--accent); "
                    "stroke-width: 12; stroke-linecap: round; }",
        "banner": ".co { left: 0 !important; right: 0 !important; width: auto !important; } "
                  ".co-box { width: 100%; font-weight: 900; font-size: 54px; text-transform: uppercase; "
                  "color: var(--paper); background: color-mix(in srgb, var(--ink) 86%, transparent); "
                  "padding: 24px 40px; border-top: 8px solid var(--accent); border-bottom: 8px solid var(--accent); }",
    }[d["callouts"]["style"]])
    out.append({
        "centered_card": "#cta { background: rgba(0,0,0,.66); justify-content: center; gap: 44px; padding: 0 90px 260px; } "
                         "#cta .line { color: var(--paper); font-weight: 800; font-size: 68px; } "
                         "#cta .pill { background: var(--accent); color: var(--on-accent); font-weight: 900; "
                         "font-size: 60px; padding: 20px 44px; border-radius: 999px; }",
        "bottom_sheet": "#cta { background: transparent; justify-content: flex-end; padding: 0 50px 320px; } "
                        "#cta .sheet { width: 100%; background: color-mix(in srgb, var(--ink) 93%, transparent); "
                        "border-radius: 40px; padding: 44px 40px; display: flex; flex-direction: column; "
                        "align-items: center; gap: 30px; box-shadow: 0 -10px 40px rgba(0,0,0,.45); } "
                        "#cta .line { color: var(--paper); font-weight: 800; font-size: 56px; } "
                        "#cta .pill { background: var(--accent); color: var(--on-accent); font-weight: 900; "
                        "font-size: 50px; padding: 18px 40px; border-radius: 22px; }",
        "big_question": "#cta { background: rgba(0,0,0,.55); justify-content: center; gap: 36px; padding: 0 70px 260px; } "
                        "#cta .line { color: var(--paper); font-weight: 900; font-size: 96px; line-height: 1.04; "
                        "text-transform: uppercase; border-bottom: 14px solid var(--accent); padding-bottom: 18px; } "
                        "#cta .pill { color: var(--accent); font-weight: 800; font-size: 46px; }",
        "split_bar": "#cta { background: rgba(0,0,0,.35); justify-content: space-between; padding: 330px 0 330px; } "
                     "#cta .bar { width: 100%; background: var(--accent); padding: 34px 70px; } "
                     "#cta .line { color: var(--on-accent); font-weight: 900; font-size: 60px; } "
                     "#cta .pill { background: var(--ink); color: var(--paper); font-weight: 900; font-size: 54px; "
                     "padding: 22px 52px; border-radius: 14px; border: 6px solid var(--accent); }",
        "minimal_line": "#cta { background: transparent; justify-content: flex-end; gap: 14px; padding: 0 80px 330px; } "
                        "#cta .line { color: var(--paper); font-weight: 800; font-size: 50px; "
                        "text-shadow: 0 4px 18px rgba(0,0,0,.85); } "
                        "#cta .pill { color: var(--accent); font-weight: 900; font-size: 44px; "
                        "text-shadow: 0 4px 18px rgba(0,0,0,.85); }",
    }[d["end_screen"]["layout"]])
    return "\n      ".join(out)


# ---------------------------------------------------------------- markup

def _esc_lines(text: str) -> str:
    return "<br>".join(html.escape(x) for x in text.split("\n"))


def headline_html(d: dict, text: str) -> str:
    return f'<div class="box">{_esc_lines(text)}</div>'


def callout_html(d: dict, i: int, text: str, side: str, top: int, start: float, dur: float) -> str:
    style = d["callouts"]["style"]
    body = _esc_lines(text)
    pos = {"left": "left: 60px;", "right": "right: 60px;", "center": "left: 230px;"}[side]
    extra = ""
    if style == "marker_box":
        margin = {"left": "margin-left: 40px", "right": "margin-right: 200px", "center": "margin-right: 0"}[side]
        extra = ('<svg class="arrow" viewBox="0 0 120 140" style="' + margin + '"><path d="M70 132 C 62 96, 58 60, 44 18" />'
                 '<path d="M22 40 C 32 30, 38 22, 44 16 C 52 26, 60 34, 70 40" /></svg>')
    elif style == "scribble":
        extra = ('<svg class="scr" viewBox="0 0 400 30" width="420" height="32"><path d="M6 20 C 70 4, 130 28, 200 12 '
                 'S 330 24, 394 10" /></svg>')
    pre = extra if style == "marker_box" else ""
    post = extra if style == "scribble" else ""
    return (f'<div class="co clip" id="co{i}" style="top: {top}px; {pos}" data-start="{start:.3f}" '
            f'data-duration="{dur:.3f}" data-track-index="2">{pre}<div class="co-box">{body}</div>{post}</div>')


def end_html(d: dict) -> str:
    e = d["end_screen"]
    line, button = html.escape(e["line"]), html.escape(e["button"])
    lay = e["layout"]
    if lay == "bottom_sheet":
        return f'<div class="sheet"><div class="line">{line}</div><div class="pill">{button}</div></div>'
    if lay == "split_bar":
        return f'<div class="bar"><div class="line">{line}</div></div><div class="pill">{button}</div>'
    return f'<div class="line">{line}</div><div class="pill">{button}</div>'


# ---------------------------------------------------------------- motion (GSAP)

def motion_in(sel: str, motion: str, at: float, side: str = "center") -> str:
    sign = -1 if side == "left" else 1
    return {
        "pop": f'tl.fromTo("{sel}", {{opacity: 0, scale: 0.55}}, {{opacity: 1, scale: 1, duration: 0.35, '
               f'ease: "back.out(2)"}}, {at:.3f});',
        "slide": f'tl.fromTo("{sel}", {{opacity: 0, x: {sign * -160}}}, {{opacity: 1, x: 0, duration: 0.35, '
                 f'ease: "power3.out"}}, {at:.3f});',
        "drop": f'tl.fromTo("{sel}", {{opacity: 0, y: -90}}, {{opacity: 1, y: 0, duration: 0.45, '
                f'ease: "bounce.out"}}, {at:.3f});',
        "wipe": f'tl.fromTo("{sel}", {{opacity: 1, clipPath: "inset(0 100% 0 0)"}}, {{clipPath: "inset(0 0% 0 0)", '
                f'duration: 0.4, ease: "power2.out"}}, {at:.3f});',
        "fade": f'tl.fromTo("{sel}", {{opacity: 0}}, {{opacity: 1, duration: 0.3, ease: "power1.out"}}, {at:.3f});',
    }[motion]


def free_bands(d: dict) -> list[int]:
    """Top positions for callouts that stay clear of the headline and the captions."""
    h = d["headline"]
    head = None if h["style"] == "none" else (HEADLINE_POSITIONS[h["position"]],
                                              HEADLINE_POSITIONS[h["position"]] + (360 if h["style"] == "big_stack" else 230))
    c = d["captions"]
    cap = (CAPTION_POSITIONS[c["position"]] - 20, CAPTION_POSITIONS[c["position"]] + 300)
    out = []
    for y in CALLOUT_BANDS:
        box = (y, y + 250)
        if any(r and box[0] < r[1] and r[0] < box[1] for r in (head, cap)):
            continue
        out.append(y)
    return out or [CALLOUT_BANDS[0]]


# ---------------------------------------------------------------- notes and memory of past looks

def describe(d: dict) -> str:
    e, c = d["end_screen"], d["captions"]
    return (f"{d['pacing']} pace; captions {c['style']} ({c['position']}, {c['case']}); headline "
            f"{d['headline']['style']}; callouts {d['callouts']['style']}; end screen {e['layout']} "
            f"\"{e['line']}\" / \"{e['button']}\" for {e['seconds']}s; accent {d['palette']['accent']}; "
            f"{len(d['punch_ins'])} punch-in(s).")


def recent_looks(path, limit: int = 8) -> str:
    """The last few looks, as a list for the planner prompt so a new job does not repeat them."""
    try:
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()[-limit:] if x.strip()]
    except (OSError, ValueError):
        return ""
    return "\n".join(f"- {r.get('sig', '')}" + (f" ({r['mood']})" if r.get("mood") else "") for r in rows
                     if isinstance(r, dict))


def remember(path, designs: list[dict], keep: int = 40) -> None:
    """Append this plan's looks to the history. A history that cannot be written is not worth failing a job for."""
    try:
        old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        new = [json.dumps({"sig": signature(d), "mood": d["mood"]}) for d in designs]
        path.write_text("\n".join((old + new)[-keep:]) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- strict schema for the planner's reply

def _enum(names) -> dict:
    return {"type": "string", "enum": list(names)}


def plan_schema() -> dict:
    """The JSON the planner must return, as a schema the provider can enforce. Choices are enums built from the same
    menus the validator uses. Word positions are indexes into the transcript: the model never writes a timestamp."""
    integer, string = {"type": "integer"}, {"type": "string"}
    span = {"type": "object", "properties": {"from": integer, "to": integer}, "required": ["from", "to"]}
    palette = {"type": "object", "properties": {k: string for k in DEFAULT_PALETTE}, "required": list(DEFAULT_PALETTE)}
    look = {"type": "object", "properties": {
        "mood": string, "palette": palette, "pacing": _enum(PACING),
        "captions": {"type": "object", "properties": {
            "style": _enum(CAPTION_STYLES), "position": _enum(CAPTION_POSITIONS), "case": _enum(CAPTION_CASES),
            "size": _enum(CAPTION_SIZES), "words_per_group": integer},
            "required": ["style", "position", "case", "size", "words_per_group"]},
        "headline": {"type": "object", "properties": {"style": _enum(HEADLINE_STYLES),
                                                       "position": _enum(HEADLINE_POSITIONS)},
                     "required": ["style", "position"]},
        "headline_motion": _enum(MOTIONS),
        "callouts": {"type": "object", "properties": {"style": _enum(CALLOUT_STYLES)}, "required": ["style"]},
        "callout_motion": _enum(MOTIONS),
        "punch_ins": {"type": "array", "items": {"type": "object", "properties": {
            "from": integer, "to": integer, "zoom": {"type": "number"}}, "required": ["from", "to", "zoom"]}},
        "end_screen": {"type": "object", "properties": {
            "layout": _enum(END_LAYOUTS), "motion": _enum(MOTIONS), "line": string, "button": string,
            "seconds": {"type": "number"}}, "required": ["layout", "motion", "line", "button", "seconds"]}},
        "required": ["mood", "palette", "pacing", "captions", "headline", "headline_motion", "callouts",
                     "callout_motion", "punch_ins", "end_screen"]}
    callout = {"type": "object", "properties": {"from": integer, "to": integer, "text": string,
                                                "side": _enum(CALLOUT_SIDES)},
               "required": ["from", "to", "text", "side"]}
    ad = {"type": "object", "properties": {
        "name": string, "funnel_stage": _enum(("cold", "problem-solution", "retargeting")), "angle": string,
        "headline": string, "segments": {"type": "array", "items": span},
        "callouts": {"type": "array", "items": callout}, "primary_text": string, "design": look},
        "required": ["name", "funnel_stage", "angle", "headline", "segments", "callouts", "primary_text", "design"]}
    fix = {"type": "object", "properties": {"from": integer, "to": integer, "text": string},
           "required": ["from", "to", "text"]}
    claim = {"type": "object", "properties": {"ad": string, "claim": string, "reason": string},
             "required": ["ad", "claim", "reason"]}
    return {"type": "object", "properties": {
        "summary": string, "response_to_request": string, "caption_fixes": {"type": "array", "items": fix},
        "highlight_words": {"type": "array", "items": integer}, "ads": {"type": "array", "items": ad},
        "claims_to_review": {"type": "array", "items": claim}},
        "required": ["summary", "response_to_request", "caption_fixes", "highlight_words", "ads",
                     "claims_to_review"]}
