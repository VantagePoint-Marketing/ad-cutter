"""The repair loop: plan, build, CHECK, then fix what the check found, and keep only fixes that really help.

After the first build and Gemini's self-check (review.py), each ad whose scorecard names a problem in a repairable area gets up to a
few more rounds. Code decides everything that matters; the AI only proposes the change:

  * WHAT is repairable. Cosmetic areas (captions, overlays, design) get a new DESIGN from the closed kit, chosen by a cheaper model that
    is shown stills from the finished ad. Structural areas (hook, cuts, story, request fit) get a reworked set of segments, headline
    and callouts from the planning model, working from the transcript. One kind of fix per round, structural first.
  * COMPLIANCE is never optimised. A compliance score of 2 or less is flagged for a person ("Needs a person") and never repaired by
    the loop; a structural rewrite is also refused if it adds a number the speaker did not say or a promise-style phrase.
  * KEEP THE BEST. A repaired ad replaces the earlier one only if every targeted area did not get worse and improved overall, no other
    area (compliance included) got worse, the new render is clean and its captions still match the speech. Otherwise the earlier
    render is put back and the new file deleted.
  * IT STOPS when no ad is left to repair, the rounds for this effort are used (Quick 1 = no repair, Balanced 2, Thorough 4), the job's
    total spend reaches the cap, the time limit passes, an ad has been tried twice for the same kind of fix, or the job is cancelled
    (progress() raises). Quick behaves exactly as before the loop existed.

`run_rounds` is pure orchestration over callables so it can be tested without any AI or video.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass, field

import design_kit
import llm
import models
from budget import BudgetExceeded
from safe_media import clean_env

log = logging.getLogger("ad-cutter")
ROUNDS = {"low": 1, "medium": 2, "high": 4}                 # the first build counts as round 1
SPEND_CAP = {"low": 0.0, "medium": 0.75, "high": 1.50}      # dollars of total job cost above which no new round starts
TIME_LIMIT = {"low": 0, "medium": 28 * 60, "high": 45 * 60}  # seconds from the start of the pipeline
COSMETIC = ("captions", "overlays", "design")
STRUCTURAL = ("hook", "cuts", "story", "request_fit")
WEAK = 3                                                    # an area at or below this score, with a named problem, is worth repairing
MAX_TRIES = 2                                               # per ad and kind of fix, accepted or not
PHRASES = {"cosmetic": "polishing the captions and overlays", "structural": "reworking the hook and story"}
# promise-style wording the loop never introduces on its own (the review may still flag existing claims for a person)
RISKY = re.compile(r"guarantee|risk[- ]?free|can't lose|cannot lose|no risk|double your|triple your|get rich|easy money|100%|never lose", re.I)


def targets(review: dict) -> dict[str, list[str]]:
    """The areas worth repairing, by kind: scored WEAK or lower AND named in at least one problem. Compliance is never a target."""
    scores = review.get("scores") or {}
    named = {p.get("area") for p in review.get("problems") or []}
    return {"structural": [a for a in STRUCTURAL if scores.get(a, 5) <= WEAK and a in named],
            "cosmetic": [a for a in COSMETIC if scores.get(a, 5) <= WEAK and a in named]}


def needs_a_person(review: dict) -> bool:
    return int((review.get("scores") or {}).get("compliance", 5)) <= 2


def choose(entry: dict) -> tuple[str, list[str]] | None:
    """The next kind of fix for this ad and the areas it targets, or None. Structural first; each kind at most MAX_TRIES times."""
    review = entry.get("review")
    if not review or not entry.get("file") or entry.get("error"):
        return None
    tried: dict[str, int] = {}
    for h in entry.get("history") or []:
        tried[h["scope"]] = tried.get(h["scope"], 0) + 1
    want = targets(review)
    for scope in ("structural", "cosmetic"):
        if want[scope] and tried.get(scope, 0) < MAX_TRIES:
            return scope, want[scope]
    return None


def accept(old: dict, new: dict | None, areas: list[str]) -> tuple[bool, str]:
    """Is the repaired ad's scorecard good enough to replace the earlier one? Nothing may get worse and the targeted areas must improve."""
    if not new:
        return False, "the repaired ad could not be checked"
    o, n = old["scores"], new["scores"]
    worse = [a for a in o if n.get(a, 0) < o[a]]
    if worse:
        return False, "it made " + ", ".join(a.replace("_", " ") for a in worse) + " worse"
    if sum(n[a] for a in areas) <= sum(o[a] for a in areas):
        return False, "the targeted areas did not improve"
    return True, "improved " + ", ".join(f"{a.replace('_', ' ')} {o[a]}→{n[a]}" for a in areas if n[a] > o[a])


