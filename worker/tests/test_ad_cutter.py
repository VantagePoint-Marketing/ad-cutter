import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ad_cutter as ac  # noqa: E402

CFG = {"ad_min_seconds": 20, "ad_max_seconds": 75, "cta_seconds": 3,
       "brand": {"name": "VantagePoint", "product": "p", "audience": "a", "cta_line": "See <it>", "cta_button": "Go"}}


def energy(spans, total=10.0, speech=-25.0, silence=-55.0):
    """Synthetic loudness: `spans` are (start, end) seconds of speech; everything else is silence."""
    n = int(total / ac.Energy.HOP)
    db = np.full(n, silence)
    for a, b in spans:
        db[int(a / ac.Energy.HOP):int(b / ac.Energy.HOP)] = speech
    return ac.Energy(db)


def words_from(text, start=0.0, step=0.5):
    return [{"w": w, "s": start + i * step, "e": start + i * step + 0.4} for i, w in enumerate(text.split())]


# ---------------------------------------------------------------- basics

def test_config_has_no_key_file_and_names_an_env_var():
    cfg = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8"))
    assert "openrouter_key_file" not in cfg
    assert cfg["openrouter_key_env"] == "OPENROUTER_VIDEO_AGENT_KEY"
    assert not any("key_file" in k for k in cfg)


def test_merge_percent_joins_number_and_sign():
    out = ac.merge_percent([{"w": "75", "s": 1, "e": 1.2}, {"w": "%", "s": 1.2, "e": 1.4}, {"w": "of", "s": 1.5, "e": 1.6}])
    assert [w["w"] for w in out] == ["75%", "of"] and out[0]["e"] == 1.4


