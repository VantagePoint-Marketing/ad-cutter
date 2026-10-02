"""The knowledge hub: notes, search, export, and the maintainer that fills it. The SQL itself runs against the real
database in Hub.selftest() when the worker starts (see the worker's log); here a stand-in connection checks the
statements are built with the right shape and the logic around them."""
import gzip
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hub as hubmod  # noqa: E402
import hub_export  # noqa: E402
import hub_import  # noqa: E402
from hub import Hub  # noqa: E402


# ---------------------------------------------------------------- pure helpers

@pytest.mark.parametrize("text, slug", [("Punch-in on the key word", "punch-in-on-the-key-word"), ("  A & B!! ", "a-b"),
                                        ("", "item"), ("ÀÉ ünï", "n"), ("x" * 200, "x" * 80)])
def test_slugify_makes_safe_names(text, slug):
    assert hubmod.slugify(text) == slug


def test_tags_are_slugs_unique_and_limited():
    tags = hubmod.clean_tags(["Hook", "hook", " Captions ", "a b"])
    assert tags == ["hook", "captions", "a-b"]
    assert len(hubmod.clean_tags([f"t{i}" for i in range(50)])) == 20


# ---------------------------------------------------------------- SQL shape, with a recording stand-in connection

class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self, log, rows=()):
        self.log, self.rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))
        return Cursor(list(self.rows))


def hub_with(rows=()):
    log = []
    return Hub(lambda: Conn(log, rows)), log


def test_upsert_checks_its_input_and_writes_one_statement_with_every_value():
    hub, log = hub_with([(7,)])
    assert hub.upsert("technique", "Punch In!", "Punch-in", "body", ["Motion", "motion"], {"find": "zoom"}) == 7
    sql, params = log[0]
    assert sql.count("%s") == len(params) == 12 and "on conflict (kind, slug) do update" in sql
    assert params[0] == "technique" and params[1] == "punch-in" and params[4] == ["motion"] and json.loads(params[5]) == {"find": "zoom"}
    with pytest.raises(ValueError):
        hub.upsert("nonsense", "a", "t")                                  # an unknown kind
    with pytest.raises(ValueError):
        hub.upsert("technique", "a", "t", origin="unknown")                # an unknown origin
    with pytest.raises(ValueError):
        hub.upsert("technique", "a", "t", status="deleted")                # an unknown status
    assert len(log) == 1                                              # nothing was written for the bad ones


def test_upsert_cuts_long_text():
    hub, log = hub_with([(1,)])
    hub.upsert("recipe", "r", "t" * 500, "b" * 50_000)
    params = log[0][1]
    assert len(params[2]) == 300 and len(params[3]) == hubmod.MAX_BODY


def test_search_builds_a_safe_query_with_the_filters_it_was_given():
    hub, log = hub_with()
    hub.search("fast cuts; drop table kb_items --", kinds=["technique", "bogus"], tags=["Hook"], limit=500)
    sql, params = log[0]
    assert "websearch_to_tsquery('english', %s)" in sql and "kind = any(%s)" in sql and "tags && %s" in sql
    assert params[0] == "fast cuts; drop table kb_items --" and params[1] == ["technique"] and params[2] == ["hook"] and params[-1] == 50
    assert "drop table" not in sql                                    # the text only ever travels as a parameter
    hub.search("", tags=["cut"])
    sql, params = log[1]
    assert "websearch_to_tsquery" not in sql and "order by updated_at desc" in sql and params == [["cut"], 8]


def test_link_refuses_unknown_relations_and_self_links():
    hub, log = hub_with()
    with pytest.raises(ValueError):
        hub.link(1, 2, "friends")
    hub.link(3, 3, "related")
    assert log == []
    hub.link(1, 2, "implements", "x" * 500)
    assert len(log[0][1][3]) == 300


def test_search_results_carry_a_short_snippet():
    row = (1, "recipe", "r", "Title", "x" * 1000, ["hook"], {"a": 1}, "seed", "active", None, 0.4)
    hub, _ = hub_with([row])
    found = hub.search("title", snippet=50)
    assert found[0]["snippet"] == "x" * 50 + "…" and found[0]["meta"] == {"a": 1} and found[0]["rank"] == 0.4


