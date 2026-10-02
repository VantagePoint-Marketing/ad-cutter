"""The agent's storage client and the hub sync, against a fake of the gate function (no network). The fake enforces the same rules as
supabase/functions/video-agent-store: one token per environment, writes only under <folder>/<own env>/, add-only except exports/ and
_selftest/, deletes only there, 6-part paths."""
import gzip
import json
import os
import sys
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_store  # noqa: E402
import hub_sync  # noqa: E402
import net  # noqa: E402
from agent_store import AgentStore, StoreError, StoreExists  # noqa: E402

TOKENS = {"S" * 43: "staging", "P" * 43: "production"}
GATE = agent_store.DEFAULT_URL


def replaceable(path):
    return path.startswith("exports/") or path.split("/")[2:3] == ["_selftest"]


class FakeGate:
    """Behaves like the deployed gate plus the signed Storage links it hands out."""

    def __init__(self):
        self.files, self.calls = {}, []

    def __call__(self, method, url, headers=None, data=None, timeout=60, max_bytes=60 * 1024 * 1024):
        self.calls.append((method, url, dict(headers or {})))
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if url.startswith("https://signed.example/put/"):
            path = url.rsplit("/put/", 1)[1]
            if path in self.files and not replaceable(path):
                raise net.HttpError(409, "exists")
            self.files[path] = data
            return b'{"Key":"ok"}', {}
        if url.startswith("https://signed.example/get/"):
            path = url.rsplit("/get/", 1)[1]
            if path not in self.files:
                raise net.HttpError(404, "no")
            if len(self.files[path]) > max_bytes:
                raise net.HttpError(413, "big")
            return self.files[path], {}
        env = TOKENS.get((headers or {}).get("x-agent-token"))
        if env is None:
            raise net.HttpError(401, '{"error":"not allowed"}')
        op = q.get("op")

        def check_write(path):
            if len(path.split("/")) < 3 or path.split("/")[1] != env:
                raise net.HttpError(403, "this token may only write under its own environment")
            if len(path.split("/")) > 6 or not path.startswith(agent_store.FOLDERS):
                raise net.HttpError(400, "bad path")
        if op == "text" and method == "PUT":
            path = q["path"]
            check_write(path)
            assert len(data) <= 1_000_000
            if path in self.files and not replaceable(path):
                raise net.HttpError(409, "already exists")
            self.files[path] = data
            return b'{"saved":true}', {}
        body = json.loads(data.decode()) if data else {}
        if op == "list":
            prefix = q.get("prefix", "")
            return json.dumps({"objects": [{"path": p, "size": len(v), "updated": None} for p, v in sorted(self.files.items()) if p.startswith(prefix)]}).encode(), {}
        if op == "sign-put":
            check_write(body["path"])
            if body["path"] in self.files and not replaceable(body["path"]):
                raise net.HttpError(409, "already exists")
            return json.dumps({"path": body["path"], "url": "https://signed.example/put/" + body["path"], "token": "x"}).encode(), {}
        if op == "sign-get":
            if body["path"] not in self.files:
                raise net.HttpError(404, "no")
            return json.dumps({"url": "https://signed.example/get/" + body["path"]}).encode(), {}
        if op == "delete":
            check_write(body["path"])
            if not replaceable(body["path"]):
                raise net.HttpError(403, "deleting is limited")
            self.files.pop(body["path"], None)
            return b'{"deleted":true}', {}
        raise net.HttpError(400, "unknown")


@pytest.fixture
def gate():
    return FakeGate()


def make_store(gate, env="staging"):
    token = next(t for t, e in TOKENS.items() if e == env)
    return AgentStore(GATE, token, env, gate)


@pytest.fixture
def store(gate):
    return make_store(gate)


