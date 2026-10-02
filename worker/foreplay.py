"""Foreplay's Public API: ads that are performing in the market, as references for what currently works.

The key comes from the environment only (FOREPLAY_API_KEY, set by hand in Railway, never in a file). Foreplay charges one
credit per ad returned (empty or error answers and the usage check are free), so every call is capped by the caller and the
monthly credits the learner may spend are limited (learner.py). Performance is judged by how long an ad has been running
and whether it is still live: Spyder rankings are not on this plan.

Only text and metadata are used here (headline, description, the ad's own words, how long it ran, format, platforms); the
analysis that comes out of them is stored in the hub, never the ad's media or full text.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlencode

import net
from llm import read_key

API = "https://public.api.foreplay.co"
KEY_ENV = "FOREPLAY_API_KEY"
CREDITS_PER_AD = 1
MAX_PAGE = 25                   # ads asked for per call (the API allows 250; small steps keep each call cheap and the budget exact)
# what "performing" means for our references: video ads that have run a long time and are still live, 10 to 75 seconds, English
DEFAULT_FILTERS = {"live": "true", "display_format": ["video"], "publisher_platform": ["facebook", "instagram"],
                   "languages": ["english"], "video_duration_min": 10, "video_duration_max": 75,
                   "running_duration_min_days": 21, "order": "longest_running"}


def _get(path: str, params: dict, request: Callable = net.request_json, key_env: str = KEY_ENV) -> dict:
    """GET with the key in the Authorization header. The API describes bearer tokens; an earlier integration used the bare
    key, so the bare key is tried first and a 401 is retried once as a bearer token."""
    url = f"{API}{path}" + (f"?{urlencode(params, doseq=True)}" if params else "")
    key = read_key(key_env)
    try:
        return request("GET", url, headers={"Authorization": key}, timeout=60)
    except net.HttpError as err:
        if err.status != 401:
            raise
    return request("GET", url, headers={"Authorization": f"Bearer {key}"}, timeout=60)


def usage(request: Callable = net.request_json, key_env: str = KEY_ENV) -> dict:
    """Credits left this billing cycle (free to ask): {"remaining": int, "total": int}."""
    data = _get("/api/usage", {}, request, key_env)
    d = data.get("data") if isinstance(data.get("data"), dict) else data
    return {"remaining": int(d.get("remaining_credits") or 0), "total": int(d.get("total_credits") or 0)}


def discover(query: str, limit: int = 10, cursor: str | None = None, request: Callable = net.request_json,
             key_env: str = KEY_ENV, **filters) -> tuple[list[dict], str | None]:
    """(ads, the cursor for the next page) for a text search with the default 'performing' filters; costs one credit per ad."""
    params = {**DEFAULT_FILTERS, **filters, "limit": max(1, min(int(limit), MAX_PAGE))}
    query = re.sub(r"[\x00-\x1f\x7f]+|\s+", " ", query or "").strip()[:120]
    if query:
        params["query"] = query
    if cursor:
        params["cursor"] = str(cursor)
    data = _get("/api/discovery/ads", params, request, key_env)
    ads = [normalise(a) for a in (data.get("data") or []) if isinstance(a, dict) and a.get("id")]
    nxt = (data.get("metadata") or {}).get("cursor") if isinstance(data.get("metadata"), dict) else None
    return ads, (str(nxt) if nxt not in (None, "") and ads else None)


def running_days(started) -> int | None:
    """Days since the ad started running (ISO text or epoch seconds/milliseconds), None when it cannot be read."""
    try:
        if isinstance(started, (int, float)):
            ts = started / 1000 if started > 1e11 else started
            when = datetime.fromtimestamp(ts, timezone.utc)
        else:
            when = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - when).days)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _s(value, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+|\s+", " ", str(value or "")).strip()[:limit]


def normalise(ad: dict) -> dict:
    """The few fields we use, trimmed. The ad's spoken words are kept (for the analysis step only) up to 3,000 characters."""
    return {"id": _s(ad.get("id"), 60), "name": _s(ad.get("name"), 200), "brand_id": _s(ad.get("brand_id"), 60),
            "headline": _s(ad.get("headline"), 300), "description": _s(ad.get("description"), 600),
            "cta": _s(ad.get("cta_title") or ad.get("cta_type"), 60), "format": _s(ad.get("display_format"), 20),
            "live": bool(ad.get("live")), "started_running": ad.get("started_running"),
            "running_days": running_days(ad.get("started_running")), "running": ad.get("running_duration"),
            "video_seconds": ad.get("video_duration"), "platforms": [_s(p, 20) for p in (ad.get("publisher_platform") or [])][:5],
            "niches": [_s(n, 30) for n in (ad.get("niches") or [])][:4], "words": _s(ad.get("full_transcription"), 3000),
            "url": _s(ad.get("foreplay_url"), 300)}
