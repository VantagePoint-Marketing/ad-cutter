import json
import shutil
import subprocess
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


# ---------------------------------------------------------------- several clips and the request (W1, 2026-10-01)

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg/ffprobe not installed")


def make_clip(path, seconds=2.0, size="320x568", layout="mono", rate=44100):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration={seconds}",
                    "-af", f"aformat=channel_layouts={layout}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
    return path


def test_build_prompt_carries_the_request_and_the_clip_map():
    ws = words_from(" ".join(["w"] * 10))      # words start at 0.0, 0.5, ... 4.5 s
    clips = [{"name": "A.MOV", "start": 0.0, "seconds": 2.0}, {"name": "B.MOV", "start": 2.0, "seconds": 3.0}]
    p = ac.build_prompt({**CFG, "ad_count": 3, "ad_count_max": 6}, ws, "Two 15-second cold ads, {curly} braces", clips)
    assert "Two 15-second cold ads, {curly} braces" in p
    assert "2 clips joined in order" in p and "Never plan more than 6 ads" in p
    assert "- Clip 1: A.MOV, 0:00 to 0:02 (words #0 to #3)" in p
    assert "- Clip 2: B.MOV, 0:02 to 0:05 (words #4 to #9)" in p
    assert "## Output" in p and p.count('"response_to_request"') == 1


def test_build_prompt_without_a_request_says_so_and_cuts_long_requests():
    p = ac.build_prompt({**CFG, "ad_count": 3}, words_from("hello world"))
    assert "No request was given" in p and "one clip" in p
    p = ac.build_prompt({**CFG, "ad_count": 3}, words_from("hello world"), "x" * 5000)
    assert "x" * ac.MAX_BRIEF in p and "x" * (ac.MAX_BRIEF + 1) not in p


def test_build_prompt_quotes_the_teams_notes_and_defaults_to_none():
    ws = words_from("hello world")
    p = ac.build_prompt({**CFG, "ad_count": 3}, ws)
    assert "## What the team said about earlier ads" in p and "(Nothing yet.)" in p
    notes = '- Not right ("Lag", headline "H"): "captions too small {x}"'
    p = ac.build_prompt({**CFG, "ad_count": 3}, ws, "req", None, notes)
    assert notes in p and "never override the request" in p and p.index(notes) < p.index("## The footage")


def test_write_notes_shows_the_self_check_for_each_ad(tmp_path):
    plan = {"summary": "s", "claims_to_review": []}
    review = {"scores": {"hook": 2, "cuts": 4, "request_fit": 3}, "verdict": "Slow start.", "look": True,
              "problems": [{"at_s": 3.0, "area": "hook", "what": "Nothing happens.", "fix": "Cut the first 2 s."},
                           {"at_s": None, "area": "request_fit", "what": "Too long.", "fix": "Trim."}]}
    report = [{"k": 1, "ad": {"name": "Lag", "headline": "H", "callouts": []}, "len": 30.0, "check": "passed",
               "file": "a.mp4", "review": review},
              {"k": 2, "ad": {"name": "Other", "headline": "H", "callouts": []}, "len": 30.0, "check": "passed",
               "file": "b.mp4", "review": None}]
    ac.write_notes(tmp_path, "A.MOV", plan, [], report, 0.1, {**CFG, "plan_model": "m"}, "")
    text = (tmp_path / "Review Notes.md").read_text(encoding="utf-8")
    assert "**Self-check:** LOOK AT THIS: hook 2/5, cuts 4/5, request fit 3/5" in text
    assert "  - Slow start." in text and "  - 0:03 (hook) Nothing happens. Fix: Cut the first 2 s." in text
    assert "  - (request fit) Too long. Fix: Trim." in text and text.count("Self-check") == 1
    review["problems"][1]["fix"] = ""
    ac.write_notes(tmp_path, "A.MOV", plan, [], report, 0.1, {**CFG, "plan_model": "m"}, "")
    assert "  - (request fit) Too long.\n" in (tmp_path / "Review Notes (2).md").read_text(encoding="utf-8")
    assert "\n\n**Primary text:**" in text                       # the blank lines between sections survive