def test_text_and_files_round_trip_small_and_large(store, gate, tmp_path):
    p = store.own("memory", "notes/one.txt")
    assert p == "memory/staging/notes/one.txt"
    store.put_text(p, "hello é")
    assert store.get_text(p) == "hello é" and store.get_text("memory/staging/notes/missing.txt") is None
    big = os.urandom(agent_store.TEXT_LIMIT + 10)
    store.put_bytes(store.own("assets", "clips/big.bin"), big)
    assert gate.files["assets/staging/clips/big.bin"] == big                          # went through a signed link, not the gate
    assert store.get_bytes("assets/staging/clips/big.bin") == big and store.get_bytes("assets/staging/clips/none.bin") is None
    assert not any("op=text" in c[1] and c[0] == "GET" for c in gate.calls)           # reads always use signed links
    src, dst = tmp_path / "a.bin", tmp_path / "out" / "b.bin"
    src.write_bytes(b"abc")
    store.put_file(store.own("references", "x/a.bin"), src)
    assert store.get_file("references/staging/x/a.bin", dst) and dst.read_bytes() == b"abc" and not store.get_file("references/staging/x/none", dst)
    assert [o["path"] for o in store.list("memory/")] == ["memory/staging/notes/one.txt"]


def test_history_is_add_only_small_and_large_but_exports_and_selftest_can_be_replaced(store, gate):
    p = store.own("memory", "hub/a.txt")
    store.put_text(p, "one")
    with pytest.raises(StoreExists):
        store.put_text(p, "two")
    assert store.get_text(p) == "one"
    big = store.own("assets", "b.bin")
    store.put_bytes(big, os.urandom(agent_store.TEXT_LIMIT + 1))
    with pytest.raises(StoreExists):
        store.put_bytes(big, os.urandom(agent_store.TEXT_LIMIT + 1))
    e = store.own("exports", "hub-vault.zip")
    store.put_text(e, "v1")
    store.put_text(e, "v2")
    assert store.get_text(e) == "v2"
    store.delete(e)
    assert store.get_text(e) is None
    with pytest.raises(StoreError, match="403"):
        store.delete(p)                                                                 # memory is never deleted by the worker
    assert store.get_text(p) == "one"


def test_an_environment_can_read_the_other_but_never_write_or_delete_there(gate):
    staging, production = make_store(gate, "staging"), make_store(gate, "production")
    staging.put_text(staging.own("memory", "hub/s.txt"), "staging's")
    assert production.get_text("memory/staging/hub/s.txt") == "staging's"
    with pytest.raises(StoreError, match="403"):
        production.put_text("memory/staging/hub/evil.txt", "x")
    with pytest.raises(StoreError, match="403"):
        staging.put_text("memory/production/hub/evil.txt", "x")
    with pytest.raises(StoreError, match="403"):
        staging.put_bytes("assets/production/evil.bin", os.urandom(agent_store.TEXT_LIMIT + 1))
    with pytest.raises(StoreError, match="403"):
        staging.delete("exports/production/hub-vault.zip")
    assert "memory/production/hub/evil.txt" not in gate.files


def test_a_wrong_token_is_refused_and_the_token_never_goes_to_a_signed_link(gate):
    with pytest.raises(StoreError, match="401"):
        AgentStore(GATE, "w" * 43, "staging", gate).list("memory/")
    with pytest.raises(StoreError):
        AgentStore(GATE, "short", "staging", gate)
    s = make_store(gate)
    gate.calls.clear()
    s.put_bytes(s.own("assets", "b.bin"), os.urandom(agent_store.TEXT_LIMIT + 10))
    s.get_bytes("assets/staging/b.bin")
    signed = [c for c in gate.calls if c[1].startswith("https://signed.example/")]
    assert len(signed) == 2 and all("x-agent-token" not in c[2] for c in signed)
    assert all(c[2].get("x-agent-token") for c in gate.calls if c[1].startswith(GATE))


@pytest.mark.parametrize("path", ["", "../x", "memory/../x", "/memory/x", "memory//x", "other/x", "memory/x\n", "memory/" + "a" * 300,
                                  "memory/‮x", "memory/a;b", "memory/.", "skills", "memory/x/../../y", "memory/a/b/c/d/e/f.txt"])
def test_bad_paths_never_reach_the_network(store, gate, path):
    for call in (lambda: store.put_text(path, "x"), lambda: store.get_text(path), lambda: store.delete(path)):
        with pytest.raises(StoreError):
            call()
    assert gate.calls == []


