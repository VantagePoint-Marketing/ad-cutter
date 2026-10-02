"""The agent's craft knowledge: every .md and .json file in worker/brain/, in name order, for the planning prompt.

These are guidelines for taste and structure. They never override the request, the format rules or the compliance
rules in the prompt, and the prompt says so. Edit or add files in brain/ to change what the agent knows; no code
change is needed.
"""
from __future__ import annotations

import re
from pathlib import Path

BRAIN_DIR = Path(__file__).resolve().parent / "brain"
MAX_CHARS = 16000


def load(directory: Path = BRAIN_DIR, limit: int = MAX_CHARS) -> str:
    parts = []
    for f in sorted(directory.glob("*")):
        if f.suffix not in {".md", ".json"} or not f.is_file():
            continue
        text = re.sub(r"<!--.*?-->", "", f.read_text(encoding="utf-8"), flags=re.S).strip()
        parts.append(f"### {f.name}\n\n{text}")
    return "\n\n".join(parts)[:limit] if parts else "(No craft notes yet.)"