def test_goals_are_deduplicated_by_the_database_and_validated_here():
    hub, log = hub_with([(5,)])
    assert hub.add_goal("kinetic captions for finance ads", "tutorial", ["captions", "motion"]) == 5
    assert "on conflict (kind, lower(goal)) do nothing" in log[0][0]
    with pytest.raises(ValueError):
        hub.add_goal("x", "podcast")
    empty, _ = hub_with([])
    assert empty.add_goal("again") is None


# ---------------------------------------------------------------- the export

def sample():
    items = [{"id": 1, "kind": "technique", "slug": "punch-in", "title": "Punch-in: zoom [on] word", "body": "Scale up 10%.",
              "tags": ["motion"], "meta": {"url": "https://x.test/a: b"}, "origin": "seed", "status": "active", "confidence": 0.8},
             {"id": 2, "kind": "lesson", "slug": "yt-abc-41", "title": "Pacing", "body": "Cut dead air.", "tags": [],
              "meta": {}, "origin": "library", "status": "active", "confidence": None},
             {"id": 3, "kind": "recipe", "slug": "gsap-scale", "title": "GSAP scale", "body": "tl.to(...)", "tags": ["gsap"],
              "meta": {}, "origin": "seed", "status": "active", "confidence": None}]
    links = [(1, 2, "taught_by", "from the lesson"), (3, 1, "implements", ""), (1, 99, "related", "")]
    return items, links


def test_the_vault_has_a_note_per_item_with_links_both_ways_and_an_index():
    files = hub_export.vault_files(*sample())
    assert set(files) >= {"technique/punch-in.md", "lesson/yt-abc-41.md", "recipe/gsap-scale.md", "README.md",
                          "_index/technique.md", "_index/lesson.md", "_index/recipe.md"}
    note = files["technique/punch-in.md"]
    assert note.startswith("---\ntitle: ") and "kind: technique" in note and "tags: [motion]" in note and "confidence: 0.80" in note
    assert "[[lesson/yt-abc-41|Pacing]]: from the lesson" in note and "**Taught in**" in note       # outgoing, with its reason
    assert "[[recipe/gsap-scale|GSAP scale]]" in note and "**Implemented by**" in note            # incoming, worded from this side
    assert "[[technique/punch-in|Punch-in: zoom  on  word]]" in files["lesson/yt-abc-41.md"]       # brackets in a title cannot break a link
    assert "**Teaches**" in files["lesson/yt-abc-41.md"]
    assert "99" not in note                                                                        # a link to a missing note is dropped
    assert 'url: "https://x.test/a: b"' in note                                                    # front matter values are quoted when needed


def test_the_zip_opens_and_holds_every_file():
    data = hub_export.vault_zip(*sample())
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None and "technique/punch-in.md" in z.namelist()
    assert "# Ad Cutter knowledge hub" in z.read("README.md").decode("utf-8")


def test_yaml_text_quotes_what_needs_it():
    assert hub_export.yaml_text("plain") == "plain"
    assert hub_export.yaml_text('a: "b"') == '"a: \\"b\\""'
    assert hub_export.yaml_text("") == '""' and hub_export.yaml_text(None) == '""'


# ---------------------------------------------------------------- the files that fill the hub

def test_the_technique_file_is_complete_and_uses_the_agreed_tags():
    techniques = hub_import.read_techniques()
    assert len(techniques) >= 35 and len({t["slug"] for t in techniques}) == len(techniques)
    for t in techniques:
        assert t["title"] and len(t["body"]) > 60 and t["find"] and t["tags"]
        assert set(t["tags"]) <= set(hubmod.TAGS), t["slug"]
    slugs = {t["slug"] for t in techniques}
    assert set(hub_import.LEVER_TECHNIQUE.values()) <= slugs                    # every lever maps to a technique that exists


def test_the_skills_seed_is_present_unique_and_licence_marked():
    notes = hub_import.read_seed()
    assert len(notes) > 1500 and len({n["slug"] for n in notes}) == len(notes)
    assert {n["kind"] for n in notes} == {"skill", "recipe"}
    assert all(n["meta"].get("licence") and n["meta"].get("source") for n in notes)
    assert all(len(n["body"]) <= 3300 and n["title"] for n in notes)
    assert {"hyperframes-animation", "embedded-captions", "ffmpeg"} <= {n["meta"]["skill"] for n in notes}
    assert any("MIT" in n["meta"]["licence"] for n in notes if n["meta"]["skill"] == "talking-head-recut")