def test_a_file_over_the_bucket_limit_is_refused_before_upload(store, gate):
    with pytest.raises(StoreError, match="limit"):
        store.put_bytes("assets/staging/huge.bin", b"x" * (agent_store.FILE_LIMIT + 1))
    assert gate.calls == []


def test_from_env_needs_a_token_and_uses_the_default_gate_and_a_plain_environment_name(monkeypatch):
    monkeypatch.delenv("AGENT_STORE_TOKEN", raising=False)
    assert AgentStore.from_env() is None
    monkeypatch.setenv("AGENT_STORE_TOKEN", "t" * 43)
    monkeypatch.delenv("AGENT_STORE_URL", raising=False)
    monkeypatch.setenv("AGENT_STORE_ENV", "Prod Env/../x")
    got = AgentStore.from_env()
    assert got.url == agent_store.DEFAULT_URL and got.env == "prodenvx"
    monkeypatch.delenv("AGENT_STORE_ENV")
    assert AgentStore.from_env().env == "default"


def test_the_gate_and_signed_links_are_on_the_worker_allowlist_and_nothing_else_from_there_is():
    net.check_url(agent_store.DEFAULT_URL)
    net.check_url("https://wxoeiwkrannpwtdpgdsa.supabase.co/storage/v1/object/upload/sign/video-agent/assets/x?token=y")
    with pytest.raises(net.BlockedHost):
        net.check_url("https://other-project.supabase.co/functions/v1/video-agent-store")
    with pytest.raises(net.BlockedHost):
        net.check_url("http://wxoeiwkrannpwtdpgdsa.supabase.co/functions/v1/video-agent-store")


def test_selftest_exercises_every_operation_including_replacing_a_large_file_and_cleans_up(store, gate):
    assert "storage works" in store.selftest() and gate.files == {}
    bad = AgentStore(GATE, "w" * 43, "staging", gate)
    with pytest.raises(StoreError):
        bad.selftest()


# ---------------------------------------------------------------- the hub snapshot and sync

def note(i, origin="agent", status="active", kind="example", **kw):
    return {"kind": kind, "slug": f"n{i}", "title": f"Note {i}", "body": "b" * 5, "tags": ["ads"], "meta": {"k": i},
            "origin": origin, "status": status, "confidence": None, **kw}


class FakeHub:
    def __init__(self, items=(), links=()):
        self.items, self.links, self.state, self.upserts, self.linked = list(items), list(links), {}, [], []
        self.have = set()

    def dump_learned(self):
        return self.items, self.links

    def everything(self):
        return [dict(i, id=n) for n, i in enumerate(self.items)], []

    def get(self, kind, slug):
        return {"kind": kind, "slug": slug} if (kind, slug) in self.have else None

    def get_state(self, key):
        return dict(self.state.get(key, {}))

    def set_state(self, key, value):
        self.state[key] = dict(value)

    def upsert(self, kind, slug, title, body="", tags=(), meta=None, origin="seed", status="active", confidence=None):
        self.upserts.append((kind, slug, origin, status))
        self.have.add((kind, slug))
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
    for bad in (b"junk", gzip.compress(b"{}"), gzip.compress(b'{"t":"meta","version":99}\n'), gzip.compress(b""), gzip.compress(b'[1,2]\n')):
        with pytest.raises(ValueError):
            hub_sync.read_snapshot(bad)


def test_a_compression_bomb_is_refused_without_unpacking_it(monkeypatch):
    monkeypatch.setattr(hub_sync, "MAX_SNAPSHOT_BYTES", 10_000)
    bomb = gzip.compress(b'{"t":"meta","version":1}\n' + b"x" * 1_000_000)
    with pytest.raises(ValueError, match="larger"):
        hub_sync.read_snapshot(bomb)


def make_syncer(hub, store, env="staging", restore_from=""):
    clock = {"t": 1000.0}
    return hub_sync.Syncer(hub, store, env=env, restore_from=restore_from, clock=lambda: clock["t"]), clock


def snapshot_files(gate, env):
    return sorted(p for p in gate.files if p.startswith(f"memory/{env}/hub/"))


