"""The design kit: every look an ad can have, as a closed list of options Gemini chooses from and our code builds.

Gemini writes a short design brief per ad (what it saw in the footage, then the choices and why); `validate_design` checks it
and turns it into a design; `resolve` turns a design into the CSS and timeline fragments `ad_cutter.compose` pastes into the
composition template. Nothing Gemini writes is ever put into the page as code: it picks names from the lists below, colours
as six-digit hex (checked for legibility), and card text that must be words the speaker actually said.

`CLASSIC` is the look every ad had before this module existed. Choosing nothing, or failing any check, gives exactly that
look (tests/fixtures/classic_compose.html proves the output is unchanged). Fonts are the SIL OFL files in template/vendor.
"""
from __future__ import annotations

import html
import re
import string
from collections.abc import Callable

# ---------------------------------------------------------------- the lists Gemini chooses from

FONTS = {   # id: (CSS family, weight for heavy text, weight for medium text, what it feels like)
    "montserrat": ('Montserrat, "Arial Black", sans-serif', 900, 800, "friendly, bold, the house look"),
    "inter": ('Inter, "Arial Black", sans-serif', 900, 800, "clean, modern, neutral"),
    "anton": ('Anton, "Arial Black", sans-serif', 400, 400, "tall and loud, news-flash energy"),
    "sora": ("Sora, Arial, sans-serif", 700, 700, "techy, rounded, product-demo feel"),
    "space-grotesk": ('"Space Grotesk", Arial, sans-serif', 700, 700, "quirky and technical, fintech"),
    "teko": ('Teko, "Arial Black", sans-serif', 700, 700, "condensed, sporty, high energy"),
    "baloo-2": ('"Baloo 2", Arial, sans-serif', 700, 700, "warm and approachable"),
    "rajdhani": ("Rajdhani, Arial, sans-serif", 700, 700, "squared, data-terminal feel"),
}
FONT_FILES = {   # the files in template/vendor behind each font: (CSS family, weight, file)
    "inter": [("Inter", 700, "inter-latin-700-normal"), ("Inter", 800, "inter-latin-800-normal"), ("Inter", 900, "inter-latin-900-normal")],
    "anton": [("Anton", 400, "anton-latin-400-normal")], "sora": [("Sora", 700, "sora-latin-700-normal")],
    "space-grotesk": [("Space Grotesk", 700, "space-grotesk-latin-700-normal")], "teko": [("Teko", 700, "teko-latin-700-normal")],
    "baloo-2": [("Baloo 2", 700, "baloo-2-latin-700-normal")], "rajdhani": [("Rajdhani", 700, "rajdhani-latin-700-normal")],
}


def font_faces(font: str) -> str:
    """Inline @font-face rules for a font. HyperFrames' static check only counts rules written inside the page, and the original
    font (Montserrat) is on its built-in list, so the original look needs none and stays byte-for-byte as it was."""
    files = FONT_FILES.get(font, [])
    if files:                                  # the callout font is declared too: the check counts every family a page uses
        files = files + [("Permanent Marker", 400, "permanent-marker-latin")]
    return "".join(f"@font-face {{ font-family: '{fam}'; font-style: normal; font-weight: {w}; font-display: block; "
                   f"src: url(vendor/{f}.woff2) format('woff2'); }}\n      " for fam, w, f in files)


MOTIONS = {   # pop = how much a spoken word jumps, ease = how it lands, card/callout = how overlays arrive
    "calm": {"pop": 1.08, "pop_s": 0.18, "ease": "power2.out", "card_s": 0.45, "card_ease": "power2.out", "callout_ease": "back.out(1.2)",
             "head_s": 0.45},
    "confident": {"pop": 1.15, "pop_s": 0.14, "ease": "power2.out", "card_s": 0.35, "card_ease": "back.out(1.6)", "callout_ease": "back.out(2)",
                  "head_s": 0.3},
    "hype": {"pop": 1.3, "pop_s": 0.12, "ease": "back.out(3)", "card_s": 0.3, "card_ease": "back.out(2.6)", "callout_ease": "back.out(3)",
             "head_s": 0.25},
}
CAPTION_STYLES = {   # highlight: "pill" boxes the key words, "color" tints the spoken word; upper: capital letters
    "pop_pill": {"size": 80, "top": 1075, "stroke": 12, "highlight": "pill", "upper": True,
                 "feel": "yellow pill on the key words, a pop on every word (the original look)"},
    "karaoke": {"size": 84, "top": 1075, "stroke": 14, "highlight": "color", "upper": True,
                "feel": "the spoken word lights up in the accent colour, no boxes"},
    "bar": {"size": 66, "top": 1090, "stroke": 0, "highlight": "color", "upper": True,
            "feel": "captions sit on a dark band across the frame, calm and readable"},
    "outline_pop": {"size": 88, "top": 1075, "stroke": 12, "highlight": "color", "upper": True,
                    "feel": "white-outlined letters that fill with colour as they are spoken"},
    "clean": {"size": 62, "top": 1110, "stroke": 9, "highlight": "color", "upper": False,
              "feel": "sentence case, smaller and quieter, for calm explanations"},
}
HEADLINE_STYLES = {"plate": "white card with dark text (the original look)", "bar": "full-width band in the accent colour",
                   "outline": "big white outlined text with no box", "underline": "white text with an accent underline"}