# words that are numbers or money talk: the loop may use them only if the speaker said them in this ad
NUMBER_WORDS = set(design_kit.SPELLED) | {"thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million",
                                          "billion", "percent", "percentage", "dozen", "double", "triple", "tenfold"}
MONEY_WORDS = {"profit", "profits", "profitable", "returns", "earn", "earnings", "income", "rich", "wealth", "wealthy", "millionaire", "money", "cash",
               "dollar", "dollars", "gains", "winning", "winners", "payout", "riches"}


def unsafe_text(ad: dict, spoken: set[str], original_primary: str | None = None) -> str | None:
    """Why a reworked ad's text must be refused, or None. Covers the headline, the callouts and the Meta copy: a promise-style phrase is
    never allowed; a number (digits or spelled out) or a money word is allowed only if the speaker said it. The Meta copy is checked only
    when it differs from `original_primary` (copy the loop did not change was never at risk from the loop)."""
    texts = [str(ad.get("headline", ""))] + [str(c.get("text", "")) for c in ad.get("callouts", []) if isinstance(c, dict)]
    if original_primary is None or str(ad.get("primary_text", "")) != str(original_primary):
        texts.append(str(ad.get("primary_text", "")))
    for t in texts:
        if RISKY.search(t):
            return f"it used promise-style wording ({t[:40]!r})"
        for tok in design_kit.tokens(t):
            if tok in spoken:
                continue
            if any(ch.isdigit() for ch in tok):
                return f"it used a number the speaker did not say ({tok})"
            if tok in NUMBER_WORDS:
                return f"it used a number word the speaker did not say ({tok})"
            if tok in MONEY_WORDS:
                return f"it used money wording the speaker did not say ({tok})"
    return None


@dataclass
class Context:
    effort: str
    repair: Callable[[dict, dict, str, list[str]], tuple[dict | None, float, str]]     # entry, review, scope, areas -> (new ad, cost, what)
    rebuild: Callable[[int, dict], dict]                                                # k, new ad -> a rendered entry
    review_one: Callable[[dict], tuple[dict | None, float]]                             # entry -> (review, cost)
    snapshot: Callable[[int], object]                                                   # keep the current render before a rebuild
    restore: Callable[[int, object], None]                                              # put it back
    discard: Callable[[dict], None]                                                     # delete an entry's output file
    commit: Callable[[int, dict], None]                                                 # the accepted ad becomes the plan's ad
    progress: Callable[..., None] = lambda stage, detail="": None
    loop_spent: float = 0.0                                                             # what the loop itself has spent (kept even if it stops early)
    spent: Callable[[], float] = lambda: 0.0                                            # total job cost so far, retries included
    now: Callable[[], float] = time.monotonic
    started: float = field(default_factory=time.monotonic)


