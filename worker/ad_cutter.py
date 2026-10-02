"""Ad cutter: raw talking-head clips + a request -> ready-to-review Meta ad cuts.

    python ad_cutter.py CLIP [CLIP ...]                     plan and render every ad (clips are joined in order)
    python ad_cutter.py CLIP --brief "3 short cold ads"     tell Gemini what you want
    python ad_cutter.py CLIP --replan                       ask Gemini for a fresh plan
    python ad_cutter.py CLIP --only 2                       render only ad 2
    python ad_cutter.py CLIP --no-render                    plan and build, skip the slow render

Steps:
  1. ffmpeg joins the clips into one 1080x1920 working copy, plus 16 kHz audio and a small proxy for Gemini.
  2. Whisper (on this PC) transcribes with word timings.
  3. Gemini Pro (OpenRouter, zero-retention endpoints) watches the proxy, reads the numbered transcript and the
     person's request, and plans the ads: how many, segments, headline, callouts, caption fixes, claims to
     review. It refers to words by index only, so it cannot invent timestamps.
  4. Every cut edge is re-transcribed locally and snapped to the quietest point between words; long pauses
     are trimmed; audio is levelled to -14 LUFS; the last frame is held for the CTA card.
  5. HyperFrames renders captions, headline, callouts and CTA. Each finished ad is transcribed again and
     compared with its captions; results go into "Review Notes.md" next to the videos.

Nothing is published or uploaded anywhere except the proxy video sent to Gemini through OpenRouter.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import difflib
import html
import json
import logging
import os
import re
import shutil
import string
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import llm
import review
from budget import BudgetExceeded, LocalLedger
from llm import extract_json  # noqa: F401  (kept importable from here for existing callers and tests)
from safe_media import UnsafeMedia, check_upload, clean_env, ffmpeg_input

HERE = Path(__file__).resolve().parent
FPS = 30
PAUSE_MIN = 0.5      # silences longer than this are trimmed...
PAUSE_KEEP = 0.12    # ...down to this much air on each side
FILLER_STARTS = {"but", "because", "so", "and", "okay", "um", "uh", "like"}
AD_SECONDS_SANE = (5.0, 120.0)   # spoken length outside this band gets a note; the request sets the real target
MAX_BRIEF = 2000                 # characters of the request that reach Gemini
log = logging.getLogger("ad-cutter")


class AdCutterError(RuntimeError):
    pass


# ---------------------------------------------------------------- config & tools

def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key in ("output_dir", "work_dir", "hyperframes_dir"):
        cfg[key] = os.path.expandvars(cfg[key])
    return cfg


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("env", clean_env())      # child processes never see API keys or credentials
    return subprocess.run(cmd, check=True, **kw)


def snap(t: float) -> float:
    return round(t * FPS) / FPS


# ---------------------------------------------------------------- 1. prepare media

def ffmpeg_to(dest: Path, args: list[str], timeout: float = 30 * 60) -> None:
    """Run ffmpeg into a temp file and rename it into place, so an interrupted run never leaves a
    half-written file that a later run would trust."""
    tmp = dest.with_name(f"{dest.stem}.partial{dest.suffix}")
    try:
        run(["ffmpeg", "-v", "error", "-y", *args, str(tmp)], timeout=timeout)
    except subprocess.TimeoutExpired as err:
        raise AdCutterError(f"ffmpeg took over {timeout / 60:.0f} minutes making {dest.name} and was stopped") from err
    tmp.replace(dest)


def hdr_to_sdr(transfer: str, tonemap: bool = True) -> str:
    """The filters that turn an HDR clip (iPhone HDR is HLG, some cameras write PQ) into ordinary video, as one
    comma-separated chain ending in 8-bit yuv420p. The render tool takes a completely different, memory-hungry HDR
    path (about 16 GB for a 30 second ad) when it sees HDR tags, so nothing HDR may reach it. `tonemap` does it
    properly (linear light, gamut and tone mapping); without it the pixels are kept and only the tags are changed,
    a flatter look that needs no zscale filter."""
    if tonemap:
        return (f"zscale=tin={transfer}:min=2020_ncl:pin=2020:rin=limited:t=linear:npl=100,format=gbrpf32le,"
                "zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    return "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv,format=yuv420p"


def working_copy_args(srcs: list[Path], demuxers: list[str], seconds: list[float], hdr: list[str | None] | None = None,
                      tonemap: bool = True) -> list[str]:
    """One ffmpeg pass: every clip is scaled and cropped to 1080x1920 at 30 fps with 48 kHz stereo sound, and the
    clips are joined in order. Each input keeps its forced demuxer and protocol whitelist, and -t caps it at its
    own checked length (plus a frame or two), so a header that understates a file's length cannot stretch the job
    past the footage limit. The sample format, rate and layout are set explicitly on every audio branch: concat
    needs them equal, and ffmpeg 5.1 (the worker image) will not guess them."""
    args, chain, labels = [], [], []
    for n, (src, demuxer, secs) in enumerate(zip(srcs, demuxers, seconds)):
        args += [*ffmpeg_input(demuxer), "-t", f"{secs + 0.25:.3f}", "-i", str(src)]
        chain.append(f"[{n}:v]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,"
                     f"setsar=1,fps={FPS},{hdr_to_sdr(hdr[n], tonemap) if hdr and hdr[n] else 'format=yuv420p'}[v{n}]")
        chain.append(f"[{n}:a]aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{n}]")
        labels.append(f"[v{n}][a{n}]")
    chain.append(f"{''.join(labels)}concat=n={len(srcs)}:v=1:a=1[v][a]")
    tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"] \
        if hdr and any(hdr) else []                  # the result must say it is ordinary video
    return [*args, "-filter_complex", ";".join(chain), "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset",
            "fast", "-crf", "17", "-g", str(FPS), *tags, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart"]


def prepare(srcs: Path | list[Path], work: Path, max_seconds: float = 600.0, names: list[str] | None = None) -> dict:
    """Working copy (1080x1920, 30 fps, stereo), 16 kHz mono wav, and a small proxy for Gemini. Several clips are
    joined in the order given, so the rest of the pipeline sees one video and one transcript. Every source is
    treated as hostile: its container is checked before ffmpeg opens it, and only the working copy (which we
    made) is used after that. Returns the files, the total duration and where each clip starts."""
    srcs = [Path(s) for s in (srcs if isinstance(srcs, (list, tuple)) else [srcs])]
    names = list(names or [s.name for s in srcs])
    work.mkdir(parents=True, exist_ok=True)
    full, wav, proxy = work / "source_1080x1920.mp4", work / "audio16k.wav", work / "proxy.mp4"
    infos = []
    for n, src in enumerate(srcs):
        try:
            infos.append(check_upload(src, max_seconds=max_seconds))
        except UnsafeMedia as err:
            which = "this video" if len(srcs) == 1 else f"clip {n + 1} ({names[n]})"
            raise AdCutterError(f"can't use {which}: {err}") from err
    seconds = [min(i.duration, max_seconds) for i in infos]
    total = sum(seconds)
    if total > max_seconds:
        raise AdCutterError(f"the clips add up to {total / 60:.1f} minutes; the limit is {max_seconds / 60:.0f} minutes "
                            "of footage per job")
    if not full.exists():
        log.info("making the 1080x1920 working copy from %s clip(s)", len(srcs))
        demuxers, hdrs = [i.demuxer for i in infos], [i.hdr for i in infos]
        slow = 3600 if any(hdrs) else 30 * 60
        try:
            ffmpeg_to(full, working_copy_args(srcs, demuxers, seconds, hdrs), slow)
        except subprocess.CalledProcessError:
            if not any(hdrs):
                raise
            log.warning("the HDR-to-standard conversion failed; keeping the pixels and changing only the colour tags")
            ffmpeg_to(full, working_copy_args(srcs, demuxers, seconds, hdrs, tonemap=False), slow)
    if not wav.exists():
        ffmpeg_to(wav, ["-i", str(full), "-vn", "-ac", "1", "-ar", "16000"])
    if not proxy.exists():
        for height, crf in ((640, 32), (480, 36)):
            ffmpeg_to(proxy, ["-i", str(full), "-vf", f"scale=-2:{height}", "-r", "10", "-c:v", "libx264",
                              "-preset", "medium", "-crf", str(crf), "-c:a", "aac", "-b:a", "48k", "-ac", "1"])
            if proxy.stat().st_size < 18_000_000:
                break
            proxy.unlink()
        else:
            raise AdCutterError("the footage is too long for Gemini planning (proxy over 18 MB); use less of it")
    duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(full)], capture_output=True, text=True, check=True,
                                    env=clean_env()).stdout.strip())
    clips, start = [], 0.0
    for name, secs in zip(names, seconds):
        clips.append({"name": name, "start": round(start, 3), "seconds": round(secs, 3)})
        start += secs
    return {"full": full, "wav": wav, "proxy": proxy, "duration": duration, "clips": clips,
            "hdr": [c.hdr is not None for c in infos]}


# ---------------------------------------------------------------- 2. transcribe

_WHISPER = None


def whisper(cfg: dict):
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel
        _WHISPER = WhisperModel(cfg["whisper_model"], device="cpu", compute_type="int8")
    return _WHISPER


def clean_token(tok: str) -> str:
    return tok.strip().strip(",.?!;:\"")


def merge_percent(words: list[dict]) -> list[dict]:
    """Whisper writes '75%' as '75' + '%'; join them so word indexes match what is said."""
    out: list[dict] = []
    for w in words:
        if w["w"] == "%" and out and re.fullmatch(r"[\d.,]+", out[-1]["w"]):
            out[-1] = {"w": out[-1]["w"] + "%", "s": out[-1]["s"], "e": w["e"]}
        elif w["w"]:
            out.append(w)
    return out


def transcribe(cfg: dict, wav: Path, cache: Path) -> list[dict]:
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    log.info("transcribing with Whisper %s", cfg["whisper_model"])
    segs, _ = whisper(cfg).transcribe(str(wav), word_timestamps=True, beam_size=5, vad_filter=True,
                                      vad_parameters={"min_silence_duration_ms": 300},
                                      condition_on_previous_text=False)
    words = merge_percent([{"w": clean_token(w.word), "s": round(w.start, 3), "e": round(w.end, 3)}
                           for s in segs for w in s.words])
    cache.write_text(json.dumps(words), encoding="utf-8")
    return words


def transcribe_clip(cfg: dict, wav: Path, t0: float, t1: float, work: Path) -> list[dict]:
    """Word timings for a short window; much more precise at edges than the long-form pass."""
    clip = work / "edge.wav"
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0, t0):.3f}", "-to", f"{t1:.3f}", "-i", str(wav), str(clip)])
    segs, _ = whisper(cfg).transcribe(str(clip), word_timestamps=True, beam_size=5, condition_on_previous_text=False)
    off = max(0.0, t0)
    return merge_percent([{"w": clean_token(w.word), "s": off + w.start, "e": off + w.end}
                          for s in segs for w in s.words])


# ---------------------------------------------------------------- audio energy

class Energy:
    """10 ms loudness profile of a 16 kHz mono wav, with a speech/silence threshold set from the recording."""

    HOP = 0.01

    def __init__(self, db: np.ndarray):
        self.db = db
        if not db.size:
            raise AdCutterError("the video has no audio to work from")
        floor = float(np.percentile(db, 10))
        voiced = db[db > floor + 6]           # judge speech level on speech only, however much silence there is
        speech = float(np.percentile(voiced, 90)) if voiced.size else floor + 20
        self.threshold = floor + 0.35 * (speech - floor)

    @classmethod
    def from_wav(cls, path: Path) -> "Energy":
        with wave.open(str(path)) as w:
            sr = w.getframerate()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)
        hop = int(sr * cls.HOP)
        n = len(a) // hop
        frames = a[: n * hop].reshape(n, hop)
        rms = np.sqrt((frames ** 2).mean(axis=1))
        return cls(20 * np.log10(np.maximum(rms, 1.0) / 32768))

    def _idx(self, t: float) -> int:
        return int(np.clip(round(t / self.HOP), 0, len(self.db)))

    def quietest(self, t0: float, t1: float) -> float:
        i0 = min(self._idx(t0), len(self.db) - 1)
        i1 = max(self._idx(t1), i0 + 1)
        return (i0 + int(np.argmin(self.db[i0:i1]))) * self.HOP

    def gaps(self, t0: float, t1: float, min_len: float) -> list[tuple[float, float]]:
        """Silent runs of at least min_len inside [t0, t1]; blips of 30 ms or less don't break a run."""
        i0, i1 = self._idx(t0), self._idx(t1)
        quiet = self.db[i0:i1] < self.threshold
        runs, start, loud = [], None, 0
        for k, q in enumerate(list(quiet) + [False] * 4):
            if q:
                start = k if start is None else start
                loud = 0
            elif start is not None:
                loud += 1
                if loud > 3:
                    end = k - loud + 1
                    if (end - start) * self.HOP >= min_len:
                        runs.append(((i0 + start) * self.HOP, (i0 + end) * self.HOP))
                    start, loud = None, 0
        return runs