def test_validate_plan_caps_the_number_of_ads_and_keeps_the_response():
    ws = words_from(" ".join(["w"] * 60))
    plan = make_plan()
    plan["ads"] = [dict(plan["ads"][0], name=f"A{i}") for i in range(8)]
    plan["response_to_request"] = "Made eight."
    out, notes = ac.validate_plan(plan, ws, {**CFG, "ad_count_max": 6})
    assert len(out["ads"]) == 6 and out["response_to_request"] == "Made eight."
    assert any("only the first 6" in n for n in notes)
    out, _ = ac.validate_plan(make_plan(), ws, CFG)
    assert out["response_to_request"] == ""


def test_validate_plan_notes_a_segment_that_runs_across_a_clip_join():
    ws = words_from(" ".join(["w"] * 60))                  # 30 s of words
    clips = [{"name": "A", "start": 0.0, "seconds": 10.0}, {"name": "B", "start": 10.0, "seconds": 20.0}]
    _, notes = ac.validate_plan(make_plan(segments=[{"from": 10, "to": 50}]), ws, CFG, clips)
    assert any("across the join between clip 1 and clip 2" in n for n in notes)
    _, notes = ac.validate_plan(make_plan(segments=[{"from": 22, "to": 50}]), ws, CFG, clips)
    assert notes == []


def test_safe_name_only_makes_names_the_bucket_accepts():
    from storage import Bucket
    for name in ("Don't Trade Blind", "Proof & Numbers", "Why 90% of Traders Lose", "Lag — the ’real’ cost",
                 "  spaced   out  ", "", "x" * 80, "../../x"):
        assert Bucket.result_key("j", f"Ad - {ac.safe_name(name)} (2026-10-01).mp4").endswith(".mp4")
    assert ac.safe_name("Don't Trade Blind") == "Dont Trade Blind" and ac.safe_name("Proof & Numbers") == "Proof Numbers"
    assert ac.safe_name("") == "Ad" and len(ac.safe_name("x" * 80)) == 60


def test_write_notes_names_the_footage_and_the_request(tmp_path):
    plan = {"summary": "s", "response_to_request": "Did as asked.", "claims_to_review": []}
    ac.write_notes(tmp_path, "A.MOV + 1 more", plan, [], [], 0.1, {**CFG, "plan_model": "m"}, "make it punchy")
    text = (tmp_path / "Review Notes.md").read_text(encoding="utf-8")
    assert text.startswith("# Ad cuts from A.MOV + 1 more") and "make it punchy" in text and "Did as asked." in text


def test_working_copy_args_guard_every_input_and_join_them():
    args = ac.working_copy_args([Path("a.mov"), Path("b.mkv")], ["mov", "matroska"], [120.0, 30.5])
    assert args.count("-i") == 2 and args.count("-protocol_whitelist") == 2 and args.count("-t") == 2
    assert "120.250" in args and "30.750" in args          # each input capped at its own checked length
    assert "-enable_drefs" in args and "matroska" in args
    fc = args[args.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=1" in fc and "[0:v]scale=1080:1920" in fc and "[1:a]aresample=48000" in fc
    assert fc.count("channel_layouts=stereo") == 2 and fc.count("setsar=1") == 2


@needs_ffmpeg
def test_prepare_joins_clips_in_order_and_maps_them(tmp_path):
    a = make_clip(tmp_path / "a.mp4", 2.0, "320x568", "mono", 44100)
    b = make_clip(tmp_path / "b.mp4", 3.0, "640x360", "stereo", 48000)
    media = ac.prepare([a, b], tmp_path / "work", max_seconds=60, names=["A.MOV", "B.MOV"])
    assert 4.8 <= media["duration"] <= 5.3
    assert [c["name"] for c in media["clips"]] == ["A.MOV", "B.MOV"]
    assert media["clips"][0]["start"] == 0.0 and abs(media["clips"][1]["start"] - 2.0) < 0.15
    assert abs(media["clips"][1]["seconds"] - 3.0) < 0.15
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,channels,sample_rate", "-of", "json",
         str(media["full"])], capture_output=True, text=True, check=True).stdout)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    au = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    assert (v["width"], v["height"]) == (1080, 1920) and au["channels"] == 2 and au["sample_rate"] == "48000"
    assert media["wav"].exists() and media["proxy"].exists()