def test_sync_checks_storage_first_then_adds_a_new_snapshot_only_when_something_changed(gate, monkeypatch):
    stamps = iter(["20261002T100000Z", "20261002T200000Z", "20261003T100000Z"])
    monkeypatch.setattr(hub_sync, "stamp", lambda: next(stamps))
    store = make_store(gate)
    hub = FakeHub([note(1)])
    s, clock = make_syncer(hub, store)
    assert s.step().startswith("self-test: storage works")
    assert s.step().startswith("saved 1 learned notes")
    assert snapshot_files(gate, "staging") == ["memory/staging/hub/20261002T100000Z.jsonl.gz"]
    assert "exports/staging/hub-vault.zip" in gate.files and not s.ready()
    clock["t"] += hub_sync.EVERY + 1
    assert s.step() == "storage is up to date" and len(snapshot_files(gate, "staging")) == 1
    hub.items.append(note(2))
    clock["t"] += hub_sync.EVERY + 1
    assert s.step().startswith("saved 2 learned notes")
    assert snapshot_files(gate, "staging") == ["memory/staging/hub/20261002T200000Z.jsonl.gz", "memory/staging/hub/20261002T100000Z.jsonl.gz"][::-1]


def test_a_storage_failure_pauses_the_syncer_and_never_raises(gate):
    s, clock = make_syncer(FakeHub([note(1)]), AgentStore(GATE, "w" * 43, "staging", gate))
    assert s.step().startswith("paused 15 min") and not s.ready()
    clock["t"] += 901
    assert s.ready()


def put_snapshot(gate, env, hub, stamp="20261002T100000Z"):
    gate.files[f"memory/{env}/hub/{stamp}.jsonl.gz"] = hub_sync.snapshot_bytes(*hub.dump_learned())


def test_production_takes_in_the_newest_staging_snapshot_in_batches_insert_only_then_carries_on(gate):
    staging = FakeHub([note(i) for i in range(hub_sync.BATCH + 10)], [("example", "n1", "technique", "x", "example_of", ""),
                                                                      ("example", "ghost", "technique", "x", "example_of", "")])
    put_snapshot(gate, "staging", FakeHub([note(999)]), "20261001T000000Z")           # an older one is ignored
    put_snapshot(gate, "staging", staging, "20261002T000000Z")
    prod = FakeHub()
    prod.have.add(("example", "n5"))                                                   # already here: must not be touched
    s, _ = make_syncer(prod, make_store(gate, "production"), env="production", restore_from="staging")
    assert s.step().startswith("self-test")
    assert s.step() == f"restoring notes from staging: {hub_sync.BATCH} of {hub_sync.BATCH + 10}"
    done = s.step()
    assert done == f"restored {hub_sync.BATCH + 10 - 1} new notes (of {hub_sync.BATCH + 10}) and 1 of 2 links from staging"
    assert ("example", "n5", "agent", "active") not in prod.upserts and len(prod.upserts) == hub_sync.BATCH + 9
    nxt = s.step()
    assert nxt.startswith("saved") and snapshot_files(gate, "production") and "n999" not in str(prod.upserts)


def test_a_restore_never_creates_rules_or_skills_or_claims_seed_or_human_origin_or_retired_status(gate):
    evil = [note(1, kind="rule", origin="seed"), note(2, kind="skill"), note(3, origin="seed"), note(4, origin="human", status="retired"),
            note(5, origin="youtube"), note(6, status="weird")]
    gate.files["memory/staging/hub/20261002T000000Z.jsonl.gz"] = hub_sync.snapshot_bytes(evil, [])
    prod = FakeHub()
    s, _ = make_syncer(prod, make_store(gate, "production"), env="production", restore_from="staging")
    s.step()
    s.step()
    assert prod.upserts == [("example", "n3", "agent", "active"), ("example", "n4", "agent", "draft"), ("example", "n5", "youtube", "active"),
                            ("example", "n6", "agent", "draft")]


