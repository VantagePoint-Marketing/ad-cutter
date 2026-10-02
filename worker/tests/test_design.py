"""The per-ad design system: validation of what the planner picks, and the HTML/CSS/GSAP it becomes."""
import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ad_cutter as ac  # noqa: E402
import brain  # noqa: E402
import design  # noqa: E402

CTA = ("Default line", "Default button")
CFG = {"brand": {"name": "B", "product": "p", "audience": "a", "cta_line": "Default line", "cta_button": "Default button"},
       "ad_count": 2, "ad_min_seconds": 20, "ad_max_seconds": 75}


def make(raw=None):
    return design.sanitize(raw, CTA)


def test_an_empty_or_garbage_design_becomes_the_safe_default_and_never_raises():
    for raw in (None, {}, "x", 5, [], {"captions": 5, "palette": "red", "end_screen": [], "punch_ins": "no"}):
        d, notes = make(raw)
        assert d["captions"]["style"] == "bold_outline" and d["end_screen"]["line"] == "Default line"
        assert d["palette"] == design.DEFAULT_PALETTE and notes


def test_unknown_names_fall_back_and_are_reported():
    d, notes = make({"pacing": "frantic", "captions": {"style": "comic_sans"}, "headline": {"style": "neon"},
                     "callouts": {"style": "lasers"}, "end_screen": {"layout": "x", "line": "L", "button": "B"}})
    assert (d["pacing"], d["captions"]["style"], d["headline"]["style"]) == ("natural", "bold_outline", "white_box")
    assert d["callouts"]["style"] == "marker_box" and d["end_screen"]["layout"] == "centered_card"
    assert sum("not a known option" in n for n in notes) == 5


def test_a_valid_design_is_kept_as_chosen():
    d, notes = make({"mood": "loud", "pacing": "tight", "palette": {"accent": "#00ffaa", "accent2": "#ff0066",
                                                                      "ink": "#000000", "paper": "#ffffff"},
                     "captions": {"style": "pill_all", "position": "middle", "case": "lower", "size": "large",
                                  "words_per_group": 2},
                     "headline": {"style": "ribbon", "position": "upper"}, "headline_motion": "drop",
                     "callouts": {"style": "banner"}, "callout_motion": "slide",
                     "end_screen": {"layout": "split_bar", "motion": "wipe", "line": "Get it", "button": "Go",
                                    "seconds": 2}})
    assert notes == []
    assert d["palette"]["accent"] == "#00FFAA" and d["captions"]["words_per_group"] == 2
    assert d["end_screen"]["layout"] == "split_bar" and d["pacing"] == "tight"


def test_palette_values_that_are_not_plain_hex_colours_are_refused():
    # the palette ends up inside a <style> block, so nothing but #RRGGBB may pass
    evil = "#FFF;}</style><script>alert(1)</script>"
    d, _ = make({"palette": {"accent": evil, "accent2": "red", "ink": "#12345", "paper": "#GGGGGG"}})
    assert d["palette"] == design.DEFAULT_PALETTE
    assert "<script" not in design.css(d)


def test_low_contrast_text_and_outline_colours_are_reset():
    d, notes = make({"palette": {"paper": "#777777", "ink": "#888888"}})
    assert (d["palette"]["paper"], d["palette"]["ink"]) == ("#FFFFFF", "#111111")
    assert any("too close" in n for n in notes)


def test_numbers_are_clamped():
    d, _ = make({"captions": {"words_per_group": 99}, "end_screen": {"line": "a", "button": "b", "seconds": 99},
                 "punch_ins": [{"from": 1, "to": 3, "zoom": 9}, {"from": 2, "to": 1, "zoom": 1.1},
                               {"from": "x", "to": 2, "zoom": 1.1}, {"from": 4, "to": 5}] + [{"from": 1, "to": 2, "zoom": 1.1}] * 9})
    assert d["captions"]["words_per_group"] == 4 and d["end_screen"]["seconds"] == 4.0
    assert d["punch_ins"][0]["zoom"] == 1.25 and len(d["punch_ins"]) <= design.MAX_PUNCH_INS
    d, _ = make({"captions": {"style": "impact_single", "words_per_group": 4}})
    assert d["captions"]["words_per_group"] == 1 and design.caption_limits(d) == (1, 9)