# ---------------------------------------------------------------- 3. plan with Gemini

def transcript_for_prompt(words: list[dict]) -> str:
    lines, cur = [], []
    for i, w in enumerate(words):
        if not cur:
            cur.append(f"[{w['s']:.1f}s]")
        cur.append(f"#{i} {w['w']}")
        if len(cur) > 14 or w["w"].endswith((".", "?", "!")):
            lines.append(" ".join(cur))
            cur = []
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)


def clip_lines(clips: list[dict], words: list[dict]) -> str:
    """One line per clip for the prompt: its name, its time span and the words heard in it."""
    lines = []
    for n, c in enumerate(clips):
        start, end = float(c["start"]), float(c["start"]) + float(c["seconds"])
        idx = [i for i, w in enumerate(words) if start <= w["s"] < end]
        span = f"words #{idx[0]} to #{idx[-1]}" if idx else "no words heard"
        lines.append(f"- Clip {n + 1}: {c['name']}, {fmt_time(start)} to {fmt_time(end)} ({span})")
    return "\n".join(lines)


def build_prompt(cfg: dict, words: list[dict], brief: str = "", clips: list[dict] | None = None,
                 team_notes: str = "") -> str:
    b = cfg["brand"]
    clips = clips or [{"name": "the video", "start": 0.0, "seconds": words[-1]["e"] if words else 0.0}]
    brief = brief.strip()[:MAX_BRIEF]
    return (HERE / "prompts" / "plan_ads.md").read_text(encoding="utf-8").format(
        brief=brief or "(No request was given. Use your judgement and the defaults.)",
        team_notes=team_notes.strip() or "(Nothing yet.)",
        clip_count_text="one clip" if len(clips) == 1 else f"{len(clips)} clips joined in order",
        clips=clip_lines(clips, words), ad_count=cfg["ad_count"], max_ad_count=cfg.get("ad_count_max", 6),
        brand_name=b["name"], brand_product=b["product"], audience=b["audience"],
        min_seconds=cfg["ad_min_seconds"], max_seconds=cfg["ad_max_seconds"], transcript=transcript_for_prompt(words))


