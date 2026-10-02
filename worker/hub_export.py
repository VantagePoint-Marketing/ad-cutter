"""The hub as an Obsidian-style vault: one markdown file per note (kind/slug.md) with front matter and [[wiki links]]
between notes, packed in a zip. Pure functions, no database: the caller reads the notes and links (hub.Hub.everything)
and passes them in, so the page's server and the worker share this file.

Open the unzipped folder as a vault in Obsidian and the links, tags and graph view work as they do for any other notes.
"""
from __future__ import annotations

import io
import re
import zipfile
from collections import defaultdict

REL_WORDS = {"part_of": "Part of", "taught_by": "Taught in", "implements": "Implements", "uses": "Uses",
             "derived_from": "Derived from", "example_of": "Example of", "related": "Related", "supports": "Supports",
             "contradicts": "Contradicts"}
INVERSE = {"part_of": "Contains", "taught_by": "Teaches", "implements": "Implemented by", "uses": "Used by",
           "derived_from": "Source of", "example_of": "Examples", "related": "Related", "supports": "Supported by",
           "contradicts": "Contradicted by"}
_YAML_UNSAFE = re.compile(r"[:#\[\]{}&*!|>'\"%@`\n]")


def yaml_text(value) -> str:
    """A scalar safe to put after a key in front matter."""
    s = str(value if value is not None else "")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"' if _YAML_UNSAFE.search(s) or not s else s


def wikilink(item: dict) -> str:
    # a vertical bar or bracket in a title would break the link syntax
    title = re.sub(r"[\[\]|]", " ", item["title"]).strip()
    return f"[[{item['kind']}/{item['slug']}|{title}]]"


def note_markdown(item: dict, outgoing: list[tuple[str, dict, str]], incoming: list[tuple[str, dict, str]]) -> str:
    """One note: front matter, the body, then its links grouped by relation."""
    meta = item.get("meta") or {}
    fm = ["---", f"title: {yaml_text(item['title'])}", f"kind: {item['kind']}", f"origin: {item['origin']}"]
    if item.get("tags"):
        fm.append("tags: [" + ", ".join(item["tags"]) + "]")
    for key in ("url", "channel", "licence", "source"):
        if meta.get(key):
            fm.append(f"{key}: {yaml_text(meta[key])}")
    if item.get("confidence") is not None:
        fm.append(f"confidence: {item['confidence']:.2f}")
    fm.append("---")
    lines = [*fm, "", f"# {item['title']}", "", item.get("body", "").strip(), ""]
    grouped: dict[str, list[str]] = defaultdict(list)
    for rel, other, note in outgoing:
        grouped[REL_WORDS.get(rel, rel)].append(wikilink(other) + (f": {note}" if note else ""))
    for rel, other, note in incoming:
        grouped[INVERSE.get(rel, rel)].append(wikilink(other) + (f": {note}" if note else ""))
    if grouped:
        lines.append("## Links")
        for label in sorted(grouped):
            lines += ["", f"**{label}**", *[f"- {x}" for x in sorted(set(grouped[label]))]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def vault_files(items: list[dict], links: list[tuple]) -> dict[str, str]:
    """{path inside the vault: markdown} for every note, plus an index note per kind and a README."""
    by_id = {it["id"]: it for it in items}
    out_links, in_links = defaultdict(list), defaultdict(list)
    for from_id, to_id, rel, note in links:
        if from_id in by_id and to_id in by_id:
            out_links[from_id].append((rel, by_id[to_id], note))
            in_links[to_id].append((rel, by_id[from_id], note))
    files: dict[str, str] = {}
    kinds: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        files[f"{it['kind']}/{it['slug']}.md"] = note_markdown(it, out_links[it["id"]], in_links[it["id"]])
        kinds[it["kind"]].append(it)
    for kind, group in kinds.items():
        files[f"_index/{kind}.md"] = f"# {kind.capitalize()} notes ({len(group)})\n\n" + "\n".join(
            f"- {wikilink(it)}" for it in sorted(group, key=lambda x: x["title"].lower())) + "\n"
    files["README.md"] = (
        "# Ad Cutter knowledge hub\n\nExported from the editing agent's knowledge hub: what it has learned from videos, ads and "
        "skills, in our own words, with links between notes. Open this folder as a vault in Obsidian.\n\n"
        + "\n".join(f"- [[_index/{k}|{k.capitalize()} ({len(v)})]]" for k, v in sorted(kinds.items())) + "\n")
    return files


def vault_zip(items: list[dict], links: list[tuple]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in sorted(vault_files(items, links).items()):
            z.writestr(path, text)
    return buf.getvalue()