def test_end_screen_text_is_trimmed_and_missing_text_falls_back_to_the_brand():
    d, _ = make({"end_screen": {"line": "x" * 200, "button": "  Tap \n here  "}})
    assert len(d["end_screen"]["line"]) == 70 and d["end_screen"]["button"] == "Tap here"
    d, notes = make({"end_screen": {"line": "Only a line"}})
    assert d["end_screen"]["line"] == "Default line" and any("default call to action" in n for n in notes)


LAYOUT_GRID = list(itertools.product(design.CAPTION_STYLES, design.HEADLINE_STYLES, design.CALLOUT_STYLES,
                                     design.END_LAYOUTS))


@pytest.mark.parametrize("cap,head,call,end", LAYOUT_GRID[::7] + LAYOUT_GRID[-3:])
def test_every_combination_renders_a_complete_document(cap, head, call, end):
    d, notes = make({"captions": {"style": cap}, "headline": {"style": head}, "callouts": {"style": call},
                     "end_screen": {"layout": end, "line": "Line <b>", "button": "Button & co"}})
    assert notes == []
    ad = {"name": "N", "headline": "Head <i>", "design": d}
    words = [{"w": "go", "s": 0.1, "e": 0.4, "key": True}, {"w": "now", "s": 0.4, "e": 0.7, "key": False}]
    doc = ac.compose(ad, words, [(0.5, 3.0, "Hi\nthere", "left"), (3.2, 5.0, "Two", "center")], 6.0, CFG,
                     [(1.0, 2.0, 1.1)])
    assert "${" not in doc and "$" not in design.css(d)
    assert "Line &lt;b&gt;" in doc and "Button &amp; co" in doc and "Head &lt;i&gt;" in doc
    assert 'id="co0"' in doc and 'id="co1"' in doc and "scale: 1.1" in doc
    assert f'data-duration="{6.0 + d["end_screen"]["seconds"]:.3f}"' in doc


def test_two_looks_produce_visibly_different_documents():
    a, _ = make({"captions": {"style": "bold_outline"}, "palette": {"accent": "#FFE11A"}})
    b, _ = make({"captions": {"style": "bar_karaoke"}, "palette": {"accent": "#22CCAA"},
                 "end_screen": {"layout": "big_question", "line": "L", "button": "B"}})
    ad = {"name": "N", "headline": "H"}
    words = [{"w": "go", "s": 0.1, "e": 0.4, "key": False}]
    assert ac.compose({**ad, "design": a}, words, [], 5.0, CFG) != ac.compose({**ad, "design": b}, words, [], 5.0, CFG)
    assert design.signature(a) != design.signature(b)


def test_every_motion_produces_a_gsap_call():
    for m in design.MOTIONS:
        js = design.motion_in("#x", m, 1.5, "left")
        assert js.startswith('tl.fromTo("#x"') and "1.500" in js


@pytest.mark.parametrize("pos,head", list(itertools.product(design.CAPTION_POSITIONS, design.HEADLINE_STYLES)))
def test_callout_bands_stay_clear_of_captions_and_headline(pos, head):
    d, _ = make({"captions": {"position": pos}, "headline": {"style": head, "position": "upper"}})
    cap_top = design.CAPTION_POSITIONS[pos]
    for y in design.free_bands(d):
        assert not (y < cap_top + 300 and cap_top - 20 < y + 250) or y == design.CALLOUT_BANDS[0]


