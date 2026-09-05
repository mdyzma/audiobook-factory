"""The contract every stage agrees on.

The chunk manifest is a JSONL file at ``data/book/<slug>/chunks.jsonl``.
`bookbinder` writes it, `narrator` reads it to synthesise, and `bookbinder`
reads it again with the rendered wavs to mux the audiobook. Nothing else
crosses the environment boundary, so the two Python stacks never have to
agree on a library version.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

# XTTS-v2 silently truncates text past these per-language limits.
# Source: Coqui TTS xtts.py char_limits.
XTTS_CHAR_LIMITS: dict[str, int] = {
    "en": 250, "es": 239, "fr": 273, "de": 253, "it": 213, "pt": 203,
    "pl": 224, "tr": 226, "ru": 182, "nl": 251, "cs": 186, "ar": 166,
    "zh-cn": 82, "ja": 71, "hu": 224, "ko": 95, "hi": 150,
}

# Rough narration speed, characters per second. Used only for progress estimates.
CHARS_PER_SECOND = 15.0


def char_limit(language: str) -> int:
    return XTTS_CHAR_LIMITS.get(language, 250)


@dataclass
class Chunk:
    """One synthesis unit: the smallest piece handed to the TTS model."""

    id: str                       # "ch003_0042" - stable, sortable, filename-safe
    chapter_index: int
    chapter_title: str
    order: int                    # position within the whole book
    text: str                     # normalised, ready for the model
    kind: str = "paragraph"       # paragraph | heading | break
    language: str = "pl"
    pause_after_ms: int = 350
    chars: int = 0
    est_seconds: float = 0.0
    source_ref: str = ""          # e.g. epub item id + paragraph index, for debugging
    audio_path: str | None = None  # filled in by narrator
    duration_sec: float | None = None

    def __post_init__(self) -> None:
        self.chars = len(self.text)
        if not self.est_seconds:
            self.est_seconds = round(self.chars / CHARS_PER_SECOND, 2)


@dataclass
class BookManifest:
    """Book-level metadata plus its chunks."""

    slug: str
    title: str
    author: str = "Unknown"
    language: str = "pl"
    source_file: str = ""
    voice: str = ""
    chapters: list[dict] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def est_hours(self) -> float:
        return round(sum(c.est_seconds for c in self.chunks) / 3600, 2)

    def write(self, out_dir: Path) -> tuple[Path, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        chunks_path = out_dir / "chunks.jsonl"
        with chunks_path.open("w", encoding="utf-8") as fh:
            for chunk in self.chunks:
                fh.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")

        meta_path = out_dir / "book.json"
        meta = {
            "slug": self.slug,
            "title": self.title,
            "author": self.author,
            "language": self.language,
            "source_file": self.source_file,
            "voice": self.voice,
            "chapters": self.chapters,
            "chunk_count": len(self.chunks),
            "est_hours": self.est_hours,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta_path, chunks_path


def read_chunks(path: Path) -> Iterator[Chunk]:
    """Stream chunks back in. Used by narrator and by the assembler."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield Chunk(**json.loads(line))


def read_book(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