def run_rounds(entries: list[dict], ctx: Context) -> tuple[float, list[str]]:
    """Run the repair rounds over the built, reviewed entries (changed in place). Returns (dollars spent here, notes for the review notes)."""
    total, notes = 0.0, []
    rounds, cap, limit = ROUNDS.get(ctx.effort, 1), SPEND_CAP.get(ctx.effort, 0.0), TIME_LIMIT.get(ctx.effort, 0)
    for e in entries:
        e.setdefault("history", [])
        if e.get("review") and needs_a_person(e["review"]):
            e["needs_person"] = True
            notes.append(f"Ad {e['k']}: needs a person. The self-check scored compliance {e['review']['scores']['compliance']}/5; the repair loop "
                         "never tries to fix compliance by itself.")
    stop = None
    for rnd in range(2, rounds + 1):
        todo = [(e, choose(e)) for e in entries]
        todo = [(e, c) for e, c in todo if c]
        if not todo:
            break
        for e, (scope, areas) in todo:
            if ctx.spent() + total >= cap:
                stop = f"the spending limit (${cap:.2f}) was reached"
                break
            if ctx.now() - ctx.started >= limit:
                stop = f"the time limit ({limit // 60} minutes) passed"
                break
            k = e["k"]
            ctx.progress("repairing", f"round {rnd} of {rounds}: ad {k}, {PHRASES[scope]}")
            step = {"round": rnd, "scope": scope, "areas": areas, "what": "", "accepted": False, "why": ""}
            e["history"].append(step)
            new_ad, cost, what = ctx.repair(e, e["review"], scope, areas)
            total += cost
            ctx.loop_spent = total
            step["what"] = what
            if new_ad is None:
                step["why"] = "no usable change was proposed"
                continue
            token = ctx.snapshot(k)
            try:
                new_e = ctx.rebuild(k, new_ad)
            except Exception as err:                       # noqa: BLE001 - a failed rebuild must never cost the ad its earlier version
                ctx.restore(k, token)
                step["why"] = f"the rebuild failed ({type(err).__name__})"
                continue
            if new_e.get("error") or not new_e.get("file") or not str(new_e.get("check", "")).startswith("passed"):
                ctx.restore(k, token)
                ctx.discard(new_e)
                step["why"] = "the rebuilt ad did not render cleanly"
                continue
            match_old, match_new = (e.get("verify") or {}).get("match"), (new_e.get("verify") or {}).get("match")
            if isinstance(match_new, (int, float)) and match_new < 0.85 and (not isinstance(match_old, (int, float)) or match_new < match_old):
                ctx.restore(k, token)
                ctx.discard(new_e)
                step["why"] = "the new captions no longer matched the speech"
                continue
            ctx.progress("repairing", f"round {rnd} of {rounds}: ad {k}, checking the change")
            new_review, rcost = ctx.review_one(new_e)
            total += rcost
            ctx.loop_spent = total
            ok, why = accept(e["review"], new_review, areas)
            step["why"] = why
            if ok:
                old_file, history = dict(e), e["history"]
                ctx.discard(old_file)
                e.clear()
                e.update(new_e)
                e["review"], e["history"] = new_review, history
                if new_review and needs_a_person(new_review):
                    e["needs_person"] = True
                step["accepted"] = True
                ctx.commit(k, new_ad)
            else:
                ctx.restore(k, token)
                ctx.discard(new_e)
        if stop:
            break
    for e in entries:
        for h in e.get("history", []):
            notes.append(f"Ad {e['k']}, round {h['round']} ({h['scope']}): " + ("kept: " if h["accepted"] else "not kept: ") + h["why"]
                         + (f" ({h['what']})" if h["what"] else "") + ".")
    if stop:
        notes.append(f"The repair loop stopped because {stop}.")
    return total, notes


# ---------------------------------------------------------------- the AI side: proposing a change

HERE = Path(__file__).resolve().parent
MAX_FRAMES = 3
FRAME_WIDTH = 540


def problems_text(review: dict, areas: list[str]) -> str:
    """The review's problems as plain lines, those in the targeted areas first. Quoted data for the repair prompt."""
    rows = sorted(review.get("problems") or [], key=lambda p: (p.get("area") not in areas, p.get("at_s") is None, p.get("at_s") or 0))
    lines = [f"- {'at ' + format(p['at_s'], '.1f') + ' s, ' if p.get('at_s') is not None else ''}{p.get('area', '').replace('_', ' ')}: "
             f"{design_kit.clean_text(p.get('what'), 300)}" + (f" Suggested fix: {design_kit.clean_text(p.get('fix'), 300)}" if p.get("fix") else "")
             for p in rows]
    return "\n".join(lines) or "(none named)"


