"""Self-check: after the ads are rendered, Gemini watches each finished ad and scores it.

    reviews, notes, cost = review_ads(cfg, client, brief=..., entries=[...], work=...)

One call per ad, over the zero-retention route (the ad contains the person's footage). The scores are shown on the
page next to the ad and kept in the job's result. A check can never fail a job: any problem (the budget, a bad
answer, a failed conversion) becomes a note and the ad simply has no scorecard. Nothing here changes an ad; the
bounded re-plan that acts on a weak score is a later step.
"""
from __future__ import annotations

import base64
import logging
import math
import re
import subprocess
from pathlib import Path

import llm
from budget import BudgetExceeded
from safe_media import clean_env

log = logging.getLogger("ad-cutter")
HERE = Path(__file__).resolve().parent
AREAS = ("hook", "cuts", "story", "captions", "overlays", "request_fit", "compliance", "design")
MAX_PROBLEMS = 6
MAX_TEXT = 240                          # characters kept of each problem's description and fix
MAX_PROXY_BYTES = 12_000_000            # one ad's small copy; a longer or busier ad is skipped, not squeezed further
JOB_COST_CAP_USD = 0.30                 # most the self-checks of one job may cost, counting failed calls at their
                                        # worst case (config: review_job_cap_usd). Separate from step D's re-plan budget.
MAX_FAILURES_IN_A_ROW = 2               # failed calls in a row after which the rest of a job's checks are skipped
MAX_OUTPUT_TOKENS = 4000               # thinking tokens count here; the answer itself is under 800


def quote(text: str, limit: int) -> str:
    """Untrusted text for a prompt: one line, no markers that could close the data block, cut to `limit`."""
    flat = re.sub(r"[<>]{3,}", "", re.sub(r"\s+", " ", str(text or ""))).strip()
    return flat[:limit]


def make_proxy(video: Path, dest: Path) -> Path:
    """A small copy of a finished ad (480 px tall, 10 fps, mono 48k sound) for Gemini to watch."""
    tmp = dest.with_name(f"{dest.stem}.partial{dest.suffix}")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", "scale=-2:480", "-r", "10", "-c:v", "libx264",
                    "-preset", "fast", "-crf", "32", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k", "-ac", "1",
                    "-movflags", "+faststart", str(tmp)], check=True, timeout=300, env=clean_env(),
                   capture_output=True)
    tmp.replace(dest)
    return dest


def build_prompt(entry: dict, brief: str, references: str = "") -> str:
    ad, v = entry["ad"], entry.get("verify") or {}
    callouts = "; ".join(quote(c.get("text", "") if isinstance(c, dict) else c, 60) for c in ad.get("callouts", []))
    match = v.get("match")
    dz = entry.get("design") or {}
    look = ("the original look" if not dz.get("designed") else
            f"{dz.get('caption_style')} captions, {dz.get('headline_style')} headline, {dz.get('callout_style')} callouts, {dz.get('font')} font, "
            f"{dz.get('motion')} motion, {dz.get('end_style')} end screen, accent {dz.get('accent')}"
            + (f", {dz.get('cards_shown')} designed card(s)" if dz.get("cards_shown") else "") + ". It was chosen because: "
            + quote(dz.get("observations"), 300) + " " + quote(dz.get("why"), 200))
    return (HERE / "prompts" / "review_ads.md").read_text(encoding="utf-8").format(
        brief=quote(brief, 2000) or "(No request was given; Gemini used its own judgement.)", k=entry["k"],
        name=quote(ad.get("name"), 80), seconds=f"{float(entry.get('len') or 0):.0f}",
        funnel_stage=quote(ad.get("funnel_stage"), 40) or "not stated", angle=quote(ad.get("angle"), 300) or "not stated",
        headline=quote(ad.get("headline"), 120), callouts=callouts or "none",
        spoken=quote(entry.get("spoken"), 1500) or "(not available)",
        match=f"{match:.2f}" if isinstance(match, (int, float)) else "n/a", look=look,
        references=quote(references, 1600).replace("<<<", " ").replace(">>>", " ") or "(Nothing was provided.)")


def validate(raw, seconds: float) -> dict | None:
    """Gemini's answer, kept only if every score are whole numbers from 1 to 5. Problems naming an unknown area
    are dropped, times are held inside the ad, and every text is cut to length. None when the scores are unusable."""
    if not isinstance(raw, dict) or not isinstance(raw.get("scores"), dict):
        return None
    scores = {}
    for area in AREAS:
        value = raw["scores"].get(area)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
            return None
        scores[area] = value
    problems = []
    for p in raw.get("problems") if isinstance(raw.get("problems"), list) else []:
        if not isinstance(p, dict) or p.get("area") not in AREAS or not str(p.get("what") or "").strip():
            continue
        at = p.get("at_s")
        ok = isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at)
        at = min(max(float(at), 0.0), seconds) if ok else None
        problems.append({"at_s": None if at is None else round(at, 1), "area": p["area"],
                         "what": quote(p["what"], MAX_TEXT), "fix": quote(p.get("fix"), MAX_TEXT)})
        if len(problems) == MAX_PROBLEMS:
            break
    return {"scores": scores, "problems": problems, "verdict": quote(raw.get("verdict"), 400),
            "vs_references": quote(raw.get("vs_references"), 300)}


