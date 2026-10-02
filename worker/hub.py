"""The knowledge hub: one graph of notes the editing agent can search and follow, and that people can browse as an
Obsidian-style vault (hub_export.py).

    hub = Hub(connect)                                  # connect() -> a new autocommit psycopg connection
    tid = hub.upsert("technique", "punch-in", "Punch-in on the key word", body="...", tags=["motion", "emphasis"])
    hub.link(lesson_id, tid, "taught_by")
    hub.search("fast cuts with a number on screen", kinds=["technique", "recipe"], limit=5)
    hub.neighbors(tid)                                   # everything linked to it, with how

Everything stored is our own words with a pointer to where it came from (a video link and a time, a skill file). No
video and no transcript is ever kept. The schema is db/migrations/005_hub.sql; every SQL statement the hub runs is in
this file.
"""
from __future__ import annotations

import json
import re

KINDS = ("source", "lesson", "technique", "recipe", "skill", "rule", "example")
RELS = ("part_of", "taught_by", "implements", "uses", "derived_from", "example_of", "related", "supports", "contradicts")
ORIGINS = ("seed", "library", "youtube", "foreplay", "agent", "human")
STATUSES = ("active", "draft", "retired")
EVIDENCE_KINDS = ("used", "score", "feedback", "compare")
# The tags notes and searches use. A fixed list on purpose: the agent builds YouTube searches from these words only, so
# nothing it has seen about a client's footage can leak into a search.
TAGS = ("hook", "story", "pacing", "cut", "captions", "typography", "motion", "transition", "graphics", "overlay", "cta",
        "color", "sound", "music", "broll", "framing", "layout", "emphasis", "proof", "compliance", "format", "tools",
        "ffmpeg", "hyperframes", "gsap", "finance", "talking-head", "ads", "social", "meta", "performance")
SLUG_SAFE = re.compile(r"[^a-z0-9]+")
MAX_BODY = 12_000


def slugify(text: str, limit: int = 80) -> str:
    """A stable, file-name-safe slug: lower-case letters, digits and single dashes."""
    s = SLUG_SAFE.sub("-", (text or "").lower()).strip("-")
    return (s[:limit].rstrip("-")) or "item"


def clean_tags(tags) -> list[str]:
    """Tags as lower-case slugs, de-duplicated in order. Free tags are allowed (the list in TAGS is what searches use)."""
    out: list[str] = []
    for t in tags or []:
        s = slugify(str(t), 40)
        if s != "item" and s not in out:
            out.append(s)
    return out[:20]


def ref(item: dict) -> str:
    """The wiki-style reference for a note: kind/slug."""
    return f"{item['kind']}/{item['slug']}"