def test_a_hub_that_lost_its_data_takes_its_own_snapshot_back_before_it_can_overwrite_anything(gate):
    old_hub = FakeHub([note(i) for i in range(5)])
    put_snapshot(gate, "production", old_hub, "20261001T000000Z")
    fresh = FakeHub()                                                                  # an empty, newly rebuilt database
    s, _ = make_syncer(fresh, make_store(gate, "production"), env="production")
    s.step()
    said = s.step()
    assert said.startswith("restored 5 new notes") and len(fresh.upserts) == 5
    fresh.items = [note(i) for i in range(5)]
    assert s.step().startswith("saved 5 learned notes")
    assert snapshot_files(gate, "production")[0] == "memory/production/hub/20261001T000000Z.jsonl.gz"        # the earlier snapshot is untouched


def test_an_empty_hub_with_nothing_saved_yet_just_saves_and_a_missing_source_waits(gate):
    s, clock = make_syncer(FakeHub(), make_store(gate))
    s.step()
    assert s.step().startswith("saved 0 learned notes")
    other = FakeGate()                                                                 # a gate where staging has saved nothing
    p, _ = make_syncer(FakeHub(), make_store(other, "production"), env="production", restore_from="staging")
    p.step()
    assert p.step().startswith("nothing to restore yet") and not p.ready()


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


# ---------------------------------------------------------------- what goes into the vault

def test_the_vault_leaves_out_the_seeded_skill_manual_chunks_but_keeps_knowledge_and_references():
    import io
    import zipfile

    import hub_export
    items = [{"id": 1, "kind": "recipe", "slug": "manual-chunk", "title": "Manual chunk", "body": "x", "tags": [], "meta": {}, "origin": "seed", "status": "active", "confidence": None},
             {"id": 2, "kind": "skill", "slug": "a-skill", "title": "A skill", "body": "x", "tags": [], "meta": {}, "origin": "seed", "status": "active", "confidence": None},
             {"id": 3, "kind": "technique", "slug": "punch-in", "title": "Punch-in", "body": "x", "tags": [], "meta": {}, "origin": "seed", "status": "active", "confidence": None},
             {"id": 4, "kind": "recipe", "slug": "from-video", "title": "From a video", "body": "x", "tags": [], "meta": {}, "origin": "youtube", "status": "active", "confidence": None},
             {"id": 5, "kind": "example", "slug": "ref-1", "title": "A reference ad", "body": "x", "tags": [], "meta": {}, "origin": "foreplay", "status": "active", "confidence": None},
             {"id": 6, "kind": "rule", "slug": "r", "title": "A rule", "body": "x", "tags": [], "meta": {}, "origin": "seed", "status": "active", "confidence": None}]
    links = [(1, 3, "implements", ""), (4, 3, "implements", ""), (5, 3, "example_of", "")]
    keep, kept_links = hub_export.curate(items, links)
    assert [i["id"] for i in keep] == [3, 4, 5, 6] and [(l[0], l[1]) for l in kept_links] == [(4, 3), (5, 3)]
    names = zipfile.ZipFile(io.BytesIO(hub_export.vault_zip(items, links))).namelist()
    assert "recipe/manual-chunk.md" not in names and "technique/punch-in.md" in names and "example/ref-1.md" in names and "recipe/from-video.md" in names
    full = zipfile.ZipFile(io.BytesIO(hub_export.vault_zip(items, links, everything=True))).namelist()
    assert "recipe/manual-chunk.md" in full


def test_a_changed_vault_rule_makes_each_environment_rewrite_its_vault_once_without_a_new_snapshot(gate, monkeypatch):
    monkeypatch.setattr(hub_sync, "stamp", lambda: "20261002T100000Z")
    hub = FakeHub([note(1)])
    s, clock = make_syncer(hub, make_store(gate))
    s.step()
    s.step()
    assert "exports/staging/hub-vault.zip" in gate.files and hub.state["sync"]["vault"] == hub_sync.VAULT_VERSION
    gate.files["exports/staging/hub-vault.zip"] = b"old, everything-in-it vault"
    hub.state["sync"] = {k: v for k, v in hub.state["sync"].items() if k != "vault"}      # as if saved by the previous version
    clock["t"] += hub_sync.EVERY + 1
    assert s.step().startswith("saved 1 learned notes")
    assert gate.files["exports/staging/hub-vault.zip"] != b"old, everything-in-it vault" and len(snapshot_files(gate, "staging")) == 1
