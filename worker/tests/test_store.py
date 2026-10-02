"""The agent's storage client and the hub sync, against a fake of the gate function (no network)."""
import gzip
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_store  # noqa: E402
import hub_sync  # noqa: E402
import net  # noqa: E402
from agent_store import AgentStore, StoreError  # noqa: E402

TOKEN = "t" * 43
GATE = agent_store.DEFAULT_URL


class FakeGate:
    """Behaves like supabase/functions/video-agent-store plus the signed Storage links it hands out."""

    def __init__(self, token=TOKEN):
        self.files, self.calls, self.token = {}, [], token

    def __call__(self, method, url, headers=None, data=None, timeout=60, max_bytes=60 * 1024 * 1024):
        self.calls.append((method, url, dict(headers or {})))
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if url.startswith("https://signed.example/put/"):
            self.files[url.rsplit("/put/", 1)[1]] = data
            return b'{"Key":"ok"}', {}
        if url.startswith("https://signed.example/get/"):
            path = url.rsplit("/get/", 1)[1]
            if path not in self.files:
                raise net.HttpError(404, "no")
            body = self.files[path]
            if len(body) > max_bytes:
                raise net.HttpError(413, "big")
            return body, {}
        if (headers or {}).get("x-agent-token") != self.token:
            raise net.HttpError(401, '{"error":"not allowed"}')
        op = q.get("op")
        if op == "text" and method == "PUT":
            assert len(data) <= 1_000_000
            self.files[q["path"]] = data
            return b'{"saved":true}', {}
        if op == "text" and method == "GET":
            if q["path"] not in self.files:
                raise net.HttpError(404, '{"error":"not found"}')
            body = self.files[q["path"]]
            if len(body) > max_bytes:
                raise net.HttpError(413, "the answer is larger than")
            return body, {}
        body = json.loads(data.decode()) if data else {}
        if op == "list":
            prefix = q.get("prefix", "")
            return json.dumps({"objects": [{"path": p, "size": len(v), "updated": None} for p, v in sorted(self.files.items()) if p.startswith(prefix)]}).encode(), {}
        if op == "sign-put":
            return json.dumps({"path": body["path"], "url": "https://signed.example/put/" + body["path"], "token": "x"}).encode(), {}
        if op == "sign-get":
            if body["path"] not in self.files:
                raise net.HttpError(404, "no")
            return json.dumps({"url": "https://signed.example/get/" + body["path"]}).encode(), {}
        if op == "delete":
            self.files.pop(body["path"], None)
            return b'{"deleted":true}', {}
        raise net.HttpError(400, "unknown")


@pytest.fixture
def gate():
    return FakeGate()


@pytest.fixture
def store(gate):
    return AgentStore(GATE, TOKEN, gate)


def test_text_and_files_round_trip_small_and_large(store, gate, tmp_path):
    store.put_text("memory/notes/one.txt", "hello é")
    assert store.get_text("memory/notes/one.txt") == "hello é" and store.get_text("memory/notes/missing.txt") is None
    big = os.urandom(agent_store.TEXT_LIMIT + 10)
    store.put_bytes("assets/clips/big.bin", big)
    assert gate.files["assets/clips/big.bin"] == big                                 # went through a signed link, not the gate
    assert not any("assets/clips" in c[1] and c[1].startswith(GATE) and "op=text" in c[1] for c in gate.calls)
    assert store.get_bytes("assets/clips/big.bin") == big and store.get_bytes("assets/clips/none.bin") is None
    src, dst = tmp_path / "a.bin", tmp_path / "out" / "b.bin"
    src.write_bytes(b"abc")
    store.put_file("references/x/a.bin", src)
    assert store.get_file("references/x/a.bin", dst) and dst.read_bytes() == b"abc" and not store.get_file("references/x/none", dst)
    assert [o["path"] for o in store.list("memory/")] == ["memory/notes/one.txt"]
    store.delete("memory/notes/one.txt")
    assert store.list("memory/") == []