def frame_times(review: dict, areas: list[str], seconds: float) -> list[float]:
    """Where to look at the finished ad: the times of the targeted problems, else spread through the ad. At most MAX_FRAMES, inside the video."""
    times: list[float] = []
    for p in review.get("problems") or []:
        t = p.get("at_s")
        if p.get("area") in areas and isinstance(t, (int, float)) and 0 <= t < seconds and all(abs(t - x) > 1.0 for x in times):
            times.append(float(t))
    for frac in (0.12, 0.5, 0.85):
        if len(times) >= MAX_FRAMES:
            break
        t = round(seconds * frac, 1)
        if all(abs(t - x) > 1.0 for x in times):
            times.append(t)
    return sorted(times[:MAX_FRAMES])


def extract_frames(video: Path, times: list[float], work: Path) -> list[bytes]:
    out = []
    for i, t in enumerate(times):
        dest = work / f"repair-frame{i}.jpg"
        try:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(t, 0):.2f}", "-i", str(video), "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2",
                            "-q:v", "5", str(dest)], check=True, timeout=60, env=clean_env(), capture_output=True)
            out.append(dest.read_bytes())
        except (subprocess.SubprocessError, OSError) as err:
            log.warning("repair: could not take a frame at %.1fs (%s)", t, type(err).__name__)
        finally:
            dest.unlink(missing_ok=True)
    return out


def describe_change(old: dict, new: dict) -> str:
    names = {"caption_style": "caption style", "caption_y": "caption position", "headline_style": "headline style", "callout_style": "callout style",
             "callout_zone": "callout position", "end_style": "end screen", "font": "font", "motion": "motion", "speaker_position": "speaker position"}
    parts = [f"{label} {old.get(k)} → {new.get(k)}" for k, label in names.items() if old.get(k) != new.get(k)]
    if old.get("palette") != new.get("palette"):
        parts.append("colours")
    if len(old.get("cards") or []) != len(new.get("cards") or []):
        parts.append(f"cards {len(old.get('cards') or [])} → {len(new.get('cards') or [])}")
    return ", ".join(parts)


