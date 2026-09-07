"""Hand corrections to the roles the detector guessed.

Role detection is typographic, so it is wrong sometimes: an unlabelled line of
dialogue, a character whose name the text never states, narration that happens
to open with a dash. Those are judgement calls a person makes in a second and a
heuristic cannot make at all.

**Corrections are keyed by `source_ref`, not by chunk id.** Chunk ids encode
position, so re-chunking a book renumbers everything and would strand every
correction. `source_ref` names the paragraph in the source document, which is
stable as long as the source is. That is the whole reason this is a separate
file rather than an edit to `chunks.jsonl`: the manifest is regenerated, and
these must outlive it.

Stored at `data/book/<slug>/role_overrides.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

FILENAME = "role_overrides.json"


def path_for(book_dir: Path) -> Path:
    return book_dir / FILENAME


def load(book_dir: Path) -> dict[str, str]:
    """source_ref -> role. Empty when nothing has been corrected."""
    path = path_for(book_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.get("roles", raw).items()
            if isinstance(v, str) and v.strip()}


def save(book_dir: Path, roles: dict[str, str]) -> Path:
    book_dir.mkdir(parents=True, exist_ok=True)
    path = path_for(book_dir)
    payload = {
        "_comment": "Hand corrections to detected roles, keyed by source_ref so "
                    "they survive re-chunking. Written by the dashboard.",
        "roles": dict(sorted(roles.items())),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def set_role(book_dir: Path, source_ref: str, role: str) -> dict[str, str]:
    roles = load(book_dir)
    if role:
        roles[source_ref] = role
    else:
        roles.pop(source_ref, None)   # empty role means "undo the correction"
    save(book_dir, roles)
    return roles
