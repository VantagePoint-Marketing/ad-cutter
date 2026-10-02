"""Keeps what the agent learned safe outside Railway, and lets one environment hand its knowledge to another.

Files in the agent's storage (agent_store.py), all under this environment's own folders (a token can only write there):
  memory/<env>/hub/<UTC time>.jsonl.gz   everything the agent LEARNED (notes not seeded from our own files, any status) and the links
                                         to or from them, one line per note or link. A NEW file is added whenever it changed (at
                                         most every 6 hours); old ones are never replaced or deleted by the worker.
  exports/<env>/hub-vault.zip            the same hub as an Obsidian vault (every active note, with [[links]]), rewritten when it changed.

`<env>` is AGENT_STORE_ENV ("staging", "production"). Two ways a hub takes in a snapshot, both insert-only:
  * a NEW environment: set HUB_RESTORE_FROM=staging on production's worker; it reads staging's newest snapshot once;
  * a hub that LOST its data (no notes learned, never uploaded from here) reads its own newest snapshot before it uploads anything.
A restore never touches a note that already exists, never creates rules or skills (those come from our files or from a person), forces
the origin to the agent's own kinds, never restores retired notes, and refuses an oversized snapshot.

`Syncer.step()` does one small unit of work and never raises an Exception.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import logging
import os
import re
import time
import zlib

import agent_store
import hub_export
from agent_store import AgentStore
from hub import Hub

log = logging.getLogger("ad-cutter")
EVERY = 6 * 3600              # seconds between looks for changes to upload
BATCH = 60                    # notes taken in per restore step
VERSION = 1
VAULT_VERSION = "curated-1"   # bump to make every environment rewrite its vault once (e.g. after changing what the vault holds)
MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024        # after decompression
RESTORE_KINDS = ("source", "lesson", "technique", "recipe", "example")      # never rule or skill
RESTORE_ORIGINS = ("youtube", "foreplay", "library", "agent")
RESTORE_STATUSES = ("active", "draft")
env_name = agent_store.env_name


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
    """(notes, links) from a snapshot; raises ValueError when it is not one of ours or is too large once unpacked."""
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = d.decompress(data, MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES or d.unconsumed_tail:
            raise ValueError("the snapshot is larger than allowed")
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (zlib.error, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError("not a hub snapshot") from err
    if not rows or not isinstance(rows[0], dict) or rows[0].get("t") != "meta" or rows[0].get("version") != VERSION:
        raise ValueError("a snapshot from a different version")
    rows = [r for r in rows if isinstance(r, dict)]
    return [r for r in rows if r.get("t") == "note"], [r for r in rows if r.get("t") == "link"]


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Syncer:
    def __init__(self, hub: Hub, store: AgentStore, env: str | None = None, restore_from: str | None = None, clock=time.time):
        self.hub, self.store, self.now = hub, store, clock
        self.env = env or store.env or env_name()
        raw = restore_from if restore_from is not None else os.environ.get("HUB_RESTORE_FROM", "")
        self.restore_from = re.sub(r"[^a-z0-9-]", "", raw.lower())
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

    # ------------------------------------------------------------ one step

    def _newest(self, env: str) -> str | None:
        paths = sorted(o["path"] for o in self.store.list(f"memory/{env}/hub/") if o["path"].endswith(".jsonl.gz"))
        return paths[-1] if paths else None

    def _step(self) -> str:
        if not self.checked:
            self.checked = True
            return f"self-test: {self.store.selftest()}"
        state = self.hub.get_state("sync")
        source = None
        if self.restore_from and self.restore_from != self.env and state.get("restored_from") != self.restore_from:
            source = self.restore_from
        elif not state.get("uploaded") and not state.get("self_restore_checked"):
            items, _ = self.hub.dump_learned()
            if not items and self._newest(self.env):          # empty hub, earlier saves exist: take them back before saving anything
                source = self.env
            else:
                state = {**state, "self_restore_checked": True}
                self.hub.set_state("sync", state)
        if source:
            return self._restore_some(state, source)
        items, links = self.hub.dump_learned()
        data = snapshot_bytes(items, links)
        digest = fingerprint(data)
        if state.get("uploaded") != digest or state.get("vault") != VAULT_VERSION:
            if state.get("uploaded") != digest:                    # a new snapshot only when what was learned changed
                self.store.put_bytes(self.store.own("memory", f"hub/{stamp()}.jsonl.gz"), data, "application/gzip")
            all_items, all_links = hub_export.curate(*self.hub.everything())
            self.store.put_bytes(self.store.own("exports", "hub-vault.zip"), hub_export.vault_zip(all_items, all_links), "application/zip")
            self.hub.set_state("sync", {**state, "uploaded": digest, "vault": VAULT_VERSION})
            self.next_at = self.now() + EVERY
            return f"saved {len(items)} learned notes and {len(links)} links, and the Obsidian vault ({len(all_items)} notes)"
        self.next_at = self.now() + EVERY
        return "storage is up to date"

    def _restore_some(self, state: dict, source: str) -> str:
        path = self._newest(source)
        data = self.store.get_bytes(path) if path else None
        if data is None:
            self.next_at = self.now() + 3600
            return f"nothing to restore yet: no snapshot under memory/{source}/hub/"
        notes, links = read_snapshot(data)
        notes = [n for n in notes if n.get("kind") in RESTORE_KINDS and isinstance(n.get("slug"), str) and isinstance(n.get("title"), str)]
        target = fingerprint(data)
        done = int(state.get("restore_done", 0)) if state.get("restore_target") == target else 0
        added = int(state.get("restore_added", 0)) if state.get("restore_target") == target else 0
        for row in notes[done:done + BATCH]:
            if self.hub.get(row["kind"], row["slug"]) is not None:       # insert-only: what is here already wins
                continue
            origin = row.get("origin") if row.get("origin") in RESTORE_ORIGINS else "agent"
            status = row.get("status") if row.get("status") in RESTORE_STATUSES else "draft"
            tags = row.get("tags") if isinstance(row.get("tags"), list) else []
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            self.hub.upsert(row["kind"], row["slug"], row["title"], str(row.get("body", "")), tags, meta, origin=origin, status=status,
                            confidence=row.get("confidence") if isinstance(row.get("confidence"), (int, float)) else None)
            added += 1
        taken = min(done + BATCH, len(notes))
        if taken < len(notes):
            self.hub.set_state("sync", {**state, "restore_target": target, "restore_done": taken, "restore_added": added})
            return f"restoring notes from {source}: {taken} of {len(notes)}"
        made = 0
        for row in links:                               # every note is in; links are quick single statements
            try:
                made += 1 if self.hub.link_by_slug(row["from"][0], row["from"][1], row["to"][0], row["to"][1], row["rel"], str(row.get("note", ""))) else 0
            except (ValueError, KeyError, IndexError, TypeError):
                continue
        done_state = {**state, "restore_done": len(notes), "restore_target": target, "restore_added": added}
        if source == self.env:
            done_state["self_restore_checked"] = True
        else:
            done_state["restored_from"] = source
        self.hub.set_state("sync", done_state)
        return f"restored {added} new notes (of {len(notes)}) and {made} of {len(links)} links from {source}"