CALLOUT_STYLES = {"marker": "hand-drawn arrow and marker label (the original look)", "tag": "clean dark label with an accent edge, no arrow"}
END_STYLES = {"classic": "dark screen with the message and a button (the original look)",
              "minimal": "the last frame stays visible with a gradient and the message at the bottom",
              "banner": "the last frame stays visible with the message on a coloured band"}
CARD_KINDS = {"stat": "a big number or short figure with a label (every word must be spoken)",
              "quote": "the speaker's own words, 3 to 14 of them, as a pull quote (taken from the transcript)",
              "lower_third": "a short topic tag, up to 4 words",
              "compare": "two short phrases side by side, written 'A|B', up to 3 words each",
              "kinetic": "one to three spoken words very large, for emphasis"}
SPEAKER_POSITIONS = ("upper", "middle", "lower")
CAPTION_Y = {"standard": 0, "high": -70, "low": 50}        # moves the caption band from its style's own place (pixels)
CALLOUT_ZONES = {"right_mid": ("right", 740), "right_high": ("right", 520), "right_low": ("right", 880),
                 "left_mid": ("left", 740), "left_high": ("left", 520), "left_low": ("left", 880)}
CARD_TOP = {"lower": 1330, "upper": 470}      # where cards sit; they avoid the captions (about 1075-1250) and Meta's bottom bar
MAX_CARDS, CARD_MIN_S, CARD_MAX_S, CARD_GAP_S = 4, 1.5, 4.0, 0.4
CLASSIC_PALETTE = {"accent": "#FFE11A", "plate": "#fff", "mark": "#E3261C"}
CLASSIC = {"motion": "confident", "font": "montserrat", "palette": dict(CLASSIC_PALETTE), "caption_style": "pop_pill",
           "headline_style": "plate", "callout_style": "marker", "end_style": "classic", "cards": [], "speaker_position": "middle",
           "caption_y": "standard", "callout_zone": "right_mid",
           "observations": "", "why": ""}
SIGNATURE_KEYS = ("caption_style", "headline_style", "callout_style", "end_style", "font", "accent")

# ---------------------------------------------------------------- colour checks

HEX = re.compile(r"#[0-9a-fA-F]{6}")


def _lin(v: float) -> float:
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def ink_for(color: str) -> str:
    """Dark or white text, whichever reads better on this colour (the classic look's own ink is #111)."""
    return "#111" if contrast(color, "#111111") >= contrast(color, "#ffffff") else "#fff"


def clean_palette(raw) -> tuple[dict, list[str]]:
    """The palette Gemini asked for, or the classic one with a reason. Accent and callout colours must stay readable on a dark
    outline (captions and arrows are drawn over video), and the card colour must carry dark or white text at 4.5:1."""
    notes: list[str] = []
    out = dict(CLASSIC_PALETTE)
    if not isinstance(raw, dict):
        return out, notes
    for key, need in (("accent", 4.5), ("plate", 4.5), ("mark", 3.0)):
        value = raw.get(key)
        if not (isinstance(value, str) and HEX.fullmatch(value.strip())):
            if value is not None:
                notes.append(f"palette {key} {str(value)[:20]!r} is not a six-digit colour; the original was kept")
            continue
        value = value.strip().upper()
        ok = (contrast(value, "#000000") >= need) if key in ("accent", "mark") else (max(contrast(value, "#111111"), contrast(value, "#ffffff")) >= need)
        if not ok:
            notes.append(f"palette {key} {value} is too dark to read on video; the original was kept")
            continue
        out[key] = value
    return out, notes


# ---------------------------------------------------------------- spoken-word checks for card text