class Hub:
    def __init__(self, connect):
        self.connect = connect

    # ---------------------------------------------------------------- writing

    def upsert(self, kind: str, slug: str, title: str, body: str = "", tags=(), meta: dict | None = None,
               origin: str = "seed", status: str = "active", confidence: float | None = None) -> int:
        """Create the note or update it in place (kind + slug identify it). Returns its id."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        if origin not in ORIGINS or status not in STATUSES:
            raise ValueError("unknown origin or status")
        slug, tags = slugify(slug, 120), clean_tags(tags)
        title, body = (title or slug).strip()[:300], (body or "").strip()[:MAX_BODY]
        with self.connect() as c:
            row = c.execute(
                "insert into kb_items (kind, slug, title, body, tags, meta, origin, status, confidence, search) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "setweight(to_tsvector('english', %s), 'A') || setweight(to_tsvector('english', %s), 'B') || "
                "setweight(to_tsvector('english', %s), 'C')) "
                "on conflict (kind, slug) do update set title = excluded.title, body = excluded.body, tags = excluded.tags, "
                "meta = excluded.meta, origin = excluded.origin, status = excluded.status, "
                "confidence = coalesce(excluded.confidence, kb_items.confidence), search = excluded.search, updated_at = now() "
                "returning id",
                (kind, slug, title, body, tags, json.dumps(meta or {}), origin, status, confidence,
                 title, " ".join(tags), body)).fetchone()
        return int(row[0])

    def link(self, from_id: int, to_id: int, rel: str, note: str = "") -> None:
        if rel not in RELS:
            raise ValueError(f"unknown relation {rel!r}")
        if from_id == to_id:
            return
        with self.connect() as c:
            c.execute("insert into kb_links (from_id, to_id, rel, note) values (%s, %s, %s, %s) "
                      "on conflict (from_id, to_id, rel) do update set note = excluded.note", (from_id, to_id, rel, note[:300]))

    def set_status(self, item_id: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError("unknown status")
        with self.connect() as c:
            c.execute("update kb_items set status = %s, updated_at = now() where id = %s", (status, item_id))

    def add_evidence(self, item_id: int, kind: str, value: dict, job_id: str | None = None, ad_k: int | None = None) -> None:
        if kind not in EVIDENCE_KINDS:
            raise ValueError("unknown evidence kind")
        with self.connect() as c:
            c.execute("insert into kb_evidence (item_id, job_id, ad_k, kind, value) values (%s, %s, %s, %s, %s)",
                      (item_id, job_id, ad_k, kind, json.dumps(value)))

    # ---------------------------------------------------------------- reading

    COLS = "id, kind, slug, title, body, tags, meta, origin, status, confidence"

    @staticmethod
    def _item(row) -> dict:
        keys = ("id", "kind", "slug", "title", "body", "tags", "meta", "origin", "status", "confidence")
        item = dict(zip(keys, row))
        item["tags"] = list(item["tags"] or [])
        item["meta"] = item["meta"] if isinstance(item["meta"], dict) else json.loads(item["meta"] or "{}")
        return item

    def get(self, kind: str, slug: str) -> dict | None:
        with self.connect() as c:
            row = c.execute(f"select {self.COLS} from kb_items where kind = %s and slug = %s", (kind, slug)).fetchone()
        return self._item(row) if row else None

    def get_id(self, item_id: int) -> dict | None:
        with self.connect() as c:
            row = c.execute(f"select {self.COLS} from kb_items where id = %s", (item_id,)).fetchone()
        return self._item(row) if row else None

    def search(self, query: str = "", kinds: list[str] | None = None, tags: list[str] | None = None, limit: int = 8,
               snippet: int = 400) -> list[dict]:
        """Best matches first. `query` is plain text (any characters are safe); with no query, the newest notes that
        carry the tags. Only active notes. Each result has a short `snippet` instead of the whole body."""
        kinds = [k for k in (kinds or []) if k in KINDS]
        tags = clean_tags(tags)
        limit = max(1, min(int(limit), 50))
        where, params = ["status = 'active'"], []
        if kinds:
            where.append("kind = any(%s)")
            params.append(kinds)
        if tags:
            where.append("tags && %s")
            params.append(tags)
        query = (query or "").strip()[:300]
        if query:
            sql = (f"select {self.COLS}, ts_rank_cd(search, q) as rank from kb_items, websearch_to_tsquery('english', %s) q "
                   f"where search @@ q and {' and '.join(where)} order by rank desc, id limit %s")
            args = [query, *params, limit]
        else:
            sql = (f"select {self.COLS}, 0::real as rank from kb_items where {' and '.join(where)} "
                   "order by updated_at desc, id limit %s")
            args = [*params, limit]
        with self.connect() as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            item = self._item(r[:10])
            item["rank"] = float(r[10] or 0)
            item["snippet"] = item["body"][:snippet] + ("…" if len(item["body"]) > snippet else "")
            out.append(item)
        return out

    def neighbors(self, item_id: int) -> list[dict]:
        """Every note linked to this one, either way round, with the relation and its direction ('out' = this note
        points at it, 'in' = it points at this note)."""
        with self.connect() as c:
            rows = c.execute(
                f"select 'out', l.rel, l.note, i.id, i.kind, i.slug, i.title, i.tags from kb_links l join kb_items i on i.id = l.to_id "
                f"where l.from_id = %s and i.status = 'active' "
                f"union all "
                f"select 'in', l.rel, l.note, i.id, i.kind, i.slug, i.title, i.tags from kb_links l join kb_items i on i.id = l.from_id "
                f"where l.to_id = %s and i.status = 'active' order by 1, 2, 7", (item_id, item_id)).fetchall()
        return [{"direction": r[0], "rel": r[1], "note": r[2], "id": r[3], "kind": r[4], "slug": r[5], "title": r[6],
                 "tags": list(r[7] or [])} for r in rows]

    def evidence(self, item_id: int, limit: int = 50) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("select kind, value, job_id::text, ad_k, created_at from kb_evidence where item_id = %s "
                             "order by created_at desc limit %s", (item_id, limit)).fetchall()
        return [{"kind": r[0], "value": r[1], "job_id": r[2], "ad_k": r[3], "at": r[4].isoformat()} for r in rows]

    def dump_learned(self) -> tuple[list[dict], list[tuple]]:
        """Everything the agent learned (every note not seeded from our own files, any status) and the links from or to those notes,
        by (kind, slug) so another database can take them in. Evidence is left out."""
        with self.connect() as c:
            items = [self._item(r) for r in c.execute(
                f"select {self.COLS} from kb_items where origin <> 'seed' and slug not like 'selftest-%%' order by kind, slug").fetchall()]
            links = c.execute(
                "select a.kind, a.slug, b.kind, b.slug, l.rel, l.note from kb_links l join kb_items a on a.id = l.from_id "
                "join kb_items b on b.id = l.to_id where (a.origin <> 'seed' or b.origin <> 'seed') "
                "and a.slug not like 'selftest-%%' and b.slug not like 'selftest-%%' order by 1, 2, 3, 4, 5").fetchall()
        return items, [tuple(r) for r in links]

    def link_by_slug(self, from_kind: str, from_slug: str, to_kind: str, to_slug: str, rel: str, note: str = "") -> bool:
        """Link two notes named by kind and slug. False when either does not exist here (yet)."""
        if rel not in RELS:
            raise ValueError(f"unknown relation {rel!r}")
        with self.connect() as c:
            row = c.execute(
                "insert into kb_links (from_id, to_id, rel, note) select a.id, b.id, %s, %s from kb_items a, kb_items b "
                "where a.kind = %s and a.slug = %s and b.kind = %s and b.slug = %s and a.id <> b.id "
                "on conflict (from_id, to_id, rel) do update set note = excluded.note returning from_id",
                (rel, note[:300], from_kind, from_slug, to_kind, to_slug)).fetchone()
        return bool(row)

    def counts(self) -> dict[str, int]:
        """Active notes per kind, for the page's summary line."""
        with self.connect() as c:
            rows = c.execute("select kind, count(*) from kb_items where status = 'active' group by kind").fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def everything(self) -> tuple[list[dict], list[tuple]]:
        """All active notes and all links between them (for the export)."""
        with self.connect() as c:
            items = [self._item(r) for r in c.execute(
                f"select {self.COLS} from kb_items where status = 'active' order by kind, slug").fetchall()]
            links = c.execute("select l.from_id, l.to_id, l.rel, l.note from kb_links l "
                              "join kb_items a on a.id = l.from_id and a.status = 'active' "
                              "join kb_items b on b.id = l.to_id and b.status = 'active'").fetchall()
        return items, links

    def delete(self, kind: str, slug: str) -> None:
        with self.connect() as c:
            c.execute("delete from kb_items where kind = %s and slug = %s", (kind, slug))

    def get_state(self, key: str) -> dict:
        with self.connect() as c:
            row = c.execute("select value from kb_state where key = %s", (key,)).fetchone()
        return (row[0] if isinstance(row[0], dict) else json.loads(row[0])) if row else {}

    def set_state(self, key: str, value: dict) -> None:
        with self.connect() as c:
            c.execute("insert into kb_state (key, value) values (%s, %s) "
                      "on conflict (key) do update set value = excluded.value, updated_at = now()", (key, json.dumps(value)))

    def _clean_selftest(self) -> None:
        with self.connect() as c:
            c.execute("delete from kb_items where kind = 'rule' and slug like 'selftest-%'")
            c.execute("delete from kb_state where key = '_selftest'")
            c.execute("delete from kb_goals where goal = 'selftest goal'")

    def selftest(self) -> str:
        """Write, search, link, read and delete two throw-away notes on the real database, to prove every statement the hub
        runs is valid there. Raises on the first problem; leaves nothing behind."""
        a = b = None
        self._clean_selftest()                          # leftovers of an interrupted run
        try:
            a = self.upsert("rule", "selftest-a", "Selftest note alpha", "zebra quartz marmalade", ["hook", "pacing"], origin="agent")
            b = self.upsert("rule", "selftest-b", "Selftest note beta", "unrelated words", ["cut"], origin="agent")
            self.upsert("rule", "selftest-a", "Selftest note alpha", "zebra quartz marmalade", ["hook", "pacing"], origin="agent")   # an update
            self.link(a, b, "related", "selftest")
            self.add_evidence(a, "used", {"selftest": True})
            found = self.search("zebra marmalade", kinds=["rule"], tags=["hook"], limit=3)
            if [x["id"] for x in found] != [a]:
                raise RuntimeError(f"search returned {[x['id'] for x in found]}, expected [{a}]")
            if not self.search("", kinds=["rule"], tags=["cut"], limit=3):
                raise RuntimeError("the tag-only search found nothing")
            if [n["id"] for n in self.neighbors(a)] != [b] or [n["id"] for n in self.neighbors(b)] != [a]:
                raise RuntimeError("the link did not show from both sides")
            items, links = self.everything()
            if not {a, b} <= {i["id"] for i in items} or not any(l[0] == a and l[1] == b for l in links):
                raise RuntimeError("everything() missed the test notes")
            self.set_state("_selftest", {"ok": True})
            if self.get_state("_selftest") != {"ok": True}:
                raise RuntimeError("state did not round-trip")
            if not self.evidence(a):
                raise RuntimeError("evidence was not stored")
            self.counts()
            learned, learned_links = self.dump_learned()
            if not isinstance(learned, list) or not isinstance(learned_links, list):
                raise RuntimeError("dump_learned returned the wrong shape")
            if not self.link_by_slug("rule", "selftest-a", "rule", "selftest-b", "supports", "by slug") or \
                    self.link_by_slug("rule", "selftest-a", "rule", "no-such-note", "supports"):
                raise RuntimeError("link_by_slug did not behave")
            gid = self.add_goal("selftest goal", "tutorial", ["selftest"], asked_by="agent")
            if gid is None or gid not in [g["id"] for g in self.goals(200)]:
                raise RuntimeError("the goals list did not show the new goal")
            if not self.skip_goal(gid):
                raise RuntimeError("the new goal could not be removed")
            if self.add_goal("selftest goal", "tutorial", ["selftest"], asked_by="agent") != gid:      # a removed goal can be asked for again
                raise RuntimeError("a removed goal was not reopened")
            self.skip_goal(gid)
            return "hub SQL works on this database"
        finally:
            self._clean_selftest()

    # ---------------------------------------------------------------- goals (what the agent wants to learn)

    def goals(self, limit: int = 60) -> list[dict]:
        """What the agent is learning or has learned: working first, then waiting, then finished, newest first."""
        with self.connect() as c:
            rows = c.execute(
                "select id, goal, kind, status, reason, asked_by, attempts, result, updated_at from kb_goals "
                "order by case status when 'working' then 0 when 'open' then 1 else 2 end, updated_at desc, id desc limit %s",
                (max(1, min(int(limit), 200)),)).fetchall()
        out = []
        for r in rows:
            result = r[7] if isinstance(r[7], dict) else json.loads(r[7] or "{}")
            out.append({"id": r[0], "goal": r[1], "kind": r[2], "status": r[3], "reason": r[4], "asked_by": r[5], "attempts": r[6],
                        "added": result.get("added") or {}, "cost_usd": result.get("cost_usd"), "at": r[8].isoformat()})
        return out

    def skip_goal(self, goal_id: int) -> bool:
        """Take a goal off the list (not one being worked on right now). True when it changed."""
        with self.connect() as c:
            row = c.execute("update kb_goals set status = 'skipped', reason = 'removed by staff', updated_at = now() "
                            "where id = %s and status <> 'working' returning id", (goal_id,)).fetchone()
        return bool(row)

    def add_goal(self, goal: str, kind: str = "tutorial", queries: list[str] | None = None, asked_by: str = "agent") -> int | None:
        """A new learning goal; None when the same goal is already waiting, running or done. A goal that was removed or failed is
        reopened."""
        if kind not in ("tutorial", "reference", "ads"):
            raise ValueError("unknown goal kind")
        with self.connect() as c:
            row = c.execute("insert into kb_goals (goal, kind, queries, asked_by) values (%s, %s, %s, %s) "
                            "on conflict (kind, lower(goal)) do update set status = 'open', reason = null, attempts = 0, "
                            "result = '{}'::jsonb, queries = excluded.queries, asked_by = excluded.asked_by, updated_at = now() "
                            "where kb_goals.status in ('skipped', 'failed') returning id",
                            (goal.strip()[:300], kind, [q.strip()[:120] for q in (queries or [])][:6], asked_by)).fetchone()
        return int(row[0]) if row else None