class Repairer:
    """Proposes a repaired ad. `validate_plan(plan) -> (plan, notes)` is ad_cutter.validate_plan; the transcript and clip lines are
    ready-made prompt text. Never raises for an AI or validation problem: it returns (None, cost, reason) and the loop moves on."""

    def __init__(self, cfg: dict, client, words: list[dict], transcript: str, clips_text: str, validate_plan, work: Path, brief: str = ""):
        self.cfg, self.client, self.words, self.transcript, self.clips_text = cfg, client, words, transcript, clips_text
        self.validate_plan, self.work, self.brief = validate_plan, work, brief
        self.pending_est = 0.0

    def __call__(self, entry: dict, review: dict, scope: str, areas: list[str]) -> tuple[dict | None, float, str]:
        self.pending_est = 0.0
        try:
            return self._cosmetic(entry, review, areas) if scope == "cosmetic" else self._structural(entry, review, areas)
        except (llm.LLMError, BudgetExceeded) as err:
            # a failed call may still have been billed: count it at its worst case (a refused budget never reached the provider)
            return None, 0.0 if isinstance(err, BudgetExceeded) else self.pending_est, f"the AI call failed: {str(err)[:80]}"

    def _json(self, model: str, content: list[dict], label: str, est_tokens: int, reasoning: str) -> tuple[dict, float]:
        self.pending_est = llm.estimate_cost(model, est_tokens, 6000)
        raw, usage = self.client.chat_json(model, content, route="zdr", label=label, est_input_tokens=est_tokens, max_tokens=6000,
                                           reasoning=reasoning, attempts=1, timeout=240)
        self.pending_est = 0.0
        return raw, float(usage.get("cost") or 0.0)

    def _cosmetic(self, entry: dict, review: dict, areas: list[str]) -> tuple[dict | None, float, str]:
        ad, k = entry["ad"], entry["k"]
        old = ad.get("design") or dict(design_kit.CLASSIC)
        frames = extract_frames(self.work / f"ad{k}" / "render.mp4", frame_times(review, areas, float(entry.get("len") or 30)), self.work)
        prompt = (HERE / "prompts" / "repair_design.md").read_text(encoding="utf-8").format(
            scores=", ".join(f"{a.replace('_', ' ')} {v}" for a, v in review["scores"].items()), problems=problems_text(review, areas),
            design=json.dumps({key: old.get(key) for key in design_kit.CLASSIC}, ensure_ascii=False), options=design_kit.options_text())
        content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(f).decode("ascii")}} for f in frames]
        content.append({"type": "text", "text": prompt})
        model = self.cfg.get("repair_model") or models.MODELS["efficient"]["openrouter"]
        raw, cost = self._json(model, content, f"repair look of ad {k}", len(prompt) // 3 + 1500 * len(frames), "low")
        brand = str((self.cfg.get("brand") or {}).get("name", ""))
        design, _notes = design_kit.validate_design(raw.get("design") if isinstance(raw, dict) else None, ad, self.words, brand)
        if design is None:
            return None, cost, "the new look had no observation or reason"
        what = describe_change(old, design)
        if not what:
            return None, cost, "it proposed no change"
        return {**ad, "design": design}, cost, what

    def _structural(self, entry: dict, review: dict, areas: list[str]) -> tuple[dict | None, float, str]:
        ad, k = entry["ad"], entry["k"]
        current = {key: ad.get(key) for key in ("name", "funnel_stage", "angle", "headline", "segments", "callouts", "primary_text")}
        prompt = (HERE / "prompts" / "repair_plan.md").read_text(encoding="utf-8").format(
            scores=", ".join(f"{a.replace('_', ' ')} {v}" for a, v in review["scores"].items()), problems=problems_text(review, areas),
            ad=json.dumps(current, ensure_ascii=False, indent=1), brief=design_kit.clean_text(self.brief, 1500) or "(No request was given.)",
            clips=self.clips_text, transcript=self.transcript, min_seconds=self.cfg["ad_min_seconds"], max_seconds=self.cfg["ad_max_seconds"],
            name=design_kit.clean_text(ad.get("name"), 80).replace("{", "").replace("}", ""))
        raw, cost = self._json(self.cfg["plan_model"], [{"type": "text", "text": prompt}], f"rework ad {k}", len(prompt) // 3, "medium")
        new = raw.get("ad") if isinstance(raw, dict) else None
        if not isinstance(new, dict):
            return None, cost, "the reworked ad was not usable"
        # the Meta copy (primary_text) is never rewritten by the loop: the self-check cannot see it, so nobody would check what changed
        candidate = {**{key: new.get(key) for key in current}, "name": ad["name"], "primary_text": ad.get("primary_text", ""), "design": ad.get("design")}
        try:
            plan, notes = self.validate_plan({"ads": [candidate]}, self.words, self.cfg, None)
        except Exception as err:                           # noqa: BLE001 - AdCutterError and friends: the rework is simply not usable
            return None, cost, f"the reworked ad failed its checks ({str(err)[:80]})"
        fixed = plan["ads"][0]
        spoken = design_kit.spoken_tokens([self.words[i]["w"] for s in fixed["segments"] for i in range(s["from"], s["to"] + 1)])
        why = unsafe_text(fixed, spoken, ad.get("primary_text", ""))
        if why:
            return None, cost, f"refused: {why}"
        if fixed["segments"] == ad["segments"] and fixed["headline"] == ad["headline"] and fixed["callouts"] == ad["callouts"]:
            return None, cost, "it proposed no change"
        changed = [label for key, label in (("segments", "the cut"), ("headline", "the headline"), ("callouts", "the callouts")) if fixed[key] != ad[key]]
        return fixed, cost, "changed " + ", ".join(changed)