def test_extract_json_handles_fences_and_prose():
    assert ac.extract_json('Sure:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert ac.extract_json('noise {"a": {"b": 2}} trailing') == {"a": {"b": 2}}


def test_transcript_for_prompt_numbers_every_word():
    text = ac.transcript_for_prompt(words_from("one two three."))
    assert "#0 one" in text and "#2 three." in text and text.startswith("[0.0s]")


def test_build_prompt_fills_placeholders(tmp_path):
    p = ac.build_prompt({**CFG, "ad_count": 3}, words_from("hello world"))
    assert "{" not in p.split("## Output")[0] and "#1 world" in p


# ---------------------------------------------------------------- energy & edges

def test_gaps_ignore_short_blips_and_threshold_adapts():
    en = energy([(0, 1), (1.02, 1.04), (3, 4)])        # 20 ms blip inside a 2 s silence
    assert -55 < en.threshold < -25
    gaps = en.gaps(0, 4, 0.5)
    assert len(gaps) == 1 and gaps[0][0] == pytest.approx(1.0, abs=0.02) and gaps[0][1] == pytest.approx(3.0, abs=0.02)


def test_start_edge_cuts_between_run_together_words():
    # "But" 4.06-4.14 runs into "if" 4.14; a 20 ms dip sits at 4.10.
    en = energy([(4.04, 4.10), (4.12, 4.6)])
    clip = [{"w": "But", "s": 4.06, "e": 4.14}, {"w": "if", "s": 4.14, "e": 4.24}]
    t = ac.start_edge(clip, 1, clip[1], en)
    assert 4.09 <= t <= 4.12


def test_start_edge_skips_silence_whisper_stretched_the_word_over():
    # Whisper says "if" is 102.10-102.88, but the audio has silence 102.22-102.64 inside it.
    en = energy([(101.5, 102.22), (102.64, 103.0)], total=104)
    clip = [{"w": "because", "s": 101.5, "e": 102.10}, {"w": "if", "s": 102.10, "e": 102.88}]
    t = ac.start_edge(clip, 1, clip[1], en)
    assert 102.5 <= t <= 102.64


def test_start_edge_leaves_air_after_a_pause():
    en = energy([(1.0, 2.0), (3.0, 4.0)])
    clip = [{"w": "end", "s": 1.5, "e": 2.0}, {"w": "Guess", "s": 3.0, "e": 3.3}]
    t = ac.start_edge(clip, 1, clip[1], en)
    assert 2.85 <= t < 3.0


def test_end_edge_stops_before_stretched_silence():
    # "gains" 58.95-59.43 but speech stops at 59.34.
    en = energy([(58.5, 59.34), (60.1, 61)], total=62)
    clip = [{"w": "gains", "s": 58.95, "e": 59.43}, {"w": "but", "s": 59.43, "e": 60.21}]
    t = ac.end_edge(clip, 0, clip[0], en)
    assert 59.34 <= t <= 59.5


def test_edges_fall_back_when_word_not_found():
    en = energy([(1, 2)])
    w = {"w": "x", "s": 1.0, "e": 2.0}
    assert ac.start_edge([], None, w, en) == pytest.approx(0.95)
    assert ac.end_edge([], None, w, en) == pytest.approx(2.05)


def test_find_word_normalises_and_picks_nearest():
    clip = [{"w": "Trend.", "s": 1.0, "e": 1.2}, {"w": "trend", "s": 3.0, "e": 3.2}]
    assert ac.find_word(clip, "trend", 2.9) == 1
    assert ac.find_word(clip, "trend", 9.0) is None


# ---------------------------------------------------------------- cutting & time mapping

def test_keep_intervals_trims_long_pauses_only():
    en = energy([(0, 1), (2.5, 3.0), (3.3, 4.0)], total=5)    # 1.5 s pause (trim), 0.3 s pause (keep)
    ivs, total = ac.keep_intervals([(0.0, 4.0)], en)
    assert len(ivs) == 2
    assert total == pytest.approx(4.0 - 1.5 + 2 * ac.PAUSE_KEEP, abs=0.05)


def test_to_out_maps_segments_played_out_of_source_order():
    # Segment 0 comes from later in the source than segment 1 (like Cut 1 today).
    en = energy([(0, 10), (60, 70)], total=80)
    ivs, total = ac.keep_intervals([(60.0, 70.0), (4.0, 9.0)], en)
    assert total == pytest.approx(15.0, abs=0.05)
    assert ac.to_out(60.0, ivs, 0) == pytest.approx(0.0, abs=0.04)
    assert ac.to_out(4.0, ivs, 1) == pytest.approx(10.0, abs=0.04)   # not 0: it plays second
    assert ac.to_out(9.5, ivs, 1) == pytest.approx(15.0, abs=0.04)   # past the end clamps to the end


# ---------------------------------------------------------------- plan validation

def make_plan(**ad_over):
    ad = {"name": "A", "headline": "H", "segments": [{"from": 0, "to": 59}], "callouts": [], "primary_text": "p"}
    ad.update(ad_over)
    return {"summary": "s", "caption_fixes": [], "highlight_words": [], "ads": [ad]}


def test_validate_plan_accepts_good_plan():
    plan, notes = ac.validate_plan(make_plan(), words_from(" ".join(["w"] * 60)), CFG)
    assert notes == [] and plan["ads"][0]["segments"] == [{"from": 0, "to": 59}]


def test_validate_plan_moves_start_off_filler_words():
    ws = words_from("but so we talk " + " ".join(["w"] * 56))
    plan, notes = ac.validate_plan(make_plan(), ws, CFG)
    assert plan["ads"][0]["segments"][0]["from"] == 2 and any("moved" in n for n in notes)


def test_validate_plan_rejects_bad_ranges_and_bad_callouts():
    ws = words_from(" ".join(["w"] * 60))
    with pytest.raises(ac.AdCutterError):
        ac.validate_plan(make_plan(segments=[{"from": 50, "to": 999}]), ws, CFG)
    plan, notes = ac.validate_plan(make_plan(segments=[{"from": 0, "to": 50}], callouts=[
        {"from": 55, "to": 58, "text": "outside"},
        {"from": 1, "to": 3, "text": "a line that is far too long to fit"},
        {"from": 1, "to": 3, "text": "ok\nfine"}]), ws, CFG)
    assert [c["text"] for c in plan["ads"][0]["callouts"]] == ["ok\nfine"] and len(notes) == 2


def test_validate_plan_flags_length_outside_target():
    _, notes = ac.validate_plan(make_plan(segments=[{"from": 0, "to": 5}]), words_from(" ".join(["w"] * 60)), CFG)
    assert any("outside" in n for n in notes)


def test_display_words_applies_fixes_and_highlights():
    ws = words_from("vantage point is up three days")
    plan = {"caption_fixes": [{"from": 0, "to": 1, "text": "VantagePoint"}, {"from": 4, "to": 4, "text": "3"}],
            "highlight_words": [4, 5]}
    d = ac.display_words(ws, plan)
    assert [w["w"] for w in d] == ["VantagePoint", "", "is", "up", "3", "days"]
    assert d[0]["e"] == ws[1]["e"] and d[4]["key"] and d[5]["key"] and not d[0]["key"]


# ---------------------------------------------------------------- captions & composition

def test_caption_groups_break_on_pause_and_length():
    ws = [{"w": "one", "s": 0, "e": 0.2}, {"w": "two", "s": 0.25, "e": 0.4}, {"w": "three", "s": 1.5, "e": 1.7},
          {"w": "extraordinarily", "s": 1.75, "e": 2.2}]
    assert [[w["w"] for w in g] for g in ac.caption_groups(ws)] == [["one", "two"], ["three"], ["extraordinarily"]]


def test_compose_escapes_text_and_marks_layers():
    ad = {"name": "A & B", "headline": "Is <this> 3 days late?"}
    words = [{"w": "3", "s": 0.1, "e": 0.4, "key": True}, {"w": "days", "s": 0.4, "e": 0.7, "key": False}]
    doc = ac.compose(ad, words, [(0.2, 2.5, "Entry:\n~3 days late")], 5.0, CFG)
    assert "Is &lt;this&gt; 3 days late?" in doc and "See &lt;it&gt;" in doc
    assert "Entry:<br>~3 days late" in doc
    assert doc.count("data-layout-allow-overlap") == 4
    assert 'data-duration="8.000"' in doc and "${" not in doc


def test_unique_file_never_overwrites(tmp_path):
    f = tmp_path / "Review Notes.md"
    assert ac.unique_file(f) == f
    f.write_text("x")
    assert ac.unique_file(f).name == "Review Notes (2).md"


# ---------------------------------------------------------------- review fixes (code-reviewer, 2026-09-29)

def test_start_edge_ignores_silence_after_the_word():
    # Whisper: word 1.0-1.6, but speech is 1.0-1.3 and then silence. The cut-in must stay at the word's start.
    en = energy([(0.2, 0.7), (1.0, 1.3)], total=3)
    clip = [{"w": "before", "s": 0.2, "e": 0.7}, {"w": "word", "s": 1.0, "e": 1.6}]
    t = ac.start_edge(clip, 1, clip[1], en)
    assert 0.7 <= t <= 1.0


def test_end_edge_ignores_silence_before_the_word():
    # Whisper: last word 0.4-1.3, stretched back over a pause; speech is 1.0-1.3. The cut-out must keep it.
    en = energy([(0.0, 0.4), (1.0, 1.3)], total=3)
    clip = [{"w": "prev", "s": 0.0, "e": 0.4}, {"w": "last", "s": 0.4, "e": 1.3}]
    t = ac.end_edge(clip, 1, clip[1], en)
    assert t >= 1.3


def test_place_callouts_stays_off_the_cta_and_each_other():
    out = ac.place_callouts([(9.3, 9.4, "late"), (1.0, 1.2, "a"), (2.0, 3.0, "b"), (9.9, 9.95, "tiny")], 10.0)
    assert [t for _, _, t in out] == ["a", "b", "late"]
    assert out[0][1] <= 1.9                      # capped at the next callout's start
    assert all(b <= 9.95 for _, b, _ in out)     # never runs into the CTA card
    assert all(b - a >= 0.5 for a, b, _ in out)


def test_validate_plan_survives_malformed_gemini_output():
    ws = words_from(" ".join(["w"] * 60))
    plan = {"caption_fixes": None, "highlight_words": [True, 3, "x", 999], "claims_to_review": "none",
            "ads": ["junk", {"name": "A", "headline": "H", "segments": ["bad", {"from": 0, "to": 59}],
                             "callouts": [None, {"from": 1, "to": 2, "text": 5}], "primary_text": None}]}
    out, notes = ac.validate_plan(plan, ws, CFG)
    assert out["caption_fixes"] == [] and out["highlight_words"] == [3] and out["claims_to_review"] == []
    ad = out["ads"][0]
    assert ad["callouts"] == [] and ad["primary_text"] == "" and len(ad["segments"]) == 1
    with pytest.raises(ac.AdCutterError):
        ac.validate_plan({"ads": [{"name": 7, "headline": "H", "segments": [{"from": 0, "to": 5}]}]}, ws, CFG)


def test_validate_plan_drops_overlapping_caption_fixes():
    ws = words_from("vantage point is up")
    plan = make_plan(segments=[{"from": 0, "to": 3}])
    plan["caption_fixes"] = [{"from": 0, "to": 1, "text": "VantagePoint"}, {"from": 1, "to": 2, "text": "dup"}]
    out, _ = ac.validate_plan(plan, ws, {**CFG, "ad_min_seconds": 0})
    assert out["caption_fixes"] == [{"from": 0, "to": 1, "text": "VantagePoint"}]


def test_validate_plan_keeps_callout_that_began_on_a_skipped_filler():
    ws = words_from("but we talk " + " ".join(["w"] * 57))
    plan = make_plan(callouts=[{"from": 0, "to": 2, "text": "ok"}])
    out, _ = ac.validate_plan(plan, ws, CFG)
    assert out["ads"][0]["callouts"] == [{"from": 1, "to": 2, "text": "ok"}]


def test_quietest_handles_times_past_the_end():
    en = energy([(0, 1)], total=2)
    assert 0 <= en.quietest(5.0, 6.0) <= 2.0
