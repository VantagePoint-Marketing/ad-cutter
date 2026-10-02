"""Build worker/hub_seed/skills.jsonl.gz from the editing skills installed on this PC (run it here, commit the result).

    python tools/build_hub_seed.py [--skills "C:/Users/<you>/.claude/skills"]

Each skill's markdown is cut into notes at its headings: the skill's own front page becomes a `skill` note and every
other section a `recipe` note (a how-to the agent can retrieve). Every note records where it came from. The worker
imports the file when it starts (hub_import.py); the original skill folders are not needed on the server.

Licences: the HyperFrames project is Apache-2.0 and talking-head-recut is MIT (with its attribution kept in
worker/hub_seed/SOURCES.md); the other skill bundles carry no licence file here, so their notes are marked
`licence: to confirm` and Robert is asked to confirm before the product is used beyond this private repository.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hub  # noqa: E402

SKILLS = ["hyperframes", "hyperframes-core", "hyperframes-creative", "hyperframes-animation", "hyperframes-keyframes",
          "hyperframes-registry", "hyperframes-studio", "hyperframes-audio", "hyperframes-cli", "embedded-captions",
          "talking-head-recut", "motion-graphics", "general-video", "ffmpeg", "media-use"]
LICENCE = {"talking-head-recut": "MIT (vtake-skills by leeoxiang; adapted); notice kept in SOURCES.md",
           "hyperframes": "Apache-2.0 (the HyperFrames project); skill bundle licence to confirm"}
DEFAULT_LICENCE = "to confirm (no licence file in the skill folder; HyperFrames project is Apache-2.0)"
BASE_TAGS = {"hyperframes": ["hyperframes", "tools"], "hyperframes-core": ["hyperframes", "tools"],
             "hyperframes-creative": ["hyperframes", "typography", "color", "layout"],
             "hyperframes-animation": ["hyperframes", "motion", "gsap"], "hyperframes-keyframes": ["hyperframes", "motion", "framing"],
             "hyperframes-registry": ["hyperframes", "graphics"], "hyperframes-studio": ["hyperframes", "layout"],
             "hyperframes-audio": ["hyperframes", "sound"], "hyperframes-cli": ["hyperframes", "tools"],
             "embedded-captions": ["captions", "typography", "hyperframes"], "talking-head-recut": ["overlay", "talking-head", "hyperframes"],
             "motion-graphics": ["motion", "graphics", "hyperframes"], "general-video": ["hyperframes", "tools"],
             "ffmpeg": ["ffmpeg", "tools"], "media-use": ["tools", "sound", "hyperframes"]}
KEYWORD_TAGS = [(r"\bcaption", "captions"), (r"\bffmpeg\b", "ffmpeg"), (r"\bgsap\b", "gsap"), (r"transition", "transition"),
                (r"lower.third|callout|overlay", "overlay"), (r"\bchart|\bdataviz|count.?up", "graphics"),
                (r"kinetic|typograph|\bfont", "typography"), (r"\bloudnorm|\blufs\b|\bduck|\bmusic|\bsfx\b", "sound"),
                (r"safe.zone", "layout"), (r"\bzoom|\bpunch|keyframe", "motion"), (r"\bcta\b|call.to.action|end card", "cta"),
                (r"grade|\blut\b|palette", "color")]
MAX_CHUNK, MIN_CHUNK = 3200, 120
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def front_matter(text: str) -> tuple[dict, str]:
    m = FRONT.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"')
    return meta, text[m.end():]


def sections(text: str) -> list[tuple[str, str]]:
    """(heading, body) pieces, cut at markdown headings of level 1-3."""
    out, heading, buf = [], "", []
    for line in text.splitlines():
        m = re.match(r"^(#{1,3}) +(.*)", line)
        if m:
            if "".join(buf).strip():
                out.append((heading, "\n".join(buf).strip()))
            heading, buf = m.group(2).strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        out.append((heading, "\n".join(buf).strip()))
    return out


def split_long(body: str) -> list[str]:
    """Pieces of at most MAX_CHUNK characters, cut at blank lines."""
    if len(body) <= MAX_CHUNK:
        return [body]
    pieces, cur = [], ""
    for para in re.split(r"\n\s*\n", body):
        if cur and len(cur) + len(para) + 2 > MAX_CHUNK:
            pieces.append(cur)
            cur = ""
        while len(para) > MAX_CHUNK:                      # one huge paragraph or code block: cut it
            pieces.append(para[:MAX_CHUNK])
            para = para[MAX_CHUNK:]
        cur = (cur + "\n\n" + para) if cur else para
    if cur:
        pieces.append(cur)
    return pieces


def tags_for(skill: str, text: str) -> list[str]:
    tags = list(BASE_TAGS.get(skill, []))
    lowered = text.lower()
    for pattern, tag in KEYWORD_TAGS:
        if re.search(pattern, lowered) and tag not in tags:
            tags.append(tag)
    return hub.clean_tags(tags)


def build(skills_dir: Path) -> list[dict]:
    notes, seen = [], set()
    for skill in SKILLS:
        root = skills_dir / skill
        if not root.is_dir():
            print(f"missing skill folder: {skill}", file=sys.stderr)
            continue
        licence = LICENCE.get(skill, DEFAULT_LICENCE)
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            meta, body = front_matter(path.read_text(encoding="utf-8", errors="replace"))
            parts = sections(body)
            for n, (heading, text) in enumerate(parts):
                for k, piece in enumerate(split_long(text)):
                    if len(piece) < MIN_CHUNK and not (rel == "SKILL.md" and n == 0):
                        continue
                    first = rel == "SKILL.md" and n == 0 and k == 0
                    label = heading or (meta.get("name") or Path(rel).stem)
                    slug = hub.slugify(f"{skill}-{Path(rel).with_suffix('').as_posix()}-{label}-{n}-{k}", 110)
                    if slug in seen:
                        continue
                    seen.add(slug)
                    where = skill if rel == "SKILL.md" else f"{skill} / {Path(rel).stem}"
                    title = (meta.get("name") or skill) if first else f"{where}: {label}" + (f" ({k + 1})" if k else "")
                    body_text = ((meta.get("description", "") + "\n\n") if first and meta.get("description") else "") + piece
                    notes.append({"kind": "skill" if first else "recipe", "slug": slug, "title": title[:200], "body": body_text,
                                  "tags": tags_for(skill, title + " " + piece[:1500]),
                                  "meta": {"skill": skill, "source": f"{skill}/{rel}", "heading": heading, "licence": licence}})
    return notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skills", default=str(Path.home() / ".claude" / "skills"))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "hub_seed" / "skills.jsonl.gz"))
    args = ap.parse_args()
    notes = build(Path(args.skills))
    data = "\n".join(json.dumps(n, ensure_ascii=False, sort_keys=True) for n in notes).encode("utf-8")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as z:     # mtime 0: same input, same bytes
        z.write(data)
    kinds = {k: sum(1 for n in notes if n["kind"] == k) for k in ("skill", "recipe")}
    print(f"{len(notes)} notes {kinds}; {len(data) / 1e6:.2f} MB of text, {out.stat().st_size / 1e6:.2f} MB gzipped; "
          f"sha256 {hashlib.sha256(out.read_bytes()).hexdigest()[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