def test_studied_lessons_become_linked_notes_with_a_link_to_the_moment():
    note = {"summary": "About pacing.", "lessons": [
        {"at_s": 41, "topic": "Pacing", "principle": "Cut dead air.", "lever": "pause_trim", "how_we_apply": "Trim pauses."},
        {"at_s": 90.7, "topic": "", "principle": "Open on the payoff.", "lever": "hook", "how_we_apply": ""},
        {"at_s": 5, "topic": "Music", "principle": "Pick music late.", "lever": "none", "how_we_apply": "Cannot."},
        {"at_s": 7, "topic": "empty", "principle": "", "lever": "hook"}]}
    sources, lessons = hub_import.lesson_notes([("abcDEFghiJK", "Editing ruins", "Chan", 600, 1, "foundation", note)])
    assert [s["slug"] for s in sources] == ["yt-abcDEFghiJK"] and sources[0]["summary"] == ["About pacing."]
    assert sources[0]["meta"]["url"] == "https://www.youtube.com/watch?v=abcDEFghiJK"
    by_slug = {l["slug"]: l for l in lessons}
    assert set(by_slug) == {"yt-abcDEFghiJK-41", "yt-abcDEFghiJK-90", "yt-abcDEFghiJK-5"}        # the empty lesson is dropped
    a = by_slug["yt-abcDEFghiJK-41"]
    assert a["technique"] == "jump-cut-pacing" and a["meta"]["url"].endswith("&t=41s") and "How we would apply it: Trim pauses." in a["body"]
    assert by_slug["yt-abcDEFghiJK-90"]["title"] == "Open on the payoff." and by_slug["yt-abcDEFghiJK-90"]["technique"] == "cold-open-hook"
    assert by_slug["yt-abcDEFghiJK-5"]["technique"] is None and by_slug["yt-abcDEFghiJK-5"]["tags"] == ["tools"]


# ---------------------------------------------------------------- the maintainer's order of work

class FakeHub:
    """Stands in for Hub: remembers notes, links and state in memory."""

    def __init__(self):
        self.items, self.links, self.state, self.tested = {}, [], {}, 0

    def selftest(self):
        self.tested += 1
        return "ok"

    def upsert(self, kind, slug, title, body="", tags=(), meta=None, origin="seed", **kw):
        self.items[(kind, slug)] = {"id": len(self.items) + 1, "kind": kind, "slug": slug, "title": title, "tags": list(tags)}
        return self.items[(kind, slug)]["id"]

    def get(self, kind, slug):
        return self.items.get((kind, slug))

    def search(self, query, kinds=None, tags=None, limit=8):
        return [v for (k, _), v in self.items.items() if k in (kinds or [k])][:limit]

    def link(self, a, b, rel, note=""):
        self.links.append((a, b, rel))

    def get_state(self, key):
        return dict(self.state.get(key, {}))

    def set_state(self, key, value):
        self.state[key] = dict(value)