def needs_a_look(review: dict, speech_match) -> bool:
    """The plan's rule for a weak ad: it has a named problem and any of: cuts, captions or compliance at 2 or less,
    hook or design at 2 or less, an average of the original seven areas under 3.2, or captions that do not match the
    speech (under 0.85)."""
    s = review["scores"]
    core = [v for a, v in s.items() if a != "design"]
    weak = (min(s["cuts"], s["captions"], s["compliance"]) <= 2 or s["hook"] <= 2 or s.get("design", 5) <= 2
            or sum(core) / len(core) < 3.2
            or (isinstance(speech_match, (int, float)) and speech_match < 0.85))
    return bool(review["problems"]) and weak


def review_ads(cfg: dict, client: llm.OpenRouter, *, brief: str, entries: list[dict], work: Path,
               progress=lambda stage, detail="": None, references: str = "") -> tuple[dict[int, dict], list[str], float]:
    """Check every entry that has a rendered `video`. Returns ({ad number: review}, notes for the editor, cost).
    Never raises for a problem with the check itself; the Stop signal and other BaseExceptions pass through."""
    reviews: dict[int, dict] = {}
    notes: list[str] = []
    spent, failures, cap = 0.0, 0, float(cfg.get("review_job_cap_usd", JOB_COST_CAP_USD))
    todo = [e for e in entries if e.get("video")]
    for n, e in enumerate(todo, 1):
        k = e["k"]
        est = 0.0
        try:
            progress("checking", f"ad {n} of {len(todo)}")
            proxy = make_proxy(Path(e["video"]), work / f"review-ad{k}.mp4")
            size = proxy.stat().st_size
            if size > MAX_PROXY_BYTES:
                notes.append(f"Self-check: ad {k} was not checked (its small copy is {size / 1e6:.0f} MB).")
                continue
            prompt = build_prompt(e, brief, references)
            tokens = llm.video_tokens(float(e.get("len") or 0)) + len(prompt) // 3
            # the worst this one call can cost (one attempt, no retry): never start a call that could pass the cap
            est = llm.estimate_cost(cfg["plan_model"], tokens, MAX_OUTPUT_TOKENS)
            if spent + est > cap:
                notes.append(f"Self-check: stopped after ${spent:.2f}; the next check could cost up to ${est:.2f} and the "
                             f"limit is ${cap:.2f} a job. Ads {', '.join(str(x['k']) for x in todo[n - 1:])} were not checked.")
                break
            content = [{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,"
                                                                  + base64.b64encode(proxy.read_bytes()).decode("ascii")}},
                       {"type": "text", "text": prompt}]
            raw, usage = client.chat_json(cfg["plan_model"], content, route="zdr", label=f"check ad {k}",
                                          est_input_tokens=tokens, max_tokens=MAX_OUTPUT_TOKENS, reasoning="low",
                                          attempts=1, timeout=300)
            spent += float(usage.get("cost") or 0.0)
            failures = 0
            result = validate(raw, float(e.get("len") or 0))
            if result is None:
                notes.append(f"Self-check: Gemini's answer for ad {k} had no usable scores, so it has no scorecard.")
                continue
            result["look"] = needs_a_look(result, (e.get("verify") or {}).get("match"))
            reviews[k] = result
        except BudgetExceeded as err:                 # the budget will not recover within this job
            notes.append(f"Self-check: ad {k} was not checked ({str(err)[:200]}).")
            break
        except llm.LLMError as err:
            # a failed call may still have been billed: count it at its worst case. One bad reply (cut-off JSON) should
            # not silence the other ads, but a provider that fails twice running is down: stop for this job
            spent += est
            failures += 1
            notes.append(f"Self-check: ad {k} was not checked ({str(err)[:200]}).")
            if failures >= MAX_FAILURES_IN_A_ROW:
                notes.append("Self-check: stopped after two failed checks in a row; the remaining ads were not checked.")
                break
        except Exception as err:   # noqa: BLE001 - a failed check must never cost the person their ads
            log.warning("self-check of ad %s failed: %s", k, err)
            notes.append(f"Self-check: ad {k} was not checked ({type(err).__name__}).")
        finally:
            (work / f"review-ad{k}.mp4").unlink(missing_ok=True)
            (work / f"review-ad{k}.partial.mp4").unlink(missing_ok=True)
    return reviews, notes, round(spent, 4)
