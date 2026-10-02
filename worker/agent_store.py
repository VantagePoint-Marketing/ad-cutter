"""The agent's own storage: skills, memory, references and assets that outlive any one Railway environment.

It lives in a private Supabase Storage bucket ("video-agent"). The worker never holds a Supabase key: it holds one token for ITS
environment and talks to a small gate function (supabase/functions/video-agent-store). Paths are <folder>/<environment>/...
with folder one of skills, memory, references, assets, exports. The gate lets a token:
  * read anything (so production can read what staging saved);
  * write only under <folder>/<its own environment>/;
  * never replace an existing file, except under exports/ and _selftest/ folders (history is add-only);
  * delete only under exports/ and _selftest/ folders.

    store = AgentStore.from_env()                  # None when AGENT_STORE_TOKEN is not set
    store.put_text(store.own("memory", "notes/one.txt"), "...")      # memory/<env>/notes/one.txt
    store.get_text("memory/staging/notes/one.txt")                    # None when it does not exist
    store.put_file(store.own("assets", "clip.mp4"), Path("clip.mp4"))
    store.get_file("assets/staging/clip.mp4", Path("copy.mp4"))
    store.list("memory/")                          # [{"path", "size", "updated"}]

Every network call goes through net.py (https only, allowlisted host, no redirects). Reads go through one-time signed links, so
the gate never holds a big file in memory.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import net

DEFAULT_URL = "https://wxoeiwkrannpwtdpgdsa.supabase.co/functions/v1/video-agent-store"
FOLDERS = ("skills/", "memory/", "references/", "assets/", "exports/")
TEXT_LIMIT = 900_000            # the gate takes up to 1 MB through itself; larger goes direct to Storage
FILE_LIMIT = 50 * 1024 * 1024   # the bucket's own limit
MAX_SEGMENTS = 6
PATH_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-/ ]*")


class StoreError(RuntimeError):
    pass


class StoreExists(StoreError):
    """The file already exists and this part of the storage never replaces files."""


def env_name() -> str:
    return re.sub(r"[^a-z0-9-]", "", os.environ.get("AGENT_STORE_ENV", "").strip().lower()) or "default"


def clean_path(path: str) -> str:
    """The same rules the gate applies, checked here first so a bad path fails before any network call."""
    parts = path.split("/") if isinstance(path, str) else []
    if (not parts or len(path) > 300 or len(parts) > MAX_SEGMENTS or not PATH_OK.fullmatch(path)
            or any(part in ("", ".", "..") for part in parts) or not path.startswith(FOLDERS)):
        raise StoreError(f"not a storable path: {str(path)[:80]!r} (use skills/, memory/, references/, assets/ or exports/, at most 6 parts)")
    return path


class AgentStore:
    def __init__(self, url: str, token: str, env: str = "default", request=net.request_bytes):
        if not token or len(token) < 20:
            raise StoreError("AGENT_STORE_TOKEN is missing or too short")
        self.url, self.token, self.env, self.request = url.rstrip("/"), token, env, request

    @classmethod
    def from_env(cls, request=net.request_bytes) -> "AgentStore | None":
        token = os.environ.get("AGENT_STORE_TOKEN", "").strip()
        if not token:
            return None
        return cls(os.environ.get("AGENT_STORE_URL", DEFAULT_URL).strip() or DEFAULT_URL, token, env_name(), request)

    def own(self, folder: str, rest: str) -> str:
        """A path this environment may write: <folder>/<this environment>/<rest>."""
        return clean_path(f"{folder.strip('/')}/{self.env}/{rest.lstrip('/')}")

    # ------------------------------------------------------------ plumbing

    def _call(self, method: str, op: str, query: str = "", body: dict | None = None, data: bytes | None = None,
              content_type: str | None = None, timeout: float = 60, max_bytes: int = 2_000_000) -> bytes:
        headers = {"x-agent-token": self.token}
        if body is not None:
            data, content_type = json.dumps(body).encode("utf-8"), "application/json"
        if content_type:
            headers["content-type"] = content_type
        try:
            out, _ = self.request(method, f"{self.url}?op={op}{query}", headers=headers, data=data, timeout=timeout, max_bytes=max_bytes)
        except net.HttpError as err:
            if err.status == 404:
                raise FileNotFoundError(op) from err
            if err.status == 409:
                raise StoreExists("that file already exists (this folder never replaces files)") from err
            raise StoreError(f"storage gate answered {err.status}: {err.body[:200]}") from err
        return out

    def _json(self, *args, **kw) -> dict:
        try:
            return json.loads(self._call(*args, **kw).decode("utf-8") or "{}")
        except ValueError as err:
            raise StoreError("the storage gate sent something that is not JSON") from err

    # ------------------------------------------------------------ text

    def put_text(self, path: str, text: str) -> None:
        self.put_bytes(path, text.encode("utf-8"), "text/plain; charset=utf-8")

    def get_text(self, path: str) -> str | None:
        data = self.get_bytes(path)
        return None if data is None else data.decode("utf-8")

    # ------------------------------------------------------------ bytes and files

    def put_bytes(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        path = clean_path(path)
        if len(data) > FILE_LIMIT:
            raise StoreError(f"{path} is {len(data) / 1e6:.0f} MB; the limit is {FILE_LIMIT // (1024 * 1024)} MB")
        if len(data) <= TEXT_LIMIT:
            self._call("PUT", "text", f"&path={quote(path, safe='/')}", data=data, content_type=content_type)
            return
        signed = self._json("POST", "sign-put", body={"path": path, "size": len(data)})
        try:
            self.request("PUT", signed["url"], headers={"content-type": content_type}, data=data, timeout=300, max_bytes=100_000)
        except net.HttpError as err:
            raise StoreError(f"upload of {path} failed: {err.status}") from err

    def get_bytes(self, path: str, max_bytes: int = FILE_LIMIT) -> bytes | None:
        path = clean_path(path)
        try:
            signed = self._json("POST", "sign-get", body={"path": path})
        except FileNotFoundError:
            return None
        try:
            out, _ = self.request("GET", signed["url"], timeout=300, max_bytes=max_bytes)
        except net.HttpError as err:
            if err.status == 404:
                return None
            raise StoreError(f"download of {path} failed: {err.status}") from err
        return out

    def put_file(self, path: str, source: Path, content_type: str = "application/octet-stream") -> None:
        self.put_bytes(path, Path(source).read_bytes(), content_type)

    def get_file(self, path: str, dest: Path, max_bytes: int = FILE_LIMIT) -> bool:
        data = self.get_bytes(path, max_bytes)
        if data is None:
            return False
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".partial")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return True

    # ------------------------------------------------------------ listing and deleting

    def list(self, prefix: str = "") -> list[dict]:
        if prefix:
            clean_path(prefix.rstrip("/") + "/x")
        return list(self._json("GET", "list", f"&prefix={quote(prefix, safe='/')}", max_bytes=1_000_000).get("objects") or [])

    def delete(self, path: str) -> None:
        """Only exports/ files and _selftest/ files can be deleted; the gate refuses anything else."""
        self._call("POST", "delete", body={"path": clean_path(path)})

    # ------------------------------------------------------------ a check that every operation works

    def selftest(self) -> str:
        """Write, read, replace, list and delete a small and a large file in this environment's _selftest folders. Raises on the
        first problem; leaves nothing behind."""
        small, big = self.own("memory", "_selftest/small.txt"), self.own("assets", "_selftest/big.bin")
        payload = os.urandom(TEXT_LIMIT + 5000)
        try:
            self.put_text(small, "agent store selftest")
            self.put_text(small, "agent store selftest, replaced")                 # selftest files may be replaced
            if self.get_text(small) != "agent store selftest, replaced":
                raise StoreError("text did not round-trip")
            if small not in [o["path"] for o in self.list(f"memory/{self.env}/_selftest/")]:
                raise StoreError("the listing did not show the new file")
            self.put_bytes(big, payload)
            self.put_bytes(big, payload[::-1])                                    # a large file replaced through a signed link
            if self.get_bytes(big) != payload[::-1]:
                raise StoreError("a large file did not round-trip")
            return "storage works (text, large file, replace, list, delete)"
        finally:
            for p in (small, big):
                try:
                    self.delete(p)
                except Exception:                  # noqa: BLE001 - a failed clean-up must not hide the real error
                    pass