def call_gemini(cfg: dict, client: llm.OpenRouter, prompt: str, proxy: Path, duration: float) -> tuple[dict, dict]:
    """Gemini watches the proxy and plans the ads. Raw footage always goes over the zero-retention route."""
    content = [
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,"
                                                  + base64.b64encode(proxy.read_bytes()).decode("ascii")}},
        {"type": "text", "text": prompt},
    ]
    try:
        return client.chat_json(cfg["plan_model"], content, route="zdr", label="plan ads",
                                est_input_tokens=llm.video_tokens(duration) + len(prompt) // 3, max_tokens=16000)
    except (llm.LLMError, BudgetExceeded) as err:
        raise AdCutterError(f"Gemini planning: {err}") from err


def clip_of(t: float, clips: list[dict]) -> int:
    """Which clip (1-based) a source time falls in; the last one for times past the end."""
    for n, c in enumerate(clips, 1):
        if t < float(c["start"]) + float(c["seconds"]):
            return n
    return len(clips)


def validate_plan(plan: dict, words: list[dict], cfg: dict, clips: list[dict] | None = None) -> tuple[dict, list[str]]:
    """Check Gemini's plan against the transcript. Fixable problems are repaired and reported."""
    n, notes = len(words), []
    if not isinstance(plan, dict):
        raise AdCutterError("Gemini's plan is not a JSON object")

    def is_idx(v) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < n

    def in_range(r) -> bool:
        return isinstance(r, dict) and is_idx(r.get("from")) and is_idx(r.get("to")) and r["from"] <= r["to"]

    def as_list(v) -> list:
        return v if isinstance(v, list) else []

    raw_fixes = as_list(plan.get("caption_fixes"))
    fixes, taken = [], set()
    for f in sorted((f for f in raw_fixes if in_range(f) and isinstance(f.get("text"), str)),
                    key=lambda f: f["from"]):
        span = set(range(f["from"], f["to"] + 1))
        if span & taken:            # overlapping fixes would duplicate or lose caption words
            continue
        taken |= span
        fixes.append(f)
    if len(fixes) != len(raw_fixes):
        notes.append("Dropped caption fixes with invalid or overlapping word ranges.")
    plan["caption_fixes"] = fixes
    plan["highlight_words"] = sorted({i for i in as_list(plan.get("highlight_words")) if is_idx(i)})
    plan["claims_to_review"] = [c for c in as_list(plan.get("claims_to_review")) if isinstance(c, dict)]
    ads = [a for a in as_list(plan.get("ads")) if isinstance(a, dict)]
    if not ads:
        raise AdCutterError("Gemini returned no usable ads")
    max_ads = int(cfg.get("ad_count_max", 6))
    if len(ads) > max_ads:
        notes.append(f"Gemini planned {len(ads)} ads; only the first {max_ads} are made.")
        ads = ads[:max_ads]
    plan["ads"] = ads
    plan["response_to_request"] = (plan["response_to_request"]
                                   if isinstance(plan.get("response_to_request"), str) else "")
    for k, ad in enumerate(ads, 1):
        for field in ("name", "headline"):
            if not isinstance(ad.get(field), str) or not ad[field].strip():
                raise AdCutterError(f"ad {k} is missing '{field}'")
        for field in ("funnel_stage", "angle", "primary_text"):
            ad[field] = ad[field] if isinstance(ad.get(field), str) else ""
        segs = [dict(s) for s in as_list(ad.get("segments")) if in_range(s)]
        if not segs:
            raise AdCutterError(f"ad {k} ({ad['name']}) has no valid segments")
        if len(segs) != len(as_list(ad.get("segments"))):
            notes.append(f"Ad {k}: dropped segments with invalid word ranges.")
        for s in segs:   # never open a segment on a connector word
            while s["from"] < s["to"] and words[s["from"]]["w"].lower() in FILLER_STARTS:
                notes.append(f"Ad {k}: segment started on '{words[s['from']]['w']}'; moved to the next word.")
                s["from"] += 1
            if clips and len(clips) > 1:
                a, b = clip_of(words[s["from"]]["s"], clips), clip_of(words[s["to"]]["e"] - 0.001, clips)
                if a != b:
                    notes.append(f"Ad {k}: a segment runs across the join between clip {a} and clip {b} "
                                 "(expect a jump cut there).")
        ad["segments"] = segs
        for c in as_list(ad.get("callouts")):   # keep callouts that began on a skipped filler word
            if isinstance(c, dict) and is_idx(c.get("from")):
                for s in segs:
                    if s["from"] - 3 <= c["from"] < s["from"] and is_idx(c.get("to")) and c["to"] >= s["from"]:
                        c["from"] = s["from"]
        spoken = sum(words[s["to"]]["e"] - words[s["from"]]["s"] for s in segs)
        lo, hi = AD_SECONDS_SANE
        if not lo <= spoken <= hi:
            notes.append(f"Ad {k}: spoken length {spoken:.0f}s is outside the {lo:.0f}-{hi:.0f}s range that works "
                         "as a Meta ad.")
        if len(ad["headline"]) > 80:
            notes.append(f"Ad {k}: headline is {len(ad['headline'])} characters (target 70).")
        good = []
        for c in as_list(ad.get("callouts")):
            inside = in_range(c) and any(s["from"] <= c["from"] and c["to"] <= s["to"] for s in segs)
            if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not c["text"].strip():
                notes.append(f"Ad {k}: dropped a callout with no text.")
                continue
            lines = c["text"].split("\n")
            if not inside:
                notes.append(f"Ad {k}: dropped callout {c.get('text')!r} (not inside the ad's segments).")
            elif len(lines) > 2 or max(len(x) for x in lines) > 28:
                notes.append(f"Ad {k}: dropped callout {c.get('text')!r} (too long for the screen).")
            else:
                good.append(c)
        ad["callouts"] = good
    return plan, notes


def display_words(words: list[dict], plan: dict) -> list[dict]:
    """Caption text per source word index, after Gemini's fixes. Dropped words get text ''."""
    disp = [{"w": w["w"], "s": w["s"], "e": w["e"], "key": False} for w in words]
    for f in sorted(plan["caption_fixes"], key=lambda f: f["from"]):
        disp[f["from"]]["w"] = f["text"].strip()
        disp[f["from"]]["e"] = words[f["to"]]["e"]
        for i in range(f["from"] + 1, f["to"] + 1):
            disp[i]["w"] = ""
    for i in plan["highlight_words"]:
        disp[i]["key"] = True
    return disp


# ---------------------------------------------------------------- 4. cut

@dataclass
class Interval:
    a: float        # source start
    b: float        # source end
    out: float      # output offset
    seg: int        # segment number within the ad


def find_word(clip_words: list[dict], text: str, t: float) -> int | None:
    norm = lambda s: re.sub(r"[^a-z0-9%$']", "", s.lower())
    cands = [(abs(w["s"] - t), i) for i, w in enumerate(clip_words) if norm(w["w"]) == norm(text)]
    cands = [c for c in cands if c[0] < 1.0]
    return min(cands)[1] if cands else None


def start_edge(clip: list[dict], k: int | None, fallback: dict, en: Energy) -> float:
    """Where to cut in so the first word is whole and nothing before it leaks in."""
    if k is None:
        return max(0.0, fallback["s"] - 0.05)
    w = clip[k]
    for a, b in en.gaps(w["s"] - 0.02, w["e"], 0.15):
        # Whisper stretched the word back over a leading silence: at most a sliver of the previous word's tail
        # sits before the gap, and the word itself is still to come after it.
        if a - w["s"] < 0.2 and w["e"] - b >= 0.05:
            return b - 0.08
    prev = clip[k - 1] if k > 0 else None
    if prev and prev["e"] > w["s"] - 0.15:      # the word before runs straight into it
        return en.quietest(prev["e"] - 0.05, w["s"] + 0.02)
    lead = en.gaps(w["s"] - 0.3, w["s"] + 0.02, 0.08)
    return max(lead[-1][0], lead[-1][1] - 0.12) if lead else en.quietest(w["s"] - 0.06, w["s"] + 0.02)


def end_edge(clip: list[dict], k: int | None, fallback: dict, en: Energy) -> float:
    """Where to cut out so the last word is whole and the next one doesn't start."""
    if k is None:
        return fallback["e"] + 0.05
    w = clip[k]
    for a, b in reversed(en.gaps(w["s"], w["e"] + 0.02, 0.15)):
        # Word stretched forward over a trailing silence: the word was spoken before the gap and at most
        # a sliver of the next word's onset sits after it.
        if w["e"] - b < 0.2 and a - w["s"] >= 0.05:
            return a + 0.08
    nxt = clip[k + 1] if k + 1 < len(clip) else None
    if nxt and nxt["s"] < w["e"] + 0.15:
        return en.quietest(w["e"] - 0.03, nxt["s"] + 0.03)
    trail = en.gaps(w["e"] - 0.02, w["e"] + 0.4, 0.08)
    return min(trail[0][0] + 0.10, trail[0][1]) if trail else w["e"] + 0.05


def refine_segment(cfg: dict, words: list[dict], seg: dict, en: Energy, wav: Path, work: Path) -> tuple[float, float]:
    first, last = words[seg["from"]], words[seg["to"]]
    clip = transcribe_clip(cfg, wav, first["s"] - 1.5, first["s"] + 1.5, work)
    a = start_edge(clip, find_word(clip, first["w"], first["s"]), first, en)
    clip = transcribe_clip(cfg, wav, last["e"] - 1.5, last["e"] + 1.5, work)
    b = end_edge(clip, find_word(clip, last["w"], last["s"]), last, en)
    if b - a < 0.5:
        a, b = first["s"], last["e"]
    return a, b


def keep_intervals(bounds: list[tuple[float, float]], en: Energy) -> tuple[list[Interval], float]:
    """Kept source intervals in play order, with long pauses shortened."""
    pieces, pos = [], 0.0
    for n, (lo, hi) in enumerate(bounds):
        cur = lo
        for a, b in en.gaps(lo, hi, PAUSE_MIN):
            if a - cur > 0:
                pieces.append((cur, a + PAUSE_KEEP, n))
            cur = b - PAUSE_KEEP
        pieces.append((cur, hi, n))
    out = []
    for a, b, n in pieces:
        a, b = snap(a), snap(b)
        if b - a >= 1 / FPS:
            out.append(Interval(a, b, pos, n))
            pos += b - a
    return out, pos


def to_out(t: float, ivs: list[Interval], seg: int) -> float | None:
    """Source time -> output time within segment `seg`. Times in a trimmed pause snap to the next kept frame."""
    last = None
    for iv in ivs:
        if iv.seg != seg:
            continue
        if t < iv.a:
            return iv.out
        if t <= iv.b:
            return iv.out + (t - iv.a)
        last = iv.out + (iv.b - iv.a)
    return last


def render_body(full: Path, ivs: list[Interval], body_len: float, cta: float, dest: Path) -> None:
    parts, labels = [], []
    for i, iv in enumerate(ivs):
        d = iv.b - iv.a
        parts.append(f"[0:v]trim=start={iv.a:.4f}:end={iv.b:.4f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={iv.a:.4f}:end={iv.b:.4f},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:d=0.01,afade=t=out:st={max(0, d - 0.01):.4f}:d=0.01[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(ivs)}:v=1:a=1[vc][ac]")
    parts.append(f"[vc]tpad=stop_mode=clone:stop_duration={cta}[v]")
    # aformat pins the layout after aresample. With ffmpeg 5.1 (Debian bookworm, the worker image) the explicit
    # aresample after loudnorm otherwise ends negotiation with no channel layout and every render fails.
    parts.append(f"[ac]highpass=f=80,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo,"
                 f"apad=whole_dur={body_len + cta:.4f}[a]")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(full), "-filter_complex", ";".join(parts),
         "-map", "[v]", "-map", "[a]", "-t", f"{body_len + cta:.4f}", "-c:v", "libx264", "-preset", "medium",
         "-crf", "16", "-pix_fmt", "yuv420p", "-g", "15", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart", str(dest)])


# ---------------------------------------------------------------- 5. compose captions & overlays

def caption_groups(words: list[dict]) -> list[list[dict]]:
    """1-3 word caption groups, broken on pauses and length."""
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or w["s"] - cur[-1]["e"] > 0.35
                    or sum(len(x["w"]) + 1 for x in cur) + len(w["w"]) > 16):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def ad_words(disp: list[dict], ad: dict, ivs: list[Interval], bounds, body_len: float) -> list[dict]:
    out = []
    for n, s in enumerate(ad["segments"]):
        lo, hi = bounds[n]
        for i in range(s["from"], s["to"] + 1):
            w = disp[i]
            if not w["w"]:
                continue
            st = to_out(min(max(w["s"], lo), hi), ivs, n)
            if st is not None:     # None only if this segment was cut away entirely
                out.append({"w": w["w"], "s": st, "key": w["key"]})
    out.sort(key=lambda w: w["s"])
    for i, w in enumerate(out):     # highlight until the next word, at most 0.6 s
        nxt = out[i + 1]["s"] if i + 1 < len(out) else body_len
        w["e"] = max(w["s"] + 0.08, min(nxt, w["s"] + 0.6))
    return out


