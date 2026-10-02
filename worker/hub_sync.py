"""Keeps what the agent learned safe outside Railway, and lets one environment hand its knowledge to another.

Two files per environment in the agent's storage (agent_store.py):
  memory/hub/<env>.jsonl.gz    everything the agent LEARNED (notes not seeded from our own files, any status) and the links
                               to or from them, as one line per note or link. Rewritten when it changed, at most every 6 hours.
  exports/hub-<env>.zip        the same hub as an Obsidian vault (every active note, with [[links]]) for people to read.

`<env>` is AGENT_STORE_ENV (for example "staging" or "production"). Production starts with an empty hub; set
HUB_RESTORE_FROM=staging on its worker and it takes in staging's snapshot once, a few notes at a time, then carries on by itself.
A restore only ADDS or updates notes by kind and slug and never deletes anything.

`Syncer.step()` does one small unit of work and never raises an Exception.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import re
import time

import hub_export
from agent_store import AgentStore, StoreError
from hub import Hub

log = logging.getLogger("ad-cutter")
EVERY = 6 * 3600              # seconds between looks for changes to upload
BATCH = 60                    # notes taken in per restore step
VERSION = 1


def env_name() -> str:
    name = re.sub(r"[^a-z0-9-]", "", os.environ.get("AGENT_STORE_ENV", "").strip().lower())
    return name or "default"


def snapshot_bytes(items: list[dict], links: list[tuple]) -> bytes:
    """One JSON line per note and per link, gzip-compressed. Deterministic for the same hub (so unchanged means identical)."""
    lines = [json.dumps({"t": "meta", "version": VERSION}, sort_keys=True)]
    for it in items:
        lines.append(json.dumps({"t": "note", "kind": it["kind"], "slug": it["slug"], "title": it["title"], "body": it["body"],
                                 "tags": it["tags"], "meta": it["meta"], "origin": it["origin"], "status": it["status"],
                                 "confidence": it["confidence"]}, sort_keys=True, ensure_ascii=False))
    for fk, fs, tk, ts, rel, note in links:
        lines.append(json.dumps({"t": "link", "from": [fk, fs], "to": [tk, ts], "rel": rel, "note": note}, sort_keys=True, ensure_ascii=False))
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as z:          # mtime 0: the same content gives the same bytes
        z.write(("\n".join(lines) + "\n").encode("utf-8"))
    return buf.getvalue()


def read_snapshot(data: bytes) -> tuple[list[dict], list[dict]]:
    """(notes, links) from a snapshot; raises ValueError when it is not one of ours."""
    try:
        rows = [json.loads(line) for line in gzip.decompress(data).decode("utf-8").splitlines() if line.strip()]
    except (OSError, ValueError, UnicodeDecodeError) as err:
        raise ValueError("not a hub snapshot") from err
    if not rows or rows[0].get("t") != "meta" or rows[0].get("version") != VERSION:
        raise ValueError("a snapshot from a different version")
    return [r for r in rows if r.get("t") == "note"], [r for r in rows if r.get("t") == "link"]


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Syncer:
    def __init__(self, hub: Hub, store: AgentStore, env: str | None = None, restore_from: str | None = None, clock=time.time):
        self.hub, self.store, self.now = hub, store, clock
        self.env = env or env_name()
        self.restore_from = re.sub(r"[^a-z0-9-]", "", (restore_from if restore_from is not None else os.environ.get("HUB_RESTORE_FROM", "")).lower())
        self.next_at, self.checked = 0.0, False

    def ready(self) -> bool:
        return self.now() >= self.next_at

    def step(self) -> str:
        try:
            return self._step()
        except Exception as err:                       # noqa: BLE001 - storage trouble must never stop the worker
            log.exception("sync: step failed")
            self.next_at = self.now() + 900
            return f"paused 15 min: {type(err).__name__}: {str(err)[:200]}"

    def _step(self) -> str:
        if not self.checked:
            self.checked = True
            return f"self-test: {self.store.selftest()}"
        state = self.hub.get_state("sync")
        if self.restore_from and self.restore_from != self.env and state.get("restored_from") != self.restore_from:
            return self._restore_some(state)
        mine = f"memory/hub/{self.env}.jsonl.gz"
        items, links = self.hub.dump_learned()
        data = snapshot_bytes(items, links)
        digest = fingerprint(data)
        if state.get("uploaded") != digest:
            self.store.put_bytes(mine, data, "application/gzip")
            all_items, all_links = self.hub.everything()
            self.store.put_bytes(f"exports/hub-{self.env}.zip", hub_export.vault_zip(all_items, all_links), "application/zip")
            self.hub.set_state("sync", {**state, "uploaded": digest})
            self.next_at = self.now() + EVERY
            return f"saved {len(items)} learned notes and {len(links)} links, and the Obsidian vault ({len(all_items)} notes)"
        self.next_at = self.now() + EVERY
        return "storage is up to date"

    def _restore_some(self, state: dict) -> str:
        path = f"memory/hub/{self.restore_from}.jsonl.gz"
        data = self.store.get_bytes(path)
        if data is None:
            self.next_at = self.now() + 3600
            return f"nothing to restore yet: {path} does not exist"
        notes, links = read_snapshot(data)
        total = len(notes) + len(links)
        done = int(state.get("restore_done", 0)) if state.get("restore_target") == fingerprint(data) else 0
        for row in notes[done:done + BATCH]:
            self.hub.upsert(row["kind"], row["slug"], row["title"], row.get("body", ""), row.get("tags", []), row.get("meta", {}),
                            origin=row.get("origin", "agent"), status=row.get("status", "active"), confidence=row.get("confidence"))
        taken = min(done + BATCH, len(notes))
        if taken < len(notes):
            self.hub.set_state("sync", {**state, "restore_target": fingerprint(data), "restore_done": taken})
            return f"restoring notes from {self.restore_from}: {taken} of {len(notes)}"
        made = 0
        for row in links:                               # every note is in; links are quick single statements
            made += 1 if self.hub.link_by_slug(row["from"][0], row["from"][1], row["to"][0], row["to"][1], row["rel"], row.get("note", "")) else 0
        self.hub.set_state("sync", {**state, "restored_from": self.restore_from, "restore_done": total, "restore_target": fingerprint(data)})
        return f"restored {len(notes)} notes and {made} of {len(links)} links from {self.restore_from}"