def test_pacing_choices_change_how_pauses_are_trimmed():
    tight, _ = make({"pacing": "tight"})
    calm, _ = make({"pacing": "breathing"})
    assert design.pace(tight)[0] < design.pace(calm)[0] and design.pace(tight)[1] < design.pace(calm)[1]


def test_caption_groups_follow_the_design_limits():
    words = [{"w": w, "s": i * 0.2, "e": i * 0.2 + 0.15} for i, w in enumerate(["a", "b", "c", "d", "e"])]
    assert [len(g) for g in ac.caption_groups(words, 2, 16)] == [2, 2, 1]
    assert [len(g) for g in ac.caption_groups(words, 1, 16)] == [1, 1, 1, 1, 1]


def test_punch_ins_cannot_overlap_or_run_into_the_end_screen():
    out = ac.place_punch_ins([(1.0, 3.0, 1.1), (2.5, 4.0, 1.2), (9.9, 12.0, 1.1), (5.0, 5.2, 1.1)], 10.0)
    assert out == [(1.0, 3.0, 1.1), (3.1, 4.0, 1.2)]


def test_a_job_whose_ads_share_one_look_is_told_so():
    ws = [{"w": "w", "s": i * 0.5, "e": i * 0.5 + 0.4} for i in range(60)]
    ad = {"name": "A", "headline": "H", "segments": [{"from": 0, "to": 59}], "callouts": [], "primary_text": "p"}
    plan = {"summary": "s", "ads": [dict(ad, name="One"), dict(ad, name="Two")]}
    _, notes = ac.validate_plan(plan, ws, CFG)
    assert any("share the same look" in n for n in notes)


def test_looks_are_remembered_between_jobs_and_a_bad_path_is_harmless(tmp_path):
    f = tmp_path / "history.jsonl"
    d1, _ = make({"mood": "calm", "captions": {"style": "pill_all"}})
    d2, _ = make({"mood": "loud", "captions": {"style": "impact_single"}})
    design.remember(f, [d1])
    design.remember(f, [d2])
    text = design.recent_looks(f)
    assert "pill_all" in text and "impact_single" in text and "(loud)" in text
    assert design.recent_looks(tmp_path / "missing.jsonl") == ""
    design.remember(tmp_path, [d1])          # a directory cannot be written as a file: no exception
    for _ in range(60):
        design.remember(f, [d1])
    assert len(f.read_text(encoding="utf-8").splitlines()) == 40


def test_the_prompt_menu_lists_every_option():
    menu = design.menu_text()
    for name in itertools.chain(design.CAPTION_STYLES, design.HEADLINE_STYLES, design.CALLOUT_STYLES,
                                design.END_LAYOUTS, design.MOTIONS, design.PACING):
        assert f"`{name}`" in menu


def test_the_brain_reads_every_note_in_order_strips_comments_and_is_capped(tmp_path):
    (tmp_path / "02_b.md").write_text("<!-- hidden -->\nsecond", encoding="utf-8")
    (tmp_path / "01_a.md").write_text("first", encoding="utf-8")
    (tmp_path / "03_c.json").write_text(json.dumps({"k": 1}), encoding="utf-8")
    (tmp_path / "skip.txt").write_text("ignored", encoding="utf-8")
    text = brain.load(tmp_path)
    assert text.index("first") < text.index("second") < text.index('"k"')
    assert "hidden" not in text and "ignored" not in text
    assert len(brain.load(tmp_path, limit=10)) == 10
    assert brain.load(tmp_path / "nowhere") == "(No craft notes yet.)"


def test_the_shipped_brain_is_loaded_into_the_planning_prompt():
    cfg = {**CFG, "ad_count": 3}
    words = [{"w": "hello", "s": 0.0, "e": 0.3}]
    p = ac.build_prompt(cfg, words, recent_looks="- bold_outline/white_box/marker_box/centered_card/#FFE11A")
    assert "Applying the craft to short ads" in p and "Curiosity loops" in p
    assert "`clean_shadow`" in p and "bold_outline/white_box" in p
    assert "Default line" in p
