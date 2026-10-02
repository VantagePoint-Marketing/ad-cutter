"""Google's Gemini API on the free tier, for one job only: watching a PUBLIC YouTube video by its link.

The free tier may use what it is sent to improve Google's products, so this client can only ever send three things:
a YouTube watch URL that it builds itself from a validated video id, an optional time window, and one of the prompt
files in worker/prompts/ (filled with fields our own code produces). It has no way to send footage, files or free
text. Raw footage and every planning call stay on OpenRouter's zero-retention route (llm.py).

    client = GeminiFree()                                     # key from env GEMINI_API_KEY, never a file
    w = client.watch("gemini-3.5-flash", "QR8LxximqWI", "watch_craft", {"scope": "..."}, window=(0, 600))
    w.text, w.video_tokens, w.usage

Errors come back as GeminiFreeError with a `kind`: "busy" (the model is overloaded: try another model or later),
"network" (a timeout or a broken connection: stop and try later), "rate_limited" (a free-tier limit of this model),
"bad_model" (the model name does not exist any more), "unavailable" (403/404: the video is private, removed or
restricted, or the key lacks permission), "bad_request" (refused; an invalid or revoked key also lands here) or
"bad_reply" (no usable text came back). The caller must check the key before blaming a video.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import net
from llm import read_key

API = "https://generativelanguage.googleapis.com/v1beta"
KEY_ENV = "GEMINI_API_KEY"
PROMPTS = Path(__file__).resolve().parent / "prompts"
# The only prompts this client may send, and the exact fields each takes. Every value is produced by our own code.
PROMPT_FIELDS = {"watch_craft": {"scope"},
                 "analyze_reference": {"name", "menu", "zoom_punch", "transitions", "rendered"}}
ALLOWED_PROMPTS = set(PROMPT_FIELDS)
PROFILE_NAME = re.compile(r"^[\w.-]{1,60}$")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
MODEL_NAME = re.compile(r"^gemini-[a-z0-9.-]+$")


class GeminiFreeError(RuntimeError):
    def __init__(self, kind: str, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.kind, self.status, self.body = kind, status, body


@dataclass
class Watch:
    model: str
    text: str
    usage: dict
    video_tokens: int | None
    seconds: float


def watch_url(video_id: str) -> str:
    if not VIDEO_ID.fullmatch(video_id or ""):
        raise ValueError(f"not a YouTube video id: {video_id!r}")
    return f"https://www.youtube.com/watch?v={video_id}"


def build_body(model: str, video_id: str, prompt: str, window: tuple[int, int] | None = None, *,
               thinking: str = "low", schema: dict | None = None, max_output: int = 8192) -> dict:
    part: dict = {"file_data": {"file_uri": watch_url(video_id)}}
    if window:
        start, end = (int(x) for x in window)
        if not 0 <= start < end:
            raise ValueError(f"bad window {window!r}")
        part["video_metadata"] = {"start_offset": f"{start}s", "end_offset": f"{end}s"}
    config: dict = {"responseMimeType": "application/json", "temperature": 0.2, "maxOutputTokens": max_output}
    if schema:
        config["responseJsonSchema"] = schema
    if not model.startswith("gemini-2."):          # Gemini 2.x takes a thinking budget, not a level; its default is fine
        config["thinkingConfig"] = {"thinkingLevel": thinking}
    return {"contents": [{"role": "user", "parts": [part, {"text": prompt}]}], "generationConfig": config}


def classify(status: int, body: str = "") -> str:
    if status in (500, 502, 503, 504):
        return "busy"
    if status == 429:
        return "rate_limited"
    if status == 404 and "models/" in body:          # "models/gemini-x is not found": a retired or misspelled model
        return "bad_model"
    if status in (403, 404):
        return "unavailable"
    return "bad_request"


def error_text(body: str) -> str:
    try:
        err = json.loads(body).get("error", {})
        return f"{err.get('status', '')}: {err.get('message', '')}"[:200]
    except (ValueError, AttributeError):
        return body[:200]


def video_tokens(usage: dict) -> int | None:
    """Tokens Google counted for the video and its sound (the proof that it was really watched)."""
    details = usage.get("promptTokensDetails")
    if not isinstance(details, list):
        return None
    return sum(int(d.get("tokenCount") or 0) for d in details
               if isinstance(d, dict) and d.get("modality") in ("VIDEO", "AUDIO"))


class GeminiFree:
    def __init__(self, request: Callable = net.request_json, key_env: str = KEY_ENV, timeout: float = 300,
                 key: str | None = None):
        """`key` is for a caller that rotates between several free keys it read from the environment itself
        (freepool.py); otherwise the one key comes from the `key_env` environment variable."""
        self.request, self.key_env, self.timeout, self.key = request, key_env, timeout, key

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.key or read_key(self.key_env)}

    @staticmethod
    def prompt(name: str, fields: dict) -> str:
        """An allow-listed prompt file, filled with the one field our code produces (`scope`: which part to
        watch). No other text can reach the free tier through this client."""
        if name not in ALLOWED_PROMPTS:
            raise ValueError(f"prompt {name!r} is not one this client may send")
        if name == "watch_craft" and (set(fields) != {"scope"} or not isinstance(fields["scope"], str)
                                      or len(fields["scope"]) > 500):
            raise ValueError("a watch prompt takes exactly one short field, 'scope'")
        if set(fields) != PROMPT_FIELDS[name] or not all(isinstance(v, str) and len(v) <= 6000 for v in fields.values()):
            raise ValueError(f"prompt {name!r} takes exactly the fields {sorted(PROMPT_FIELDS[name])}, as short text")
        if name == "analyze_reference" and not PROFILE_NAME.fullmatch(fields["name"]):
            raise ValueError("a profile name is letters, digits, dots, dashes and underscores only")
        return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").format(**fields)

    def watch(self, model: str, video_id: str, prompt_name: str, fields: dict,
              window: tuple[int, int] | None = None, *, thinking: str = "low", schema: dict | None = None,
              max_output: int = 8192) -> Watch:
        if not MODEL_NAME.fullmatch(model):
            raise ValueError(f"not a Gemini model name: {model!r}")
        body = build_body(model, video_id, self.prompt(prompt_name, fields), window, thinking=thinking,
                          schema=schema, max_output=max_output)
        started = time.time()
        try:
            data = self.request("POST", f"{API}/models/{model}:generateContent", headers=self._headers(), body=body,
                                timeout=self.timeout)
        except net.HttpError as err:
            raise GeminiFreeError(classify(err.status, err.body), f"{model}: HTTP {err.status} {error_text(err.body)}",
                                  err.status, err.body) from err
        except net.BlockedHost as err:
            raise GeminiFreeError("bad_request", f"{model}: {err}") from err
        except OSError as err:                    # timeouts and broken connections: try again later
            raise GeminiFreeError("network", f"{model}: {type(err).__name__}: {err}") from err
        candidates = data.get("candidates") or [{}]
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        texts = [p.get("text") for p in parts if isinstance(p, dict) and p.get("text") and not p.get("thought")]
        if not texts:
            reason = (candidates[0] or {}).get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason")
            raise GeminiFreeError("bad_reply", f"{model}: no text came back ({reason or 'no reason given'})")
        usage = data.get("usageMetadata") or {}
        return Watch(model, texts[-1], usage, video_tokens(usage), time.time() - started)

    def key_ok(self) -> str:
        """A free call that proves the key works (lists one model)."""
        data = self.request("GET", f"{API}/models?pageSize=1", headers=self._headers(), timeout=30)
        return f"key works ({len(data.get('models') or [])} model listed)"
