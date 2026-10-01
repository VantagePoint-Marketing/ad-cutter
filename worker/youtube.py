"""YouTube Data API v3, metadata only: titles, channels, lengths, view counts and whether a video is public.

Never media: YouTube's media hosts are blocked in net.py, and Gemini watches videos by link (gemini_free.py).
The key goes in a request header, never in a URL, so it cannot end up in a log. videos.list costs 1 quota unit
per call of up to 50 ids (10,000 units a day on a default key).

YouTube's developer policies: never cache audiovisual content, and refresh or delete stored API data within
30 days (library.py refreshes metadata older than 25 days).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import net
from llm import read_key

API = "https://www.googleapis.com/youtube/v3"
KEY_ENV = "YOUTUBE_API_KEY"
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)(?:\.\d+)?S)?)?$")


def iso_seconds(value: str) -> int | None:
    """ISO 8601 durations as YouTube writes them (PT1H2M3S, P1DT2M, PT45S) -> seconds."""
    m = _DURATION.match(value or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def video_id(text: str) -> str | None:
    """The id from a watch URL, a youtu.be or /shorts/ link, or a bare id; None for anything else."""
    text = (text or "").strip()
    if VIDEO_ID.fullmatch(text):
        return text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    candidate = None
    if host.endswith("youtube.com"):
        candidate = (parse_qs(parts.query).get("v") or [None])[0]
        if not candidate and parts.path.startswith(("/shorts/", "/live/", "/embed/")):
            candidate = parts.path.split("/")[2]
    elif host == "youtu.be":
        candidate = parts.path.lstrip("/").split("/")[0]
    return candidate if candidate and VIDEO_ID.fullmatch(candidate) else None


def videos(ids: list[str], request: Callable = net.request_json, key_env: str = KEY_ENV) -> dict[str, dict]:
    """Metadata for up to 50 videos in one call (1 quota unit). Ids YouTube does not return are left out."""
    ids = [i for i in ids if VIDEO_ID.fullmatch(i)]
    if not ids:
        return {}
    if len(ids) > 50:
        raise ValueError("videos.list takes at most 50 ids per call")
    query = urlencode({"part": "snippet,contentDetails,statistics,status", "id": ",".join(ids), "maxResults": 50})
    data = request("GET", f"{API}/videos?{query}", headers={"X-Goog-Api-Key": read_key(key_env)}, timeout=60)
    out = {}
    for item in data.get("items") or []:
        snippet, details = item.get("snippet") or {}, item.get("contentDetails") or {}
        stats, status = item.get("statistics") or {}, item.get("status") or {}
        out[item.get("id")] = {
            "title": str(snippet.get("title") or "")[:300],
            "channel": str(snippet.get("channelTitle") or "")[:200],
            "seconds": iso_seconds(details.get("duration") or ""),
            "views": int(stats.get("viewCount") or 0),
            "published_at": snippet.get("publishedAt"),
            "public": status.get("privacyStatus") == "public",
            "processed": status.get("uploadStatus") in ("processed", None),
            "live": snippet.get("liveBroadcastContent") not in (None, "none"),
        }
    return out