def test_the_token_is_sent_on_gate_calls_and_a_wrong_one_is_refused(gate):
    bad = AgentStore(GATE, "w" * 43, gate)
    with pytest.raises(StoreError, match="401"):
        bad.list("memory/")
    gate.calls.clear()
    AgentStore(GATE, TOKEN, gate).list("memory/")
    assert [c[2].get("x-agent-token") for c in gate.calls] == [TOKEN]
    big = os.urandom(agent_store.TEXT_LIMIT + 10)
    gate.calls.clear()
    AgentStore(GATE, TOKEN, gate).put_bytes("assets/b.bin", big)
    signed = [c for c in gate.calls if c[1].startswith("https://signed.example/")]
    assert signed and all("x-agent-token" not in c[2] for c in signed)                # the token never goes to a signed file link
    with pytest.raises(StoreError):
        AgentStore(GATE, "short", gate)


@pytest.mark.parametrize("path", ["", "../x", "memory/../x", "/memory/x", "memory//x", "other/x", "memory/x\n", "memory/" + "a" * 300,
                                  "memory/‮x", "memory/a;b", "memory/.", "skills", "memory/x/../../y"])
def test_bad_paths_never_reach_the_network(store, gate, path):
    with pytest.raises(StoreError):
        store.put_text(path, "x")
    with pytest.raises(StoreError):
        store.get_text(path)
    with pytest.raises(StoreError):
        store.delete(path)
    assert gate.calls == []


def test_a_file_over_the_bucket_limit_is_refused_before_upload(store, gate):
    with pytest.raises(StoreError, match="limit"):
        store.put_bytes("assets/huge.bin", b"x" * (agent_store.FILE_LIMIT + 1))
    assert gate.calls == []


def test_from_env_needs_a_token_and_uses_the_default_gate(monkeypatch):
    monkeypatch.delenv("AGENT_STORE_TOKEN", raising=False)
    assert AgentStore.from_env() is None
    monkeypatch.setenv("AGENT_STORE_TOKEN", TOKEN)
    monkeypatch.delenv("AGENT_STORE_URL", raising=False)
    assert AgentStore.from_env().url == agent_store.DEFAULT_URL


def test_the_gate_and_signed_links_are_on_the_worker_allowlist_and_nothing_else_from_there_is():
    net.check_url(agent_store.DEFAULT_URL)
    net.check_url("https://wxoeiwkrannpwtdpgdsa.supabase.co/storage/v1/object/upload/sign/video-agent/assets/x?token=y")
    with pytest.raises(net.BlockedHost):
        net.check_url("https://other-project.supabase.co/functions/v1/video-agent-store")
    with pytest.raises(net.BlockedHost):
        net.check_url("http://wxoeiwkrannpwtdpgdsa.supabase.co/functions/v1/video-agent-store")


def test_selftest_exercises_every_operation_and_cleans_up(store, gate):
    assert "storage works" in store.selftest() and gate.files == {}
    gate.token = "other"
    with pytest.raises(StoreError):
        store.selftest()


# ---------------------------------------------------------------- the hub snapshot and sync

def note(i, origin="agent", status="active", **kw):
    return {"kind": "example", "slug": f"n{i}", "title": f"Note {i}", "body": "b" * 5, "tags": ["ads"], "meta": {"k": i},
            "origin": origin, "status": status, "confidence": None, **kw}


class FakeHub:
    def __init__(self, items=(), links=()):
        self.items, self.links, self.state, self.upserts, self.linked = list(items), list(links), {}, [], []

    def dump_learned(self):
        return self.items, self.links

    def everything(self):
        return [dict(i, id=n) for n, i in enumerate(self.items)], []

    def get_state(self, key):
        return dict(self.state.get(key, {}))

    def set_state(self, key, value):
        self.state[key] = dict(value)

    def upsert(self, kind, slug, title, body="", tags=(), meta=None, origin="seed", status="active", confidence=None):
        self.upserts.append((kind, slug, origin, status))
        return len(self.upserts)

    def link_by_slug(self, fk, fs, tk, ts, rel, note=""):
        self.linked.append((fk, fs, tk, ts, rel))
        return fs != "ghost"


