"""Rotate several free Gemini keys across a ladder of free models, and say when every free option is used up.

Order of preference, always: the best model first, spreading calls across all the keys; a model steps down only when
it is out for the day on every key (or does not exist any more). A short per-minute limit is waited out rather than
stepping down, up to `max_wait` seconds. Only when nothing free is left does `FreeExhausted` tell the caller to use
the paid route, if it chooses to.

Keys come from the environment only (`GEMINI_API_KEYS`, comma separated, and the single `GEMINI_API_KEY` the library
uses). They are never logged: a key is only ever named by its position ("key 2").
"""
from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable

import gemini_free
from gemini_free import GeminiFreeError

log = logging.getLogger("ad-cutter")
DAY = 24 * 3600.0
DEFAULT_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"]


class FreeExhausted(RuntimeError):
    """Every free key and model is used up (or cooling for longer than we will wait)."""


def keys_from_env(env=None) -> list[str]:
    """The free keys, in order and without duplicates or blanks."""
    env = os.environ if env is None else env
    raw = [*(env.get("GEMINI_API_KEYS", "") or "").split(","), env.get("GEMINI_API_KEY", "") or ""]
    out: list[str] = []
    for k in (x.strip() for x in raw):
        if k and k not in out:
            out.append(k)
    return out


def retry_seconds(body: str, default: float = 60.0) -> float:
    m = re.search(r'retryDelay"?\s*:\s*"?([\d.]+)s', body or "")
    return float(m.group(1)) + 1 if m else default


def is_daily(body: str) -> bool:
    return bool(re.search(r"PerDay|per day|daily", body or "", re.I))


class KeyRing:
    def __init__(self, keys: list[str], models: list[str] | None = None, *, max_wait: float = 90.0,
                 max_total_wait: float = 600.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, make_client: Callable[[str], gemini_free.GeminiFree] | None = None):
        self.keys = list(keys)
        self.models = list(models or DEFAULT_MODELS)
        self.max_wait, self.max_total_wait = max_wait, max_total_wait
        self.clock, self.sleep = clock, sleep
        self.make_client = make_client or (lambda key: gemini_free.GeminiFree(key=key, timeout=900))
        self.until: dict[tuple[int, str], float] = {}       # (key, model) -> not usable before this time
        self.dead: set[int] = set()                         # keys Google refuses
        self.gone: set[str] = set()                         # models that do not exist
        self.cursor = 0                                     # round robin: the next call starts after the last key used
        self.waited = 0.0
        self.calls: dict[int, int] = {}

    # -- state
    def _alive_models(self) -> list[str]:
        out = []
        for m in self.models:
            if m in self.gone:
                continue
            if any(i not in self.dead and self.until.get((i, m), 0) < float("inf") for i in range(len(self.keys))):
                out.append(m)
        return out

    def _free_keys(self, model: str) -> list[int]:
        now = self.clock()
        order = [(self.cursor + n) % len(self.keys) for n in range(len(self.keys))]
        return [i for i in order if i not in self.dead and self.until.get((i, model), 0) <= now]

    def _soonest(self, model: str) -> float | None:
        waits = [self.until.get((i, model), 0) - self.clock() for i in range(len(self.keys)) if i not in self.dead]
        waits = [w for w in waits if w != float("inf")]
        return min(waits) if waits else None

    def status(self) -> str:
        return (f"{len(self.keys) - len(self.dead)} of {len(self.keys)} keys usable; models left: "
                f"{', '.join(self._alive_models()) or 'none'}")

    # -- the call
    def watch(self, video_id: str, prompt_name: str, fields: dict, window=None, **kw) -> tuple[gemini_free.Watch, dict]:
        """One free watch, rotating keys and stepping down models as needed. Returns (watch, {"model", "key"})."""
        if not self.keys:
            raise FreeExhausted("no free Gemini keys are set (GEMINI_API_KEYS)")
        while True:
            alive = self._alive_models()
            if not alive:
                raise FreeExhausted(f"every free model is used up or unavailable ({self.status()})")
            model = alive[0]
            free = self._free_keys(model)
            if not free:
                wait = self._soonest(model)
                if wait is None or wait > self.max_wait or self.waited + wait > self.max_total_wait:
                    self.until.update({(i, model): float("inf") for i in range(len(self.keys))})   # give up on it
                    continue
                log.info("free %s is cooling on every key; waiting %.0f s", model, wait)
                self.sleep(max(wait, 0.0))
                self.waited += max(wait, 0.0)
                continue
            i = free[0]
            self.cursor = (i + 1) % len(self.keys)
            try:
                w = self.make_client(self.keys[i]).watch(model, video_id, prompt_name, fields, window, **kw)
            except GeminiFreeError as err:
                self._note_failure(i, model, err)
                if err.kind in ("unavailable", "network"):
                    raise
                continue
            self.calls[i] = self.calls.get(i, 0) + 1
            return w, {"model": model, "key": i + 1}

    def _note_failure(self, i: int, model: str, err: GeminiFreeError) -> None:
        if err.kind == "rate_limited":
            if is_daily(err.body):
                self.until[(i, model)] = float("inf")
                log.info("free key %s is out for the day on %s", i + 1, model)
            else:
                self.until[(i, model)] = self.clock() + retry_seconds(err.body)
        elif err.kind == "bad_model":
            self.gone.add(model)
            log.info("free model %s does not exist; dropped", model)
        elif err.kind == "busy":
            self.until[(i, model)] = self.clock() + 60
        elif err.kind == "bad_reply":
            self.until[(i, model)] = self.clock() + 30
        elif err.kind == "bad_request":
            if re.search(r"API[ _]?KEY|expired|revoked", err.body or str(err), re.I):
                self.dead.add(i)
                log.warning("free key %s was refused by Google and is skipped", i + 1)
            else:
                raise err                  # a malformed request would fail the same way on every key
        # "unavailable" (private/removed video) and "network" are raised by the caller