SPELLED = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                                           "sixteen seventeen eighteen nineteen twenty".split())}
TOKEN = re.compile(r"[a-z0-9]+(?:[.'][a-z0-9]+)*")
CARD_CHARS = re.compile(r"[^A-Za-z0-9 .,'|$%\-]")      # anything else (symbols, emoji, other alphabets) cannot be checked against speech
CONNECTORS = {"vs", "and", "or", "the", "a", "to", "of", "in", "no"}     # allowed on a card even if not spoken


def tokens(text: str) -> list[str]:
    return TOKEN.findall(str(text or "").lower().replace(",", ""))


def spoken_tokens(words: list[str]) -> set[str]:
    out: set[str] = set()
    for w in words:
        for t in tokens(w):
            out.add(t)
            if t in SPELLED:
                out.add(SPELLED[t])
            if t.isdigit() and int(t) <= 20:
                out.add(next((k for k, v in SPELLED.items() if v == t), t))
    return out


def clean_text(text, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f<>{}`\\]+|\s+", " ", str(text or "")).strip()[:limit]


# ---------------------------------------------------------------- checking Gemini's brief

def choice(raw: dict, key: str, allowed, default: str, notes: list[str]) -> str:
    value = raw.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, str) and value in allowed:
        return value
    notes.append(f"{key} {str(value)[:30]!r} is not one of the options; the original was kept")
    return default


def validate_card(c, ad_words: list[tuple[int, str]], segments: list[dict], brand: set[str], spoken: set[str]) -> tuple[dict | None, str]:
    """A card Gemini asked for, cleaned, or (None, why not). `ad_words` are (transcript index, word) pairs of this ad."""
    if not isinstance(c, dict) or not isinstance(c.get("kind"), str) or c["kind"] not in CARD_KINDS:
        return None, "unknown card kind"
    a, b = c.get("from"), c.get("to")
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (a, b)) or a > b:
        return None, "no word range"
    if not any(s["from"] <= a and b <= s["to"] for s in segments):
        return None, "not inside the ad's segments"
    kind = c["kind"]
    if kind == "quote":
        quote = [w for i, w in ad_words if a <= i <= b]
        if not 3 <= len(quote) <= 14:
            return None, "a quote needs 3 to 14 spoken words"
        return {"kind": kind, "from": a, "to": b, "text": clean_text(" ".join(quote), 140), "label": ""}, ""
    text, label = clean_text(c.get("text"), 60), clean_text(c.get("label"), 40)
    if not text:
        return None, "no text"
    if CARD_CHARS.search(text) or CARD_CHARS.search(label) or (label and not tokens(label)):
        return None, "only plain letters, numbers and . , ' | $ % - are allowed"
    parts = text.split("|") if kind == "compare" else [text]
    if kind == "compare" and (len(parts) != 2 or not all(p.strip() for p in parts)):
        return None, "a comparison is written 'A|B'"
    limit = {"stat": 4, "lower_third": 4, "compare": 3, "kinetic": 3}[kind]
    for part in parts:
        if len(tokens(part)) > limit or not tokens(part):
            return None, f"too long for a {kind} card"
    allowed = spoken | CONNECTORS | brand
    for t in tokens(text) + tokens(label):
        if t not in allowed:
            return None, f"'{t}' was not said by the speaker"
    return {"kind": kind, "from": a, "to": b, "text": text, "label": label}, ""


def validate_design(raw, ad: dict, words: list[dict], brand_name: str = "") -> tuple[dict | None, list[str]]:
    """(design, notes) for one ad. None when the brief has nothing behind it (the ad then gets the classic look); otherwise every
    choice that was not valid falls back to the classic value and is reported."""
    notes: list[str] = []
    if not isinstance(raw, dict):
        return None, notes
    observations, why = clean_text(raw.get("observations"), 400), clean_text(raw.get("why"), 300)
    if len(observations) < 20 or len(why) < 20:
        return None, ["the design had no observation of the footage or no reason behind it, so the original look was used"]
    d = {"observations": observations, "why": why}
    d["motion"] = choice(raw, "motion", MOTIONS, CLASSIC["motion"], notes)
    d["font"] = choice(raw, "font", FONTS, CLASSIC["font"], notes)
    d["caption_style"] = choice(raw, "caption_style", CAPTION_STYLES, CLASSIC["caption_style"], notes)
    d["headline_style"] = choice(raw, "headline_style", HEADLINE_STYLES, CLASSIC["headline_style"], notes)
    d["callout_style"] = choice(raw, "callout_style", CALLOUT_STYLES, CLASSIC["callout_style"], notes)
    d["end_style"] = choice(raw, "end_style", END_STYLES, CLASSIC["end_style"], notes)
    d["speaker_position"] = choice(raw, "speaker_position", SPEAKER_POSITIONS, CLASSIC["speaker_position"], notes)
    d["caption_y"] = choice(raw, "caption_y", CAPTION_Y, CLASSIC["caption_y"], notes)
    d["callout_zone"] = choice(raw, "callout_zone", CALLOUT_ZONES, CLASSIC["callout_zone"], notes)
    d["palette"], pnotes = clean_palette(raw.get("palette"))
    notes += pnotes
    segments = [s for s in ad.get("segments", []) if isinstance(s, dict)]
    ad_words = [(i, words[i]["w"]) for s in segments for i in range(s["from"], s["to"] + 1)]
    spoken = spoken_tokens([w for _, w in ad_words])
    brand = set(tokens(brand_name))
    valid = []
    for c in (raw.get("cards") if isinstance(raw.get("cards"), list) else [])[:MAX_CARDS + 4]:
        card, why_not = validate_card(c, ad_words, segments, brand, spoken)
        if card is None:
            notes.append(f"a {str(c.get('kind') if isinstance(c, dict) else '?')[:20]} card was dropped: {why_not}")
        else:
            valid.append(card)
    cards, last_end = [], -1.0
    for card in sorted(valid, key=lambda c: (words[c["from"]]["s"], c["to"])):
        start, end = words[card["from"]]["s"], words[card["to"]]["e"]
        if cards and start < last_end + CARD_GAP_S:
            notes.append("a card was dropped: it would overlap the one before it")
            continue
        if len(cards) >= MAX_CARDS:
            notes.append("extra cards were dropped (at most four)")
            break
        cards.append(card)
        last_end = max(end, start + CARD_MIN_S)
    d["cards"] = cards
    return d, notes


def signature(design: dict | None) -> tuple:
    d = design or CLASSIC
    return tuple(d["palette"]["accent"] if k == "accent" else d[k] for k in SIGNATURE_KEYS)


def diversify(designs: list[dict | None]) -> list[str]:
    """Ads in one batch must not all look alike: when an ad matches an earlier one on all but one choice, its caption style moves
    to one the batch has not used. Changes the designs in place; returns notes."""
    notes: list[str] = []
    order = list(CAPTION_STYLES)
    for k, d in enumerate(designs):
        if d is None:
            continue
        for e in designs[:k]:
            same = sum(a == b for a, b in zip(signature(d), signature(e or CLASSIC)))
            if same >= len(SIGNATURE_KEYS) - 1:
                used = {(x or CLASSIC)["caption_style"] for x in designs[:k + 1]}
                free = [s for s in order if s not in used] or [s for s in order if s != d["caption_style"]]
                notes.append(f"Ad {k + 1}: looked too much like an earlier ad, so its captions changed from {d['caption_style']} to {free[0]}.")
                d["caption_style"] = free[0]
                break
    return notes


# ---------------------------------------------------------------- the CSS and timeline pieces

HEADLINE_CSS = string.Template('''/* Top hook text: just below Meta's top UI zone (~270 px) */
      #headline { position: absolute; top: 280px; left: 70px; right: 70px; display: flex; justify-content: center; }
      #headline .box { background: ${plate}; color: ${plate_ink}; font-weight: ${w_mid}; font-size: 50px; line-height: 1.16;
        letter-spacing: -0.01em; text-align: center; padding: 22px 30px; border-radius: 18px;
        box-shadow: 0 8px 28px rgba(0,0,0,.35); }''')
HEADLINE_EXTRA = {
    "plate": "",
    "bar": string.Template('''
      #headline { left: 0; right: 0; }
      #headline .box { background: ${accent}; color: ${accent_ink}; width: 100%; border-radius: 0; box-shadow: none; padding: 26px 70px; }'''),
    "outline": string.Template('''
      #headline .box { background: none; color: #fff; -webkit-text-stroke: 8px #000; paint-order: stroke fill; box-shadow: none;
        text-shadow: 0 6px 18px rgba(0,0,0,.55); padding: 0 10px; font-size: 58px; }'''),
    "underline": string.Template('''
      #headline .box { background: none; color: #fff; text-shadow: 0 4px 16px rgba(0,0,0,.7); box-shadow: none; border-radius: 0;
        border-bottom: 10px solid ${accent}; padding: 6px 6px 14px; font-size: 54px; }'''),
}
CAPTION_CSS = string.Template('''/* Word-by-word captions: lower-middle, above Meta's bottom UI zone (~1250 px) */
      .cap { position: absolute; left: 30px; right: 30px; top: ${cap_top}px; text-align: center; white-space: nowrap;
        font-weight: ${w_heavy}; font-size: ${cap_size}px; line-height: 1.1; }
      .w { position: relative; display: inline-block; margin: 0 20px; }
      .w .b, .w .y { color: #fff; -webkit-text-stroke: ${cap_stroke}px #000; paint-order: stroke fill;
        text-shadow: 0 6px 18px rgba(0,0,0,.5); }
      .w .y { position: absolute; left: 0; top: 0; color: ${accent}; opacity: 0; }
      .w .p { position: absolute; left: -10px; right: -10px; top: 2px; bottom: 2px; display: flex; align-items: center;
        justify-content: center; background: ${accent}; color: ${accent_ink}; border-radius: 12px; opacity: 0;
        box-shadow: 0 6px 18px rgba(0,0,0,.4); }''')
CAPTION_EXTRA = {
    "pop_pill": "", "karaoke": "",
    "bar": '''
      .cap { left: 0; right: 0; background: rgba(0,0,0,.66); padding: 20px 0; }
      .w .b, .w .y { -webkit-text-stroke: 0; text-shadow: none; }''',
    "outline_pop": '''
      .w .b { color: rgba(0,0,0,.72); -webkit-text-stroke: 6px #fff; text-shadow: 0 6px 18px rgba(0,0,0,.6); }''',
    "clean": "",
}
CALLOUT_CSS = string.Template('''/* Hand-drawn marker callouts, middle-right of the frame */
      .co { position: absolute; ${co_side}: 60px; top: ${co_top}px; width: 620px; display: flex; flex-direction: column;
        align-items: center; transform-origin: 50% 100%; }
      .co .arrow { width: 130px; height: 150px; margin-bottom: -8px; margin-right: 200px; }
      .co .arrow path { fill: none; stroke: ${mark}; stroke-width: 10; stroke-linecap: round; stroke-linejoin: round; }
      .co-box { font-family: "Permanent Marker", cursive; font-size: 56px; line-height: 1.1; color: #111;
        text-align: center; background: rgba(255,255,255,.94); padding: 14px 28px;
        border: 6px solid ${mark}; border-radius: 255px 18px 225px 18px / 18px 225px 18px 255px;
        box-shadow: 0 8px 24px rgba(0,0,0,.35); }''')
CALLOUT_EXTRA = {
    "marker": "",
    "tag": string.Template('''
      .co .arrow { display: none; }
      .co-box { font-family: ${font_family}; font-weight: ${w_heavy}; font-size: 50px; color: #fff; text-align: left; background: rgba(0,0,0,.74);
        border: 0; border-left: 14px solid ${mark}; border-radius: 12px; padding: 16px 28px; }'''),
}
CARD_CSS = string.Template('''
      .cd { position: absolute; left: 60px; right: 60px; top: ${card_top}px; display: flex; align-items: center; justify-content: center;
        text-align: center; font-weight: ${w_heavy}; transform-origin: 50% 50%; }
      .cd.stat { flex-direction: column; }
      .cd .n { font-size: 150px; line-height: 1; color: ${accent}; -webkit-text-stroke: 8px #000; paint-order: stroke fill;
        text-shadow: 0 8px 24px rgba(0,0,0,.5); }
      .cd .l { font-size: 52px; line-height: 1.1; color: #fff; margin-top: 6px; text-shadow: 0 4px 14px rgba(0,0,0,.8); }
      .cd .q { background: ${plate}; color: ${plate_ink}; font-size: 48px; line-height: 1.2; font-weight: ${w_mid}; padding: 26px 34px;
        border-radius: 22px; border-left: 14px solid ${accent}; box-shadow: 0 8px 28px rgba(0,0,0,.35); }
      .cd .t { display: inline-block; background: ${accent}; color: ${accent_ink}; font-size: 50px; padding: 16px 34px; border-radius: 12px;
        box-shadow: 0 8px 24px rgba(0,0,0,.35); }
      .cd.cmp { gap: 22px; }
      .cd .s { flex: 1; font-size: 46px; line-height: 1.1; padding: 24px 20px; border-radius: 20px; background: ${plate}; color: ${plate_ink};
        box-shadow: 0 8px 24px rgba(0,0,0,.35); }
      .cd .s.r { background: ${accent}; color: ${accent_ink}; }
      .cd .vs { font-size: 40px; color: #fff; text-shadow: 0 4px 14px rgba(0,0,0,.8); }
      .cd .k { font-size: 130px; line-height: 1.05; color: #fff; -webkit-text-stroke: 12px #000; paint-order: stroke fill; text-transform: uppercase;
        text-shadow: 0 8px 24px rgba(0,0,0,.5); }''')
CTA_CSS = string.Template('''/* CTA end card over the held last frame */
      #cta { position: absolute; inset: 0; background: rgba(0,0,0,.66); display: flex; flex-direction: column;
        align-items: center; justify-content: center; gap: 44px; padding: 0 90px 260px; }
      #cta .line { color: #fff; font-weight: ${w_mid}; font-size: 68px; line-height: 1.15; text-align: center; }
      #cta .pill { background: ${accent}; color: ${accent_ink}; font-weight: ${w_heavy}; font-size: 60px; padding: 20px 44px; border-radius: 999px; }''')
CTA_EXTRA = {
    "classic": "",
    "minimal": '''
      #cta { background: linear-gradient(to top, rgba(0,0,0,.88) 0%, rgba(0,0,0,0) 72%); justify-content: flex-end; padding: 0 90px 300px; }
      #cta .line { font-size: 60px; text-shadow: 0 4px 16px rgba(0,0,0,.7); }''',
    "banner": string.Template('''
      #cta { background: rgba(0,0,0,.4); justify-content: flex-end; gap: 0; padding: 0 0 280px; }
      #cta .line { background: ${plate}; color: ${plate_ink}; width: 100%; padding: 38px 90px; font-size: 62px; }
      #cta .pill { margin-top: 36px; }'''),
}


def _sub(part, v: dict) -> str:
    return part.substitute(v) if isinstance(part, string.Template) else part


def resolve(design: dict | None, with_cards: bool = False) -> dict:
    """Everything `compose` needs for this design: CSS blocks, motion numbers, and which highlight and case to use. Card styles are
    included only when cards will be shown."""
    d = {**CLASSIC, **(design or {})}
    family, w_heavy, w_mid, _ = FONTS[d["font"]]
    pal = d["palette"]
    cap = CAPTION_STYLES[d["caption_style"]]
    v = {"font_family": family, "w_heavy": w_heavy, "w_mid": w_mid, "accent": pal["accent"], "accent_ink": ink_for(pal["accent"]),
         "plate": pal["plate"], "plate_ink": ink_for(pal["plate"]), "mark": pal["mark"], "cap_top": cap["top"] + CAPTION_Y[d["caption_y"]], "cap_size": cap["size"],
         "co_side": CALLOUT_ZONES[d["callout_zone"]][0], "co_top": CALLOUT_ZONES[d["callout_zone"]][1],
         "cap_stroke": cap["stroke"], "card_top": CARD_TOP["upper" if d["speaker_position"] == "lower" else "lower"]}
    m = MOTIONS[d["motion"]]
    css_callout = CALLOUT_CSS.substitute(v) + _sub(CALLOUT_EXTRA[d["callout_style"]], v)
    if with_cards:
        css_callout += CARD_CSS.substitute(v)
    return {
        "design": d, "motion": m, "cap": cap, "v": v, "font_family": family,
        "css_headline": font_faces(d["font"]) + HEADLINE_CSS.substitute(v) + _sub(HEADLINE_EXTRA[d["headline_style"]], v),
        "css_caption": CAPTION_CSS.substitute(v) + CAPTION_EXTRA[d["caption_style"]],
        "css_callout": css_callout,
        "css_cta": CTA_CSS.substitute(v) + _sub(CTA_EXTRA[d["end_style"]], v),
    }


def headline_anim(r: dict) -> str:
    return f'tl.fromTo("#headline", {{opacity: 0, y: -20}}, {{opacity: 1, y: 0, duration: {r["motion"]["head_s"]:g}, ease: "power2.out"}}, 0);'


def cta_anim(r: dict, cta_start: str, pill_at: str) -> str:
    return (f'tl.fromTo("#cta", {{opacity: 0}}, {{opacity: 1, duration: 0.3}}, {cta_start});\n'
            f'      tl.fromTo("#cta .pill", {{scale: 0.7}}, {{scale: 1, duration: 0.35, ease: "back.out(2.5)"}}, {pill_at});')


# ---------------------------------------------------------------- the elements

def caption_groups(words: list[dict]) -> list[list[dict]]:
    """1-3 word caption groups, broken on pauses and length."""
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or w["s"] - cur[-1]["e"] > 0.35 or sum(len(x["w"]) + 1 for x in cur) + len(w["w"]) > 16):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build_captions(words: list[dict], body_len: float, r: dict) -> tuple[list[str], list[str]]:
    """(HTML elements, timeline lines) for the word-by-word captions of this style."""
    els, js = [], []
    cap, m = r["cap"], r["motion"]
    groups = caption_groups(words)
    for gi, g in enumerate(groups):
        gs = g[0]["s"]
        ge = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else body_len
        ge = max(gs + 0.1, min(ge, g[-1]["e"] + 0.5, body_len))
        spans = []
        for wi, w in enumerate(g):
            wid = f"g{gi}w{wi}"
            t = html.escape(w["w"].upper() if cap["upper"] else w["w"])
            layer = "p" if (w["key"] and cap["highlight"] == "pill") else "y"
            spans.append(f'<span class="w" id="{wid}"><span class="b" data-layout-allow-overlap>{t}</span>'
                         f'<span class="{layer}" data-layout-allow-overlap>{t}</span></span>')
            js.append(f'tl.set("#{wid} .{layer}", {{opacity: 1}}, {w["s"]:.3f});')
            if not w["key"]:
                js.append(f'tl.set("#{wid} .y", {{opacity: 0}}, {w["e"]:.3f});')
            js.append(f'tl.fromTo("#{wid}", {{scale: {m["pop"]:g}}}, {{scale: 1, duration: {m["pop_s"]:g}, ease: "{m["ease"]}"}}, '
                      f'{w["s"]:.3f});')
        els.append(f'<div class="cap clip" id="g{gi}" data-start="{gs:.3f}" data-duration="{ge - gs:.3f}" '
                   f'data-track-index="3">{"".join(spans)}</div>')
        js.append(f'tl.fromTo("#g{gi}", {{opacity: 0, y: 14}}, {{opacity: 1, y: 0, duration: 0.1}}, {gs:.3f});')
    return els, js


def build_callouts(callouts: list[tuple[float, float, str]], r: dict) -> tuple[list[str], list[str]]:
    els, js = [], []
    ease = r["motion"]["callout_ease"]
    for ci, (a, b, text) in enumerate(callouts):
        body = "<br>".join(html.escape(x) for x in text.split("\n"))
        els.append(f'<div class="co clip" id="co{ci}" data-start="{a:.3f}" data-duration="{b - a:.3f}" '
                   f'data-track-index="2"><svg class="arrow" viewBox="0 0 120 140">'
                   f'<path d="M70 132 C 62 96, 58 60, 44 18" /><path d="M22 40 C 32 30, 38 22, 44 16 C 52 26, 60 34, '
                   f'70 40" /></svg><div class="co-box">{body}</div></div>')
        js.append(f'tl.fromTo("#co{ci}", {{opacity: 0, scale: 0.55, rotation: -10}}, '
                  f'{{opacity: 1, scale: 1, rotation: -4, duration: 0.35, ease: "{ease}"}}, {a:.3f});')
        js.append(f'tl.to("#co{ci}", {{opacity: 0, duration: 0.2}}, {max(a, b - 0.2):.3f});')
    return els, js


def card_html(card: dict) -> str:
    e = html.escape
    kind = card["kind"]
    if kind == "stat":
        return f'<div class="cd stat"><div class="n">{e(card["text"])}</div>' + (f'<div class="l">{e(card["label"])}</div>' if card["label"] else "") + "</div>"
    if kind == "quote":
        return f'<div class="cd"><div class="q">“{e(card["text"])}”</div></div>'
    if kind == "lower_third":
        return f'<div class="cd"><div class="t">{e(card["text"])}</div></div>'
    if kind == "compare":
        a, b = (p.strip() for p in card["text"].split("|"))
        return f'<div class="cd cmp"><div class="s">{e(a)}</div><div class="vs">vs</div><div class="s r">{e(b)}</div></div>'
    return f'<div class="cd"><div class="k">{e(card["text"])}</div></div>'


def build_cards(cards: list[tuple[float, float, dict]], r: dict) -> tuple[list[str], list[str]]:
    """(HTML, timeline) for cards placed in time as (start, end, card), clamped to 1.5-4 s."""
    els, js = [], []
    m = r["motion"]
    for ci, (a, b, card) in enumerate(cards):
        b = min(max(b, a + CARD_MIN_S), a + CARD_MAX_S)
        els.append(f'<div class="clip" id="cd{ci}" data-start="{a:.3f}" data-duration="{b - a:.3f}" data-track-index="5">{card_html(card)}</div>')
        js.append(f'tl.fromTo("#cd{ci} .cd", {{opacity: 0, scale: 0.8, y: 30}}, {{opacity: 1, scale: 1, y: 0, duration: {m["card_s"]:g}, '
                  f'ease: "{m["card_ease"]}"}}, {a:.3f});')
        js.append(f'tl.to("#cd{ci} .cd", {{opacity: 0, duration: 0.2}}, {max(a, b - 0.2):.3f});')
    return els, js


def place_cards(cards: list[dict], timing: Callable[[int, int], tuple[float, float] | None],
                body_len: float) -> list[tuple[float, float, dict]]:
    """Card times in the finished ad. `timing(from, to)` gives (start, end) in ad time, or None when the words were cut away.
    Cards that would run into the closing card or each other are dropped."""
    out: list[tuple[float, float, dict]] = []
    for c in cards:
        t = timing(c["from"], c["to"])
        if not t:
            continue
        a, b = t
        b = min(max(b, a + CARD_MIN_S), a + CARD_MAX_S, body_len - 0.05)
        if b - a < CARD_MIN_S or (out and a < out[-1][1] + CARD_GAP_S):     # build_cards never stretches a card past what is kept here
            continue
        out.append((a, b, c))
    return out


# ---------------------------------------------------------------- what the planner is shown and what is remembered

def summary(design: dict | None) -> dict:
    """The few fields that identify a look, for the job's result and for the history Gemini is shown."""
    d = design or CLASSIC
    return {"caption_style": d["caption_style"], "headline_style": d["headline_style"], "callout_style": d["callout_style"],
            "end_style": d["end_style"], "font": d["font"], "motion": d["motion"], "accent": d["palette"]["accent"],
            "cards": [c["kind"] for c in d["cards"]], "designed": design is not None}


def history_text(items: list[dict], limit: int = 20) -> str:
    """The recent looks as plain lines, newest first, for the prompt (so Gemini does not fall back on its favourite)."""
    lines = []
    def known(value, table) -> str:
        return value if isinstance(value, str) and value in table else "?"
    for s in items[:limit]:
        if isinstance(s, dict) and s.get("caption_style") in CAPTION_STYLES:
            accent = s.get("accent") if isinstance(s.get("accent"), str) and HEX.fullmatch(s["accent"]) else "?"
            cards = [c for c in (s.get("cards") if isinstance(s.get("cards"), list) else []) if isinstance(c, str) and c in CARD_KINDS]
            lines.append(f"- {s['caption_style']} captions, {known(s.get('headline_style'), HEADLINE_STYLES)} headline, {known(s.get('font'), FONTS)} font, "
                         f"{known(s.get('motion'), MOTIONS)} motion, {known(s.get('end_style'), END_STYLES)} end screen, accent {accent}"
                         + (f", cards: {', '.join(cards)}" if cards else ""))
    return "\n".join(lines) or "(No earlier ads yet.)"


def options_text() -> str:
    """The closed lists as prompt text, built from the same tables the code checks against, so the two cannot drift apart."""
    def block(title: str, table: dict) -> str:
        return f"{title}:\n" + "\n".join(f"  - `{k}`: {v if isinstance(v, str) else v['feel']}" for k, v in table.items())
    return "\n\n".join([
        "Fonts:\n" + "\n".join(f"  - `{k}`: {v[3]}" for k, v in FONTS.items()),
        block("Motion (`motion`)", {"calm": "slow, soft landings, for explanations", "confident": "the original, a clear pop on each word",
                                    "hype": "big pops and bouncy arrivals, for energy"}),
        block("Caption styles (`caption_style`)", CAPTION_STYLES), block("Headline styles (`headline_style`)", HEADLINE_STYLES),
        block("Callout styles (`callout_style`)", CALLOUT_STYLES), block("End screens (`end_style`)", END_STYLES),
        block("Caption position (`caption_y`)", {"standard": "the style's own place", "high": "70 px higher (clear of a speaker's lower body)",
                                                 "low": "50 px lower"}),
        block("Callout zone (`callout_zone`)", {k: f"{side} side, {'high' if top < 700 else 'middle' if top < 800 else 'low'}"
                                                for k, (side, top) in CALLOUT_ZONES.items()}),
        block("Card kinds (`cards[].kind`)", CARD_KINDS)])