def seed_dir(tmp_path, n=5):
    (tmp_path / "techniques.json").write_text(json.dumps([
        {"slug": "cold-open-hook", "title": "Hook", "body": "b" * 70, "tags": ["hook"], "find": "hook"},
        {"slug": "jump-cut-pacing", "title": "Pace", "body": "b" * 70, "tags": ["pacing"], "find": "pace"}]), encoding="utf-8")
    notes = [{"kind": "recipe", "slug": f"r{i}", "title": f"R{i}", "body": "x", "tags": ["gsap"], "meta": {}} for i in range(n)]
    with gzip.open(tmp_path / "skills.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(x) for x in notes))
    return tmp_path


class Clock:
    t = 100_000.0

    def __call__(self):
        return self.t


def maintainer(tmp_path, monkeypatch, n=5, batch=2):
    monkeypatch.setattr(hub_import, "import_library", lambda hub, connect: (3, 9))
    clock, fake = Clock(), FakeHub()
    return hub_import.Maintainer(lambda: None, fake, seed_dir(tmp_path, n), clock, batch), fake, clock


def run_to_idle(m, limit=30):
    said = []
    for _ in range(limit):
        said.append(m.step())
        if said[-1] == "hub up to date":
            return said
    raise AssertionError(said)


def test_the_maintainer_tests_the_database_then_loads_techniques_then_the_skills_in_batches_then_links_then_the_lessons(tmp_path, monkeypatch):
    m, fake, clock = maintainer(tmp_path, monkeypatch)
    said = run_to_idle(m)
    assert said[0] == "self-test: ok" and said[1] == "2 techniques loaded"
    assert said[2:5] == ["skills loading: 2 of 5", "skills loading: 4 of 5", "skills loaded: 5 notes"]
    assert said[5].startswith("linked recipes to techniques: ") and said[6] == "studied lessons in the hub: 3 videos, 9 lessons"
    assert sum(1 for k in fake.items if k[0] == "recipe") == 5 and sum(1 for k in fake.items if k[0] == "technique") == 2
    assert fake.links and all(rel == "implements" for _, _, rel in fake.links) and fake.tested == 1
    assert m.ready() is False                                              # nothing to do: it rests


def test_nothing_is_repeated_after_a_restart_and_a_changed_seed_is_reloaded(tmp_path, monkeypatch):
    m, fake, clock = maintainer(tmp_path, monkeypatch)
    run_to_idle(m)
    again = hub_import.Maintainer(lambda: None, fake, tmp_path, clock, 2)         # a new worker, same database
    again.checked = True
    again.library_at = clock.t
    assert again.step() == "hub up to date"
    notes = [{"kind": "recipe", "slug": "extra", "title": "E", "body": "x", "tags": [], "meta": {}}]
    with gzip.open(tmp_path / "skills.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write(json.dumps(notes[0]))
    assert again.step() == "skills loaded: 1 notes" and ("recipe", "extra") in fake.items


def test_the_hub_looks_for_new_lessons_every_half_hour_and_rests_between(tmp_path, monkeypatch):
    m, fake, clock = maintainer(tmp_path, monkeypatch)
    run_to_idle(m)
    clock.t += 301
    assert m.ready() and m.step() == "hub up to date"
    clock.t += hub_import.LIBRARY_EVERY
    assert m.step().startswith("studied lessons in the hub")


def test_a_failing_step_pauses_the_hub_and_never_raises(tmp_path, monkeypatch):
    m, fake, clock = maintainer(tmp_path, monkeypatch)

    def boom():
        raise RuntimeError("relation kb_items does not exist")
    fake.selftest = boom
    said = m.step()
    assert said.startswith("paused 10 min: RuntimeError") and not m.ready()
    clock.t += 601
    assert m.ready()


def test_the_selftest_failure_is_reported_loudly_not_swallowed_into_a_pass(tmp_path, monkeypatch):
    m, fake, clock = maintainer(tmp_path, monkeypatch)
    fake.selftest = lambda: (_ for _ in ()).throw(RuntimeError("search returned [], expected [4]"))
    assert "search returned" in m.step()


# ---------------------------------------------------------------- what the planner is told

import ad_cutter as ac  # noqa: E402
import hub_context  # noqa: E402


class SearchHub:
    def __init__(self, by_kind):
        self.by_kind, self.asked = by_kind, []

    def search(self, query="", kinds=None, tags=None, limit=8):
        self.asked.append((query, tuple(kinds or ()), limit))
        return self.by_kind.get((kinds or [""])[0], [])[:limit]


def note(i, kind, title, body, **meta):
    return {"id": i, "kind": kind, "slug": f"s{i}", "title": title, "body": body, "tags": [], "meta": meta}


def test_the_plan_gets_techniques_rules_and_lessons_without_repeats_and_within_the_limit():
    hub = SearchHub({"technique": [note(1, "technique", "Hook", "Open on the payoff. " * 40), note(2, "technique", "Pace", "Cut dead air.")],
                     "rule": [note(3, "rule", "Open on the question", "House rule.")],
                     "lesson": [note(4, "lesson", "Pacing", 'Keep "breathing room"\n\nbetween cuts.', channel="HillierSmith")]})
    text = hub_context.fetch(hub, "short ads about lag")
    lines = text.splitlines()
    assert lines[0].startswith("- Technique, Hook: Open on the payoff.") and len(lines[0]) < 320 and lines[0].endswith("…")
    assert sum(1 for l in lines if "Technique, Hook" in l) == 1                               # the same note is not listed twice
    assert any(l.startswith("- Rule, Open on the question") for l in lines)
    assert any(l.startswith("- Lesson from a video (HillierSmith): Keep 'breathing room' between cuts.") for l in lines)
    assert hub.asked[0][0] == "short ads about lag" and hub.asked[0][1] == ("technique",)
    assert any(q == hub_context.CORE_QUERY for q, _, _ in hub.asked)                            # core craft help even for a vague request


def test_an_empty_request_still_gets_the_core_techniques_and_an_empty_hub_says_so():
    hub = SearchHub({"technique": [note(1, "technique", "Hook", "Open on the payoff.")]})
    assert hub_context.fetch(hub, "").startswith("- Technique, Hook")
    assert hub_context.fetch(SearchHub({}), "anything") == hub_context.EMPTY


def test_the_knowledge_block_is_cut_to_its_budget():
    many = [note(i, "technique", f"T{i}", "x" * 250) for i in range(1, 60)]
    text = hub_context.fetch(SearchHub({"technique": many}), "q", max_chars=900)
    assert len(text) <= 900 and text.count("\n") >= 1


def test_a_broken_hub_gives_the_planner_less_help_never_an_error():
    class Broken:
        def search(self, *a, **k):
            raise RuntimeError("relation kb_items does not exist")
    assert hub_context.fetch(Broken(), "q") == hub_context.EMPTY


def test_the_plan_prompt_carries_the_knowledge_block_as_hints():
    words = [{"w": "hello", "s": 0.0, "e": 0.4}, {"w": "world", "s": 0.5, "e": 0.9}]
    cfg = {"brand": {"name": "VP", "product": "p", "audience": "a"}, "ad_count": 3, "ad_min_seconds": 20, "ad_max_seconds": 75}
    plain = ac.build_prompt(cfg, words)
    assert "## What the agent has learned about editing" in plain and "(Nothing relevant yet.)" in plain
    block = "- Technique, Hook: Open on the payoff. {braces}"
    p = ac.build_prompt(cfg, words, "req", None, "", block)
    assert block in p and "never override the request" in p and p.index(block) < p.index("## The footage")


from test_jobs import cancellable, env, fake_run, job  # noqa: E402,F401
import jobs  # noqa: E402


def test_the_job_gives_the_planner_what_the_hub_knows_for_the_typed_request(monkeypatch, env):
    log, cfg, bucket, conn = env
    asked = []
    monkeypatch.setattr(hub_context, "fetch", lambda hub, request: asked.append(request) or "- Technique, Hook: x")
    monkeypatch.setattr(ac, "run_pipeline", fake_run())
    opts = {"brief": "lead with lag\n\nMake 3 ads.", "request": "lead with lag"}
    assert jobs.run_job(conn, bucket, job(options=opts), cfg) == "ready"
    assert ac.run_pipeline.kwargs["knowledge"] == "- Technique, Hook: x"
    assert asked == ["lead with lag"]                                       # what the person typed, not the added sentence


# ---------------------------------------------------------------- references for the finished-ad check

def test_references_are_a_few_examples_with_their_ids_and_a_note_of_how_long_an_ad_ran():
    hub = SearchHub({"example": [note(7, "example", 'A "long" runner', "**Hook:** blunt question " * 40, running_days=80),
                                 note(8, "example", "A short", "Fast cuts.")]})
    text, ids = hub_context.fetch_references(hub, "ads about lag")
    assert ids == [7, 8] and "an ad that has run 80 days" in text and "a short video that performs well" in text
    assert len(text) <= hub_context.REF_CHARS and '"long"' not in text and "\n\n" not in text
    assert hub_context.fetch_references(SearchHub({}), "x") == ("", [])
    assert hub_context.fetch_references(object(), "x") == ("", [])                           # a broken hub never fails a job


def test_each_checked_ad_leaves_a_comparison_note_on_the_references_it_was_set_against():
    import jobs

    class Rec:
        def __init__(self, connect):
            Rec.seen = []

        def add_evidence(self, item_id, kind, value, job_id=None, ad_k=None):
            Rec.seen.append((item_id, kind, value, job_id, ad_k))
    orig, jobs.Hub = jobs.Hub, Rec
    try:
        review = {"scores": {"hook": 4, "cuts": 2}, "vs_references": "Slower than the references."}
        jobs.record_comparisons(None, "j1", [7, 8], [{"k": 1, "review": review}, {"k": 2, "review": None}])
    finally:
        jobs.Hub = orig
    assert [(s[0], s[1], s[3], s[4]) for s in Rec.seen] == [(7, "compare", "j1", 1), (8, "compare", "j1", 1)]
    assert Rec.seen[0][2] == {"avg": 3.0, "hook": 4, "vs": "Slower than the references."}
    jobs.Hub = lambda c: (_ for _ in ()).throw(RuntimeError("db down"))
    try:
        jobs.record_comparisons(None, "j1", [7], [{"k": 1, "review": review}])               # swallowed
    finally:
        jobs.Hub = orig