def compose(ad: dict, words: list[dict], callouts: list[tuple[float, float, str]], body_len: float,
            cfg: dict) -> str:
    els, js = [], []
    groups = caption_groups(words)
    for gi, g in enumerate(groups):
        gs = g[0]["s"]
        ge = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else body_len
        ge = max(gs + 0.1, min(ge, g[-1]["e"] + 0.5, body_len))
        spans = []
        for wi, w in enumerate(g):
            wid = f"g{gi}w{wi}"
            t = html.escape(w["w"].upper())
            layer = "p" if w["key"] else "y"
            spans.append(f'<span class="w" id="{wid}"><span class="b" data-layout-allow-overlap>{t}</span>'
                         f'<span class="{layer}" data-layout-allow-overlap>{t}</span></span>')
            js.append(f'tl.set("#{wid} .{layer}", {{opacity: 1}}, {w["s"]:.3f});')
            if not w["key"]:
                js.append(f'tl.set("#{wid} .y", {{opacity: 0}}, {w["e"]:.3f});')
            js.append(f'tl.fromTo("#{wid}", {{scale: 1.15}}, {{scale: 1, duration: 0.14, ease: "power2.out"}}, '
                      f'{w["s"]:.3f});')
        els.append(f'<div class="cap clip" id="g{gi}" data-start="{gs:.3f}" data-duration="{ge - gs:.3f}" '
                   f'data-track-index="3">{"".join(spans)}</div>')
        js.append(f'tl.fromTo("#g{gi}", {{opacity: 0, y: 14}}, {{opacity: 1, y: 0, duration: 0.1}}, {gs:.3f});')
    for ci, (a, b, text) in enumerate(callouts):
        body = "<br>".join(html.escape(x) for x in text.split("\n"))
        els.append(f'<div class="co clip" id="co{ci}" data-start="{a:.3f}" data-duration="{b - a:.3f}" '
                   f'data-track-index="2"><svg class="arrow" viewBox="0 0 120 140">'
                   f'<path d="M70 132 C 62 96, 58 60, 44 18" /><path d="M22 40 C 32 30, 38 22, 44 16 C 52 26, 60 34, '
                   f'70 40" /></svg><div class="co-box">{body}</div></div>')
        js.append(f'tl.fromTo("#co{ci}", {{opacity: 0, scale: 0.55, rotation: -10}}, '
                  f'{{opacity: 1, scale: 1, rotation: -4, duration: 0.35, ease: "back.out(2)"}}, {a:.3f});')
        js.append(f'tl.to("#co{ci}", {{opacity: 0, duration: 0.2}}, {max(a, b - 0.2):.3f});')
    cta = float(cfg["cta_seconds"])
    tpl = string.Template((HERE / "template" / "composition.html").read_text(encoding="utf-8"))
    return tpl.substitute(
        title=html.escape(ad["name"]), total=f"{body_len + cta:.3f}", body=f"{body_len:.3f}",
        cta_start=f"{body_len:.3f}", cta_dur=f"{cta:.3f}", cta_pill_at=f"{body_len + 0.25:.3f}",
        headline=html.escape(ad["headline"]), cta_line=html.escape(cfg["brand"]["cta_line"]),
        cta_button=html.escape(cfg["brand"]["cta_button"]),
        elements="\n      ".join(els), timeline="\n      ".join(js))