def test_snapshots_are_deterministic_and_read_back_exactly():
    items, links = [note(1), note(2, status="draft")], [("example", "n1", "technique", "punch-in", "example_of", "why")]
    a, b = hub_sync.snapshot_bytes(items, links), hub_sync.snapshot_bytes(items, links)
    assert a == b and hub_sync.fingerprint(a) == hub_sync.fingerprint(b)
    notes, got_links = hub_sync.read_snapshot(a)
    assert [n["slug"] for n in notes] == ["n1", "n2"] and notes[1]["status"] == "draft" and got_links[0]["to"] == ["technique", "punch-in"]
    for bad in (b"junk", gzip.compress(b"{}"), gzip.compress(b'{"t":"meta","version":99}\n'), gzip.compress(b"")):
        with pytest.raises(ValueError):
            hub_sync.read_snapshot(bad)


def make_syncer(hub, store, **kw):
    clock = {"t": 1000.0}
    s = hub_sync.Syncer(hub, store, env=kw.pop("env", "staging"), restore_from=kw.pop("restore_from", ""), clock=lambda: clock["t"])
    return s, clock


def test_sync_checks_storage_first_then_saves_only_when_something_changed(store, gate):
    hub = FakeHub([note(1)])
    s, clock = make_syncer(hub, store)
    assert s.step().startswith("self-test: storage works")
    first = s.step()
    assert first.startswith("saved 1 learned notes") and "memory/hub/staging.jsonl.gz" in gate.files and "exports/hub-staging.zip" in gate.files
    assert not s.ready()                                                             # waits six hours
    clock["t"] += hub_sync.EVERY + 1
    assert s.step() == "storage is up to date"
    hub.items.append(note(2))
    clock["t"] += hub_sync.EVERY + 1
    assert s.step().startswith("saved 2 learned notes")


def test_a_storage_failure_pauses_the_syncer_and_never_raises(store, gate):
    gate.token = "changed"
    s, clock = make_syncer(FakeHub([note(1)]), store)
    said = s.step()
    assert said.startswith("paused 15 min") and not s.ready()
    clock["t"] += 901
    assert s.ready()


def test_production_takes_in_stagings_notes_in_batches_then_links_then_carries_on(store, gate):
    staging = FakeHub([note(i) for i in range(hub_sync.BATCH + 10)], [("example", "n1", "technique", "x", "example_of", ""),
                                                                      ("example", "ghost", "technique", "x", "example_of", "")])
    gate.files["memory/hub/staging.jsonl.gz"] = hub_sync.snapshot_bytes(*staging.dump_learned())
    prod = FakeHub()
    s, _ = make_syncer(prod, store, env="production", restore_from="staging")
    assert s.step().startswith("self-test")
    assert s.step() == f"restoring notes from staging: {hub_sync.BATCH} of {hub_sync.BATCH + 10}"
    assert len(prod.upserts) == hub_sync.BATCH
    done = s.step()
    assert done == f"restored {hub_sync.BATCH + 10} notes and 1 of 2 links from staging" and len(prod.upserts) == hub_sync.BATCH + 10
    assert prod.upserts[0][2:] == ("agent", "active")
    nxt = s.step()                                                                    # restore is finished: it saves its own snapshot now
    assert nxt.startswith("saved") and "memory/hub/production.jsonl.gz" in gate.files and len(prod.upserts) == hub_sync.BATCH + 10


def test_restore_waits_when_the_other_environment_has_saved_nothing_and_never_restores_from_itself(store, gate):
    s, clock = make_syncer(FakeHub(), store, env="production", restore_from="staging")
    s.step()
    assert s.step().startswith("nothing to restore yet") and not s.ready()
    same, _ = make_syncer(FakeHub([note(1)]), store, env="staging", restore_from="staging")
    same.step()
    assert same.step().startswith("saved")                                           # asked to restore from itself: ignored


def test_the_snapshot_holds_nothing_but_learned_notes_and_never_selftest_notes():
    import hub as hubmod
    sql_seen = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=()):
            sql_seen.append(" ".join(sql.split()))
            return type("C", (), {"fetchall": lambda self: []})()
    hubmod.Hub(lambda: Conn()).dump_learned()
    assert "origin <> 'seed'" in sql_seen[0] and "selftest-" in sql_seen[0] and "selftest-" in sql_seen[1] and "evidence" not in " ".join(sql_seen)


def test_environment_names_are_plain():
    os.environ["AGENT_STORE_ENV"] = "Prod Env/../x"
    try:
        assert hub_sync.env_name() == "prodenvx"
    finally:
        del os.environ["AGENT_STORE_ENV"]
    assert hub_sync.env_name() == "default"