@needs_ffmpeg
def test_prepare_refuses_footage_over_the_total_limit(tmp_path):
    a = make_clip(tmp_path / "a.mp4", 2.0)
    b = make_clip(tmp_path / "b.mp4", 2.0)
    with pytest.raises(ac.AdCutterError, match="add up to"):
        ac.prepare([a, b], tmp_path / "work", max_seconds=3)


@needs_ffmpeg
def test_prepare_names_the_bad_clip(tmp_path):
    good = make_clip(tmp_path / "a.mp4", 2.0)
    bad = tmp_path / "b.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(ac.AdCutterError, match=r"clip 2 \(B\.MOV\)"):
        ac.prepare([good, bad], tmp_path / "work", names=["A.MOV", "B.MOV"])


# ---------------------------------------------------------------- HDR clips (the render tool cannot take them)

def test_an_hdr_clip_gets_the_conversion_chain_and_standard_tags_and_an_ordinary_clip_does_not():
    plain = ac.working_copy_args([Path("a.mov")], ["mov"], [10.0])
    assert "zscale" not in " ".join(plain) and "-color_trc" not in plain          # ordinary footage: nothing changed
    hdr = ac.working_copy_args([Path("a.mov"), Path("b.mov")], ["mov", "mov"], [10.0, 10.0], ["arib-std-b67", None])
    fc = hdr[hdr.index("-filter_complex") + 1]
    assert fc.count("zscale=tin=arib-std-b67") == 1 and "tonemap=tonemap=hable" in fc      # only the HDR clip is converted
    assert "[1:v]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,setsar=1,fps=30,format=yuv420p[v1]" in fc
    assert hdr[hdr.index("-color_trc") + 1] == "bt709" and hdr[hdr.index("-colorspace") + 1] == "bt709"
    fallback = ac.working_copy_args([Path("a.mov")], ["mov"], [10.0], ["smpte2084"], tonemap=False)
    fc = fallback[fallback.index("-filter_complex") + 1]
    assert "zscale" not in fc and "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709" in fc


def probe_tags(path):
    res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=pix_fmt,color_transfer,color_primaries,color_space", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True)
    return json.loads(res.stdout)["streams"][0]


needs_hlg = pytest.mark.skipif(not (shutil.which("ffmpeg") and "libx265" in subprocess.run(
    ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout), reason="needs ffmpeg with libx265")


@needs_hlg
def test_prepare_turns_an_hdr_clip_into_standard_video_and_says_so(tmp_path):
    from tests.test_safe_media import make_hlg
    clip = make_hlg(tmp_path / "hlg.mp4", 3.0)
    media = ac.prepare(clip, tmp_path / "work", max_seconds=60, names=["iPhone.MOV"])
    assert media["hdr"] == [True]
    tags = probe_tags(media["full"])
    assert (tags["pix_fmt"], tags["color_transfer"], tags["color_primaries"]) == ("yuv420p", "bt709", "bt709")


@needs_hlg
def test_if_the_conversion_filter_is_missing_the_clip_still_becomes_standard_video(tmp_path, monkeypatch):
    from tests.test_safe_media import make_hlg
    clip = make_hlg(tmp_path / "hlg.mp4", 3.0)
    real, used = ac.ffmpeg_to, []

    def flaky(dest, args, timeout=1800):
        used.append("zscale" in " ".join(args))
        if used[-1]:
            raise subprocess.CalledProcessError(1, "ffmpeg")        # as if this ffmpeg had no zscale filter
        return real(dest, args, timeout)
    monkeypatch.setattr(ac, "ffmpeg_to", flaky)
    media = ac.prepare(clip, tmp_path / "work", max_seconds=60)
    assert used[:2] == [True, False]
    assert probe_tags(media["full"])["color_transfer"] == "bt709"


def test_a_failing_ordinary_clip_is_not_retried_as_hdr(tmp_path, monkeypatch):
    clip = make_clip(tmp_path / "a.mp4", 2.0)

    def broken(dest, args, timeout=1800):
        raise subprocess.CalledProcessError(1, "ffmpeg")
    monkeypatch.setattr(ac, "ffmpeg_to", broken)
    with pytest.raises(subprocess.CalledProcessError):
        ac.prepare(clip, tmp_path / "work", max_seconds=60)