# ---------------------------------------------------------------- 6. render & verify

def npx() -> str:
    return "npx.cmd" if os.name == "nt" else "npx"


def render(cfg: dict, project: Path, dest: Path) -> None:
    try:
        res = subprocess.run([npx(), "hyperframes", "render", str(project), "-q", cfg["render_quality"], "-o",
                              str(dest), "--quiet"], cwd=cfg["hyperframes_dir"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=45 * 60, env=clean_env())
    except subprocess.TimeoutExpired as err:
        raise AdCutterError("HyperFrames render took over 45 minutes and was stopped") from err
    if res.returncode != 0 or not dest.exists():
        tail = re.sub(r"\x1b\[[0-9;]*m", "", res.stderr or res.stdout)[-500:]
        raise AdCutterError(f"HyperFrames render failed (exit {res.returncode}): {tail}")


def check_composition(cfg: dict, project: Path) -> str:
    res = subprocess.run([npx(), "hyperframes", "check", str(project)], cwd=cfg["hyperframes_dir"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", env=clean_env(),
                         timeout=5 * 60)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", res.stdout + res.stderr)
    return "passed" if "Check passed" in plain else "FAILED:\n" + "\n".join(
        line for line in plain.splitlines() if "✗" in line or "error(s)" in line)[:1500]


def verify(cfg: dict, video: Path, expected: list[dict], body_len: float, work: Path) -> dict:
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration", "-of", "json",
         str(video)], capture_output=True, text=True, check=True, env=clean_env()).stdout)
    v = next((s for s in probe["streams"] if s["codec_type"] == "video"), {})
    has_audio = any(s["codec_type"] == "audio" for s in probe["streams"])
    wav = work / "verify.wav"
    run(["ffmpeg", "-v", "error", "-y", "-t", f"{body_len:.3f}", "-i", str(video), "-ac", "1", "-ar", "16000", str(wav)])
    segs, _ = whisper(cfg).transcribe(str(wav), beam_size=5, vad_filter=True)
    heard = " ".join(s.text.strip() for s in segs)
    norm = lambda s: re.findall(r"[a-z0-9]+", s.lower().replace("vantagepoint", "vantage point"))
    h, e = norm(heard), norm(" ".join(w["w"] for w in expected))
    return {"size": f"{v.get('width')}x{v.get('height')}", "audio": has_audio,
            "duration": float(probe["format"]["duration"]),
            "match": difflib.SequenceMatcher(None, h, e).ratio(),
            "heard_start": " ".join(h[:6]), "caption_start": " ".join(e[:6]),
            "heard_end": " ".join(h[-6:]), "caption_end": " ".join(e[-6:])}


