"""The only way the worker's own code talks to the internet.

Every request goes through request_json(), which refuses any host that is not on the allowlist and always refuses
YouTube's video hosts, so no code path can download YouTube media. (boto3's bucket traffic and the Hugging Face model
download at image build time are separate and are not routed through here.)
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ALLOWED_HOSTS = {
    "openrouter.ai",              # Gemini via OpenRouter
    "www.googleapis.com",         # YouTube Data API (metadata only)
    "youtube.googleapis.com",
    "public.api.foreplay.co",     # Foreplay API
    "generativelanguage.googleapis.com",   # Gemini free tier: watching public YouTube videos by link (gemini_free.py)
    "wxoeiwkrannpwtdpgdsa.supabase.co",    # the agent's own storage (agent_store.py): its gate function and signed file links
}
# Never, even if someone adds them to the allowlist: YouTube pages and video streams.
BLOCKED_SUFFIXES = ("youtube.com", "youtu.be", "googlevideo.com", "ytimg.com")


class BlockedHost(PermissionError):
    pass


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


def check_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        raise BlockedHost(f"only https is allowed, not {parts.scheme!r}")
    if any(host == s or host.endswith("." + s) for s in BLOCKED_SUFFIXES):
        raise BlockedHost(f"{host} is blocked (no YouTube media access)")
    if host not in ALLOWED_HOSTS:
        raise BlockedHost(f"{host} is not on the worker's allowlist")
    return url


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would re-send our Authorization header to the new host before we could check
    it. None of the APIs we call should redirect, so a redirect is treated as an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BlockedHost(f"refusing to follow a {code} redirect to {urlsplit(newurl).hostname}")


_OPENER = urllib.request.build_opener(_NoRedirects)


def request_json(method: str, url: str, headers: dict[str, str] | None = None, body: dict | None = None,
                 timeout: float = 60) -> dict:
    """JSON in, JSON out. Raises HttpError for non-2xx answers and BlockedHost for disallowed URLs or redirects."""
    check_url(url)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as err:
        raise HttpError(err.code, err.read().decode("utf-8", "replace")) from err


def request_bytes(method: str, url: str, headers: dict[str, str] | None = None, data: bytes | None = None,
                  timeout: float = 120, max_bytes: int = 60 * 1024 * 1024) -> tuple[bytes, dict[str, str]]:
    """Raw bytes in, raw bytes out, on the same allowlist and with the same no-redirect rule as request_json. Refuses an answer
    larger than max_bytes. Returns (body, response headers)."""
    check_url(url)
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise HttpError(413, f"the answer is larger than {max_bytes} bytes")
            return body, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as err:
        raise HttpError(err.code, err.read(2000).decode("utf-8", "replace")) from err
