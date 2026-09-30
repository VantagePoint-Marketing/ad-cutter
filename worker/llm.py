"""Gemini via OpenRouter, with the key from an environment variable only and a spending guard around every call.

    client = OpenRouter(key_env="OPENROUTER_VIDEO_AGENT_KEY", ledger=LocalLedger(...))
    plan, usage = client.chat_json(model, content, route="zdr", est_input_tokens=..., max_tokens=16000, label="plan")

Before each call: estimate the worst-case cost (input estimate + max_tokens of output), reserve it in the ledger, and
check OpenRouter's own remaining limit for this key. After: settle with the cost OpenRouter reports.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Callable

import net
from budget import BudgetExceeded

log = logging.getLogger("ad-cutter")
API = "https://openrouter.ai/api/v1"

# USD per 1M tokens (input, output), standard route, prompts under 200k tokens. Checked 2026-09-30.
PRICES = {
    "google/gemini-3.1-pro-preview": (2.00, 12.00),
}
# Provider routing. Raw VantagePoint footage always uses "zdr". "youtube" (public YouTube links only; Google AI Studio
# is the only provider that accepts them and it is not zero-retention) is confirmed by the Phase 2 spike before use.
ROUTES = {
    "zdr": {"data_collection": "deny", "zdr": True},
    "youtube": {"only": ["google-ai-studio"], "allow_fallbacks": False},
}


class LLMError(RuntimeError):
    pass


def read_key(name: str) -> str:
    """An environment variable, and on Windows the user's saved environment (a process started before the variable
    was saved won't have it in os.environ). Never a file."""
    value = os.environ.get(name, "").strip()
    if not value and sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                value = str(winreg.QueryValueEx(k, name)[0]).strip()
        except OSError:
            value = ""
    if not value:
        raise LLMError(f"{name} is not set. Add it as an environment variable (never in a file).")
    return value


def extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    text = fence.group(1) if fence else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(text)


def estimate_cost(model: str, input_tokens: int, max_output_tokens: int) -> float:
    if model not in PRICES:
        raise LLMError(f"no price on file for {model}; add it to llm.PRICES before using it")
    pin, pout = PRICES[model]
    if input_tokens >= 200_000:                  # OpenRouter doubles Gemini Pro prices past 200k prompt tokens
        pin, pout = pin * 2, pout * 1.5
    return round((input_tokens * pin + max_output_tokens * pout) / 1_000_000, 4)


def video_tokens(seconds: float) -> int:
    """Gemini counts roughly 300 tokens per second of video+audio at default detail."""
    return int(seconds * 300) + 1000


class OpenRouter:
    def __init__(self, key_env: str, ledger, request: Callable = net.request_json, title: str = "VantagePoint Video Agent",
                 sleep: Callable[[float], None] = time.sleep):
        self.key_env, self.ledger, self.request, self.title, self.sleep = key_env, ledger, request, title, sleep

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {read_key(self.key_env)}", "X-Title": self.title}

    def key_remaining(self) -> float | None:
        """What OpenRouter says is left on this key this period (None = no limit set on the key)."""
        data = self.request("GET", f"{API}/key", headers=self._headers(), timeout=30).get("data") or {}
        rem = data.get("limit_remaining")
        return None if rem is None else float(rem)

    def chat_json(self, model: str, content: list[dict], *, route: str = "zdr", est_input_tokens: int,
                  max_tokens: int = 16000, reasoning: str = "medium", label: str = "", timeout: float = 600,
                  attempts: int = 3) -> tuple[dict, dict]:
        if route not in ROUTES:
            raise LLMError(f"unknown route {route!r}")
        estimate = estimate_cost(model, est_input_tokens, max_tokens)
        worst = estimate * attempts                  # every attempt can be billed
        remaining = self.key_remaining()
        if remaining is None:                        # OpenRouter's limit is our hard stop: never run without one
            raise BudgetExceeded("the OpenRouter key has no spending limit set; add a monthly limit on openrouter.ai")
        if remaining < worst:
            raise BudgetExceeded(f"the OpenRouter key has ${remaining:.2f} left this month; this call could cost "
                                 f"up to ${worst:.2f}")
        res = self.ledger.reserve(worst, label)
        body = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": max_tokens,
                "reasoning": {"effort": reasoning}, "response_format": {"type": "json_object"},
                "usage": {"include": True}, "provider": ROUTES[route]}
        last, charged, unknown, outcome, in_flight_error = None, 0.0, 0.0, None, None
        try:
            for attempt in range(1, attempts + 1):
                data = None
                try:
                    data = self.request("POST", f"{API}/chat/completions", headers=self._headers(), body=body,
                                        timeout=timeout)
                    usage = data.get("usage") or {}
                    charged += float(usage.get("cost") or 0.0)
                    if data.get("error"):
                        raise LLMError(f"provider error: {str(data['error'])[:300]}")
                    parsed = extract_json(data["choices"][0]["message"].get("content") or "")
                    outcome = f"ok, attempt {attempt}"
                    return parsed, usage
                except net.BlockedHost as err:        # a refused redirect/host won't change on retry
                    last = str(err)
                    break
                except net.HttpError as err:
                    last = str(err)
                    if err.status >= 500:
                        unknown += estimate          # may have been billed upstream: count the worst case
                    if err.status < 500 and err.status != 429:
                        break
                except TimeoutError as err:
                    # the request may still be running (and billed) upstream: count the worst case, don't pay twice
                    unknown += estimate
                    outcome = "timeout"
                    raise LLMError(f"Gemini timed out after {timeout / 60:.0f} minutes") from err
                except Exception as err:          # noqa: BLE001 - bad JSON, cut-off reads, odd shapes: retry
                    last = f"{type(err).__name__}: {err}"
                    if data is None:                  # failed after sending but before a usable reply: may be billed
                        unknown += estimate
                log.warning("Gemini attempt %s failed: %s", attempt, last)
                if attempt < attempts:
                    self.sleep(20)
            outcome = f"failed: {last}"[:300]
            raise LLMError(f"Gemini call failed: {last}")
        except BaseException as err:
            in_flight_error = err
            raise
        finally:
            # always record what this call cost, however it ended; an interrupted in-flight call counts at worst case
            if outcome is None:
                unknown += estimate
            try:
                self.ledger.settle(res, charged + unknown, model, note=outcome or "interrupted")
            except Exception:                     # noqa: BLE001 - never hide the error that got us here
                log.exception("could not record the cost of a Gemini call")
                if in_flight_error is None:
                    raise