# ---------------------------------------------------------------- orchestration

def unique_file(path: Path) -> Path:
    """`path`, or `name (2).ext`, `name (3).ext`... so an existing file is never overwritten."""
    p, n = path, 2
    while p.exists():
        p = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return p


def safe_name(s: str) -> str:
    """A file-name-safe ad name: only the characters the bucket's result keys accept (storage._SAFE), so an
    apostrophe or an ampersand in Gemini's name can never fail the upload after the renders are done."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9._ ()-]+", "", s)).strip()[:60] or "Ad"


def fmt_time(s: float) -> str:
    return f"{int(s // 60)}:{int(round(s % 60)):02d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("videos", type=Path, nargs="+", help="one or more clips, joined in this order")
    ap.add_argument("--brief", default="", help="what you want from the footage (Gemini plans around it)")
    ap.add_argument("--config", type=Path, default=HERE / "config.json")
    ap.add_argument("--replan", action="store_true", help="ask Gemini for a new plan instead of reusing the last one")
    ap.add_argument("--only", type=int, action="append", help="build only this ad number (repeatable)")
    ap.add_argument("--no-render", action="store_true", help="build and check compositions but skip rendering")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    cfg = load_config(args.config)
    srcs = [v.resolve() for v in args.videos]
    for src in srcs:
        if not src.exists():
            raise AdCutterError(f"no such file: {src}")
    first = srcs[0]
    work = Path(cfg["work_dir"]) / re.sub(r"[^\w.-]+", "_", f"{first.stem}_{sum(s.stat().st_size for s in srcs)}")
    out_dir = Path(cfg["output_dir"]) / f"{first.stem} ({dt.date.today():%Y-%m-%d})"
    client = llm.OpenRouter(key_env=cfg["openrouter_key_env"],
                            ledger=LocalLedger(Path(cfg["work_dir"]) / "spend-ledger.jsonl", cfg["monthly_budget_usd"]))
    result = run_pipeline(cfg, srcs, work, out_dir, client, replan=args.replan, only=args.only,
                          render_it=not args.no_render, brief=args.brief, names=[s.name for s in srcs])
    log.info("done: %s", out_dir)
    return 1 if any(e.get("error") for e in result["report"]) else 0


def run_pipeline(cfg: dict, srcs: Path | list[Path], work: Path, out_dir: Path, client: llm.OpenRouter, *,
                 replan: bool = False, only: list[int] | None = None, render_it: bool = True, brief: str = "",
                 names: list[str] | None = None, progress=lambda stage, detail="": None,
                 team_notes: str = "") -> dict:
    """Raw clip(s) + the person's request -> checked ad cuts in out_dir (+ Review Notes.md). Used by the command
    line and the cloud worker. `progress(stage, detail)` is called as work moves along. `team_notes` is the
    feedback block from feedback.team_notes. After rendering, Gemini scores each finished ad (review.py; off with
    `review_enabled: false` in the config). Returns plan, report, notes, planning cost and self-check cost."""
    progress("preparing", "checking and converting the footage")
    media = prepare(srcs, work, float(cfg.get("max_source_seconds", 600)), names)
    label = media["clips"][0]["name"] + (f" + {len(media['clips']) - 1} more" if len(media["clips"]) > 1 else "")
    progress("transcribing")
    words = transcribe(cfg, media["wav"], work / "words.json")
    en = Energy.from_wav(media["wav"])
    log.info("%s words, %.0fs of footage in %s clip(s)", len(words), media["duration"], len(media["clips"]))

    plan_file = work / "plan.json"
    cost = None
    if plan_file.exists() and not replan:
        raw_plan = json.loads(plan_file.read_text(encoding="utf-8"))
        log.info("reusing the saved Gemini plan; a new or changed request needs --replan")
    else:
        progress("planning", "Gemini is watching the footage")
        log.info("asking %s to watch the footage and plan the ads", cfg["plan_model"])
        prompt = build_prompt(cfg, words, brief, media["clips"], team_notes)
        raw_plan, usage = call_gemini(cfg, client, prompt, media["proxy"], media["duration"])
        cost = usage.get("cost")
        plan_file.write_text(json.dumps(raw_plan, indent=2), encoding="utf-8")
        log.info("plan received (cost $%s)", cost)
    plan, notes = validate_plan(json.loads(json.dumps(raw_plan)), words, cfg, media["clips"])
    for n, was_hdr in enumerate(media.get("hdr") or []):
        if was_hdr:
            notes.append(f"Clip {n + 1} ({media['clips'][n]['name']}) is HDR video. It was converted to standard "
                         "colours, so it may look a little different from how it looks on your phone.")
    disp = display_words(words, plan)

    out_dir.mkdir(parents=True, exist_ok=True)     # re-runs share the day's folder; files are never overwritten
    unique_file(out_dir / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    report = []
    if only and not set(only) & set(range(1, len(plan["ads"]) + 1)):
        raise AdCutterError(f"--only {only}: the plan has ads 1-{len(plan['ads'])}")
    todo = [k for k in range(1, len(plan["ads"]) + 1) if not only or k in only]
    for n, (k, ad) in enumerate(((k, plan["ads"][k - 1]) for k in todo), 1):
        progress("rendering", f"ad {n} of {len(todo)}")
        log.info("ad %s: %s", k, ad["name"])
        entry = {"k": k, "ad": ad, "len": 0.0, "check": "not run", "file": None}
        try:   # one failed ad must not cost the others their render or the review notes
            build_ad(cfg, k, ad, words, disp, en, media, work, out_dir, entry, render_it=render_it)
        except Exception as err:   # noqa: BLE001 - any failure is recorded in the notes; the other ads go on
            log.error("ad %s failed: %s", k, err)
            entry["error"] = f"{type(err).__name__}: {err}"[:500]
        report.append(entry)

    review_cost = 0.0
    if render_it and cfg.get("review_enabled", True) and any(e.get("file") for e in report):
        progress("checking", "Gemini is watching the finished ads")
        reviews, review_notes, review_cost = review.review_ads(
            cfg, client, brief=brief, work=work, progress=progress,
            entries=[{**e, "video": work / f"ad{e['k']}" / "render.mp4"} for e in report if e.get("file")])
        for e in report:
            e["review"] = reviews.get(e["k"])
        notes += review_notes

    write_notes(out_dir, label, plan, notes, report, cost, cfg, brief)
    return {"plan": plan, "report": report, "notes": notes, "cost": cost, "duration": media["duration"],
            "clips": media["clips"], "review_cost": review_cost}


def place_callouts(spans: list[tuple[float, float, str]], body_len: float) -> list[tuple[float, float, str]]:
    """Give each callout at least 2 s on screen, but never past the body (the CTA card follows) or into the
    next callout (they share one screen position). Callouts left with under 0.5 s are dropped."""
    out = []
    next_start = body_len - 0.05
    for a, b, text in sorted(spans, reverse=True):   # last first, so each only yields to callouts that are kept
        b = min(max(b, a + 2.0), next_start - (0.1 if out else 0.0))
        if b - a >= 0.5:
            out.append((a, b, text))
            next_start = a
    return out[::-1]


def build_ad(cfg: dict, k: int, ad: dict, words: list[dict], disp: list[dict], en: Energy, media: dict,
             work: Path, out_dir: Path, entry: dict, render_it: bool) -> None:
    bounds = [refine_segment(cfg, words, s, en, media["wav"], work) for s in ad["segments"]]
    ivs, body_len = keep_intervals(bounds, en)
    if not ivs or body_len < 1.0:
        raise AdCutterError("nothing left to play after cutting")
    entry["len"] = body_len + float(cfg["cta_seconds"])
    project = work / f"ad{k}"
    project.mkdir(exist_ok=True)
    render_body(media["full"], ivs, body_len, float(cfg["cta_seconds"]), project / "body.mp4")
    caps = ad_words(disp, ad, ivs, bounds, body_len)
    spans = []
    for c in ad["callouts"]:
        n = next(n for n, s in enumerate(ad["segments"]) if s["from"] <= c["from"] and c["to"] <= s["to"])
        lo, hi = bounds[n]
        a = to_out(min(max(words[c["from"]]["s"], lo), hi), ivs, n)
        b = to_out(min(max(words[c["to"]]["e"], lo), hi), ivs, n)
        if a is not None and b is not None:
            spans.append((a, b, c["text"]))
    (project / "index.html").write_text(compose(ad, caps, place_callouts(spans, body_len), body_len, cfg),
                                        encoding="utf-8")
    # fonts and GSAP travel with the project: the render browser has no internet access
    shutil.copytree(HERE / "template" / "vendor", project / "vendor", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("licenses", "*.md"))
    (project / "hyperframes.json").write_text(json.dumps({"media": {"autoProxy": True}}), encoding="utf-8")
    (project / "meta.json").write_text(json.dumps({"id": f"ad{k}", "name": ad["name"]}), encoding="utf-8")
    entry["check"] = check_composition(cfg, project)
    if not entry["check"].startswith("passed") or not render_it:
        return
    rendered = project / "render.mp4"          # render locally; only a finished file is copied to OneDrive
    rendered.unlink(missing_ok=True)
    log.info("rendering ad %s (a few minutes)", k)
    render(cfg, project, rendered)
    entry["verify"] = verify(cfg, rendered, caps, body_len, work)
    entry["spoken"] = " ".join(w["w"] for w in caps)[:1500]
    dest = unique_file(out_dir / f"Ad - {safe_name(ad['name'])} ({dt.date.today():%Y-%m-%d}).mp4")
    shutil.copyfile(rendered, dest)
    entry["file"] = dest.name


def write_notes(out_dir: Path, label: str, plan: dict, notes: list[str], report: list[dict], cost, cfg: dict,
                brief: str = "") -> None:
    L = [f"# Ad cuts from {label}", "",
         f"Planned by {cfg['plan_model']} on {dt.date.today():%Y-%m-%d}"
         + (f" (planning cost ${cost:.2f})" if isinstance(cost, (int, float)) else " (reused saved plan)") + ".", "",
         f"**What was asked:** {brief.strip() or 'nothing specific; Gemini used the defaults'}", "",
         f"**What Gemini saw:** {plan.get('summary', '')}", ""]
    if plan.get("response_to_request"):
        L += [f"**How Gemini read the request:** {plan['response_to_request']}", ""]
    for e in report:
        ad, v = e["ad"], e.get("verify")
        L += [f"## Ad {e['k']}: {ad['name']}", "",
              f"- **File:** {e['file'] or 'not rendered'}  ({fmt_time(e['len'])})",
              f"- **Stage:** {ad.get('funnel_stage', '')}. {ad.get('angle', '')}",
              f"- **Headline:** {ad['headline']}",
              f"- **Callouts:** " + ("; ".join(c['text'].replace(chr(10), ' / ') for c in ad['callouts']) or "none"),
              f"- **Layout check:** {(e['check'].splitlines() or ['not run'])[0]}"]
        if e.get("error"):
            L.append(f"- **FAILED:** {e['error']}")
        if v:
            ok = v["match"] >= 0.85 and v["size"] == "1080x1920" and v["audio"]
            L += [f"- **Audio check:** {'OK' if ok else 'LOOK AT THIS'}: speech matches captions {v['match']:.0%}; "
                  f"{v['size']}, audio {'yes' if v['audio'] else 'MISSING'}",
                  f"  - starts: heard \"{v['heard_start']}\" / captions \"{v['caption_start']}\"",
                  f"  - ends: heard \"{v['heard_end']}\" / captions \"{v['caption_end']}\""]
        r = e.get("review")
        if r:
            L.append(f"- **Self-check:** {'LOOK AT THIS: ' if r.get('look') else ''}"
                     + ", ".join(f"{a.replace('_', ' ')} {n}/5" for a, n in r["scores"].items()))
            if r.get("verdict"):
                L.append(f"  - {r['verdict']}")
            for p in r["problems"]:
                when = f"{fmt_time(p['at_s'])} " if p["at_s"] is not None else ""
                L.append(f"  - {when}({p['area'].replace('_', ' ')}) {p['what']}" + (f" Fix: {p['fix']}" if p["fix"] else ""))
        L += ["", "**Primary text:**", "", *[f"> {x}" for x in str(ad.get("primary_text", "")).splitlines()], ""]
    claims = plan.get("claims_to_review") or []
    if claims:
        L += ["## Claims to get approved before running", ""]
        L += [f"- **{c.get('ad', '')}:** \"{c.get('claim', '')}\" ({c.get('reason', '')})" for c in claims]
        L.append("")
    if notes:
        L += ["## Pipeline notes", "", *[f"- {n}" for n in notes], ""]
    unique_file(out_dir / "Review Notes.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AdCutterError, subprocess.CalledProcessError) as err:
        log.error("stopped: %s", err)
        sys.exit(1)
