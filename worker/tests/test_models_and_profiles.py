"""Model roles (Pro for references, Flash for footage), strict schemas, and the style-profile tier."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ad_cutter as ac  # noqa: E402
import budget  # noqa: E402
import design  # noqa: E402
import llm  # noqa: E402
import net  # noqa: E402
import styleprofile  # noqa: E402

PRO, FLASH = "google/gemini-3.1-pro-preview", "google/gemini-3.8-flash"
BRAND = {"name": "B", "product": "p", "audience": "a", "cta_line": "x", "cta_button": "y"}
YT = "https://www.youtube.com/watch?v=9TG1joKdSCY"


# ---------------------------------------------------------------- roles

def test_default_roles_follow_the_two_tier_layout():
    assert llm.model_for({}, "reference") == (PRO, "high")
    assert llm.model_for({}, "footage") == (FLASH, "medium")
    assert llm.model_for({}, "review") == (FLASH, "low")


def test_the_shipped_config_uses_the_layout_and_every_model_has_a_price():
    cfg = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8"))
    assert "plan_model" not in cfg and cfg["structured_output"] is True
    for role in ("reference", "footage", "review"):
        model, _ = llm.model_for(cfg, role)
        assert model in llm.PRICES
    assert llm.model_for(cfg, "reference")[0] == PRO and llm.model_for(cfg, "footage")[0] == FLASH


def test_a_role_can_be_overridden_and_an_old_plan_model_config_still_works():
    cfg = {"models": {"footage": {"model": "x/y", "reasoning": "low"}}}
    assert llm.model_for(cfg, "footage") == ("x/y", "low") and llm.model_for(cfg, "reference") == (PRO, "high")
    old = {"plan_model": "old/model"}
    assert llm.model_for(old, "footage")[0] == "old/model" and llm.model_for(old, "review")[0] == "old/model"
    assert llm.model_for(old, "reference")[0] == PRO         # the reference tier never silently downgrades


def test_flash_costs_less_than_pro_for_the_same_footage():
    tokens = llm.video_tokens(60)
    assert llm.estimate_cost(FLASH, tokens, 16000) < llm.estimate_cost(PRO, tokens, 16000) / 2


# ---------------------------------------------------------------- strict schema

class Transport:
    def __init__(self, replies):
        self.replies, self.posts = list(replies), []

    def __call__(self, method, url, headers=None, body=None, timeout=60):
        if method == "GET":
            return {"data": {"limit_remaining": 50.0}}
        self.posts.append(json.loads(json.dumps(body)))      # a snapshot: the client edits its body between tries
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def make_client(monkeypatch, tmp_path, replies):
    monkeypatch.setenv("TEST_VA_KEY", "sk-or-test")
    t = Transport(replies)
    return llm.OpenRouter("TEST_VA_KEY", budget.LocalLedger(tmp_path / "l.jsonl", 100.0), request=t,
                          sleep=lambda s: None), t


def good(cost=0.01):
    return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"cost": cost}}


def ask(client, **kw):
    return client.chat_json(FLASH, [{"type": "text", "text": "hi"}], est_input_tokens=1000, max_tokens=1000, **kw)


def test_the_schema_is_sent_as_strict_json_schema(monkeypatch, tmp_path):
    client, t = make_client(monkeypatch, tmp_path, [good()])
    ask(client, schema={"type": "object"}, schema_name="ad_plan")
    rf = t.posts[0]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True and rf["json_schema"]["name"] == "ad_plan"


def test_without_a_schema_plain_json_is_requested(monkeypatch, tmp_path):
    client, t = make_client(monkeypatch, tmp_path, [good()])
    ask(client)
    assert t.posts[0]["response_format"] == {"type": "json_object"}


def test_a_provider_that_refuses_the_schema_gets_plain_json_instead(monkeypatch, tmp_path):
    refusal = net.HttpError(400, '{"error":{"message":"response_format json_schema is not supported"}}')
    client, t = make_client(monkeypatch, tmp_path, [refusal, good(0.02)])
    parsed, usage = ask(client, schema={"type": "object"})
    assert parsed == {"ok": True} and usage["cost"] == 0.02
    assert [p["response_format"]["type"] for p in t.posts] == ["json_schema", "json_object"]


def test_other_400_errors_are_not_retried(monkeypatch, tmp_path):
    client, t = make_client(monkeypatch, tmp_path, [net.HttpError(400, "bad key")])
    with pytest.raises(llm.LLMError):
        ask(client, schema={"type": "object"})
    assert len(t.posts) == 1


def test_the_plan_schema_offers_exactly_the_choices_the_validator_accepts():
    sch = design.plan_schema()
    look = sch["properties"]["ads"]["items"]["properties"]["design"]["properties"]
    assert set(look["captions"]["properties"]["style"]["enum"]) == set(design.CAPTION_STYLES)
    assert set(look["headline"]["properties"]["style"]["enum"]) == set(design.HEADLINE_STYLES)
    assert set(look["callouts"]["properties"]["style"]["enum"]) == set(design.CALLOUT_STYLES)
    assert set(look["end_screen"]["properties"]["layout"]["enum"]) == set(design.END_LAYOUTS)
    assert set(look["pacing"]["enum"]) == set(design.PACING) and set(look["callout_motion"]["enum"]) == set(design.MOTIONS)
    ad = sch["properties"]["ads"]["items"]
    assert "design" in ad["required"] and "side" in ad["properties"]["callouts"]["items"]["properties"]
    json.dumps(sch)                                          # serialisable, no stray objects


def test_the_footage_planner_asks_flash_with_the_strict_schema(monkeypatch, tmp_path):
    seen = {}

    class Client:
        def chat_json(self, model, content, **kw):
            seen.update(model=model, **kw)
            return {}, {}

    proxy = tmp_path / "p.mp4"
    proxy.write_bytes(b"x")
    ac.call_gemini({"brand": BRAND}, Client(), "prompt", proxy, 30.0)
    assert seen["model"] == FLASH and seen["reasoning"] == "medium" and seen["route"] == "zdr"
    assert seen["schema"] == design.plan_schema()
    ac.call_gemini({"brand": BRAND, "structured_output": False}, Client(), "prompt", proxy, 30.0)
    assert seen["schema"] is None


# ---------------------------------------------------------------- style profiles

def test_a_profile_keeps_only_names_the_renderer_knows_and_clamps_numbers():
    prof, notes = styleprofile.sanitize({
        "summary": "Fast rap edits", "cadence": {"average_shot_seconds": 99, "cut_on_beat": 1, "b_roll_ratio": 5,
                                                  "zoom_punch": "constantly", "pace": "tight"},
        "captions": {"styles": ["impact_single", "comic_sans", 7], "case": ["upper"], "position": ["middle", "nope"]},
        "headline": {"styles": ["ribbon"]}, "callouts": {"styles": ["lasers"], "motions": ["pop", "teleport"]},
        "transitions": {"types": ["hard_cut", "whip_pan", "magic"]},
        "end_screen": {"layouts": ["big_question"], "tone": "playful"},
        "rules": ["cut every 1.4 s", "", 5, "x" * 400], "avoid": ["slow intros"]}, "Rap/Edit 1", YT)
    assert prof["name"] == "Rap_Edit_1" and prof["captions"]["styles"] == ["impact_single"]
    assert prof["captions"]["position"] == ["middle"] and prof["callouts"]["styles"] == []
    assert prof["callouts"]["motions"] == ["pop"] and prof["transitions"]["types"] == ["hard_cut", "whip_pan"]
    c = prof["cadence"]
    assert c["average_shot_seconds"] == 30 and c["b_roll_ratio"] == 1 and c["zoom_punch"] == "none"
    assert c["cut_on_beat"] is True and c["pace"] == "tight"
    assert len(prof["rules"]) == 2 and len(prof["rules"][1]) == 160
    assert any("whip_pan" in n for n in notes)


@pytest.mark.parametrize("raw", [None, {}, "x", 5, [], {"cadence": 5, "captions": "no", "rules": "no"}])
def test_a_garbage_profile_never_raises(raw):
    prof, notes = styleprofile.sanitize(raw, "x", "")
    assert prof["cadence"]["pace"] == "natural" and prof["rules"] == [] and notes or raw


def test_profile_schema_is_built_from_the_renderer_vocabulary():
    sch = styleprofile.profile_schema()
    assert set(sch["properties"]["captions"]["properties"]["styles"]["items"]["enum"]) == set(design.CAPTION_STYLES)
    assert set(sch["properties"]["end_screen"]["properties"]["layouts"]["items"]["enum"]) == set(design.END_LAYOUTS)
    assert set(sch["properties"]["cadence"]["properties"]["pace"]["enum"]) == set(design.PACING)


class FakeClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def chat_json(self, model, content, **kw):
        self.calls.append((model, content, kw))
        return self.reply, {"cost": 0.9}


REPLY = {"summary": "s", "cadence": {"average_shot_seconds": 1.4, "cut_on_beat": True, "b_roll_ratio": 0.4,
                                       "zoom_punch": "every_4_6_seconds", "pace": "tight"},
         "captions": {"styles": ["pill_all"], "case": ["upper"], "position": ["middle"], "notes": "n"},
         "headline": {"styles": ["tag"], "notes": ""}, "callouts": {"styles": ["banner"], "motions": ["slide"], "notes": ""},
         "transitions": {"types": ["hard_cut"], "notes": ""}, "end_screen": {"layouts": ["split_bar"], "tone": "direct"},
         "rules": ["cut every 1.4 s"], "avoid": []}


def test_a_reference_is_analysed_by_pro_with_deep_reasoning_over_the_youtube_route():
    client = FakeClient(REPLY)
    profile, notes, usage = styleprofile.analyze({}, client, YT, "rap_style", minutes=12)
    model, content, kw = client.calls[0]
    assert model == PRO and kw["reasoning"] == "high" and kw["route"] == "youtube"
    assert kw["schema"] == styleprofile.profile_schema() and kw["attempts"] == 1
    assert content[0]["video_url"]["url"] == YT and "split_bar" in content[1]["text"]
    assert profile["name"] == "rap_style" and profile["captions"]["styles"] == ["pill_all"] and usage["cost"] == 0.9


def test_a_local_reference_goes_zero_retention_and_bad_sources_are_refused(tmp_path):
    clip = tmp_path / "ref.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    client = FakeClient(REPLY)
    styleprofile.analyze({}, client, clip, "mine")
    assert client.calls[0][2]["route"] == "zdr" and client.calls[0][1][0]["video_url"]["url"].startswith("data:video/mp4")
    for bad in ("https://evil.example.com/x.mp4", "https://www.youtube.com/playlist?list=abc", str(tmp_path / "no.mp4")):
        with pytest.raises(llm.LLMError):
            styleprofile.analyze({}, client, bad, "x")
    big = tmp_path / "big.mp4"
    big.write_bytes(b"0" * 10)
    old = styleprofile.MAX_LOCAL_BYTES
    styleprofile.MAX_LOCAL_BYTES = 5
    try:
        with pytest.raises(llm.LLMError, match="make a copy"):
            styleprofile.analyze({}, client, big, "x")
    finally:
        styleprofile.MAX_LOCAL_BYTES = old


def test_the_cost_estimate_needs_no_network_and_grows_with_length():
    assert styleprofile.estimate({}, "x", 30) > styleprofile.estimate({}, "x", 5) > 0


def test_saved_profiles_reach_the_planning_prompt(tmp_path):
    prof, _ = styleprofile.sanitize(REPLY, "rap style", YT)
    path = styleprofile.save(prof, tmp_path)
    assert path.name == "rap_style.json" and json.loads(path.read_text(encoding="utf-8"))["name"] == "rap_style"
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    text = styleprofile.load_all(tmp_path)
    assert "rap_style" in text and "pill_all" in text and "cut every 1.4 s" in text and "average shot 1.4 s" in text
    assert styleprofile.load_all(tmp_path / "nowhere") == "(No style profiles yet.)"
    assert len(styleprofile.load_all(tmp_path, limit=20)) == 20


def test_the_prompt_carries_the_style_profiles_section():
    cfg = {"brand": BRAND, "ad_count": 2, "ad_min_seconds": 20, "ad_max_seconds": 75}
    p = ac.build_prompt(cfg, [{"w": "hi", "s": 0.0, "e": 0.3}])
    assert "## Style profiles" in p and "(No style profiles yet.)" in p
