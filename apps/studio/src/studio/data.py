"""Read the pipeline's output. Nothing here writes.

Every stage already communicates through files under `data/`, so the dashboard
needs no API into the other environments: it reads the same things `just
progress` and `just report` read, using bookbinder's models rather than
mirroring them.
"""

from __future__ import annotations

from studio.paths import project_root as _project_root

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bookbinder.manifest import (
    BookMeta,
    Chunk,
    QaReport,
    RenderProgress,
    RenderReport,
    is_dry_run_audio,
    read_chunks,
)

# Slugs and voice names come in through URLs and are used to build paths.
# Anything outside this set is rejected rather than sanitised, so there is no
# clever escaping to get wrong.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# A render that has written nothing for this long is presumed dead; `running`
# stays true forever if the process was killed.
STALE_AFTER_SEC = 120

AUDIO_SUFFIXES = (".m4b", ".mp3", ".wav")


class UnsafeName(ValueError):
    """A slug or voice name that will not be turned into a path."""


def check_name(name: str) -> str:
    if not SAFE_NAME.match(name or ""):
        raise UnsafeName(f"unsafe name: {name!r}")
    return name


def project_root() -> Path:
    return _project_root()


def _load(path: Path, model):
    if not path.exists():
        return None
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        # A half-written or hand-edited file should grey out one card, not take
        # down the dashboard.
        return None


def age_seconds(timestamp: str) -> float | None:
    try:
        then = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


@dataclass
class VoiceView:
    name: str
    language: str = "pl"
    mode: str = "instant"
    segment_count: int = 0
    total_minutes: float = 0.0
    reference_count: int = 0
    has_audition: bool = False
    has_latents: bool = False
    has_dataset: bool = False


@dataclass
class BookView:
    slug: str
    title: str = ""
    author: str = ""
    language: str = "pl"
    chapter_count: int = 0
    chunk_count: int = 0
    est_hours: float = 0.0
    cast: dict[str, str] = field(default_factory=dict)
    meta: BookMeta | None = None
    report: RenderReport | None = None
    progress: RenderProgress | None = None
    qa: QaReport | None = None
    outputs: list[str] = field(default_factory=list)
    dry_run_audio: bool = False

    @property
    def state(self) -> str:
        """One word for the dashboard: what is happening to this book."""
        if self.progress and self.progress.running:
            return "stale" if self._stale else "rendering"
        # Checked before `done`, because a dry run produces outputs that look
        # finished in every way except that they are silent.
        if self.dry_run_audio:
            return "silence"
        if self.outputs:
            return "done"
        if self.report:
            return "failed" if not self.report.ok else "rendered"
        return "prepared"

    @property
    def _stale(self) -> bool:
        if not self.progress or not self.progress.updated_at:
            return False
        age = age_seconds(self.progress.updated_at)
        return age is not None and age > STALE_AFTER_SEC

    @property
    def percent(self) -> float:
        if self.progress:
            return self.progress.percent
        return 100.0 if self.outputs else 0.0


def voice_dir(root: Path, name: str) -> Path:
    return root / "data" / "voices" / check_name(name)


def list_voices(root: Path) -> list[VoiceView]:
    voices_root = root / "data" / "voices"
    if not voices_root.is_dir():
        return []

    out: list[VoiceView] = []
    for profile_path in sorted(voices_root.glob("*.json")):
        name = profile_path.stem
        if not SAFE_NAME.match(name):
            continue
        try:
            raw = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cache = voices_root / name
        out.append(VoiceView(
            name=name,
            language=raw.get("language", "pl"),
            mode=raw.get("mode", "instant"),
            segment_count=int(raw.get("segment_count") or 0),
            total_minutes=float(raw.get("total_minutes") or 0.0),
            reference_count=len(raw.get("reference_wavs") or []),
            has_audition=(cache / "audition.wav").exists(),
            has_latents=(cache / "latents.pt").exists(),
            has_dataset=(root / "data" / "datasets" / name).is_dir(),
        ))
    return out


def get_voice(root: Path, name: str) -> VoiceView | None:
    check_name(name)
    return next((v for v in list_voices(root) if v.name == name), None)


def find_outputs(root: Path, slug: str) -> list[str]:
    out_dir = root / "data" / "out"
    if not out_dir.is_dir():
        return []
    return sorted(
        p.name for p in out_dir.iterdir()
        if p.is_file() and p.stem == slug and p.suffix in AUDIO_SUFFIXES
    )


def get_book(root: Path, slug: str) -> BookView | None:
    check_name(slug)
    book_dir = root / "data" / "book" / slug
    if not book_dir.is_dir():
        return None

    meta = _load(book_dir / "book.json", BookMeta)
    audio_dir = root / "data" / "audio" / slug
    view = BookView(
        slug=slug,
        title=meta.title if meta else slug,
        author=meta.author if meta else "",
        language=meta.language if meta else "pl",
        chapter_count=len(meta.chapters) if meta else 0,
        chunk_count=meta.chunk_count if meta else 0,
        est_hours=meta.est_hours if meta else 0.0,
        cast=dict(meta.cast) if meta else {},
        meta=meta,
        report=_load(audio_dir / "report.json", RenderReport),
        progress=_load(audio_dir / "progress.json", RenderProgress),
        qa=_load(audio_dir / "qa_report.json", QaReport),
        outputs=find_outputs(root, slug),
        dry_run_audio=is_dry_run_audio(audio_dir),
    )
    return view


def list_books(root: Path) -> list[BookView]:
    books_root = root / "data" / "book"
    if not books_root.is_dir():
        return []
    views = []
    for d in sorted(books_root.iterdir()):
        if d.is_dir() and SAFE_NAME.match(d.name):
            view = get_book(root, d.name)
            if view:
                views.append(view)
    return views


def load_chunks(root: Path, slug: str, limit: int = 0) -> list[Chunk]:
    """Fragments, preferring the rendered manifest.

    `chunks.jsonl` is what the chunker wrote and carries no audio paths, so
    reading it alone means the page can never offer per-fragment playback.
    `rendered.jsonl` is the same schema with `audio_path` and `duration_sec`
    filled in, so use it once a render has produced one.
    """
    check_name(slug)
    rendered = root / "data" / "audio" / slug / "rendered.jsonl"
    path = rendered if rendered.exists() else root / "data" / "book" / slug / "chunks.jsonl"
    if not path.exists():
        return []
    chunks = []
    for chunk in read_chunks(path):
        chunks.append(chunk)
        if limit and len(chunks) >= limit:
            break
    return chunks


def rendered_audio(root: Path, slug: str, chunk_id: str) -> Path | None:
    """The wav for one fragment, if it has been rendered."""
    check_name(slug)
    check_name(chunk_id)
    path = root / "data" / "audio" / slug / f"{chunk_id}.wav"
    return path if path.is_file() else None


def output_file(root: Path, slug: str, filename: str) -> Path | None:
    check_name(slug)
    if Path(filename).name != filename or Path(filename).suffix not in AUDIO_SUFFIXES:
        raise UnsafeName(f"unsafe output name: {filename!r}")
    path = root / "data" / "out" / filename
    return path if path.is_file() and path.stem == slug else None