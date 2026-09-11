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


def contained(root: Path, path: Path) -> Path:
    """Resolve `path` and confirm it is still inside the project.

    `check_name` stops traversal through a URL, but not a symlink already on
    disk: `data/out/solaris.m4b` pointing at /etc/passwd is a perfectly legal
    name that resolves somewhere else entirely, and serving it follows the
    link. Both sides are resolved before comparing, because the project root
    itself is often reached through a symlink, and the comparison uses
    `is_relative_to` rather than a string prefix, which would also accept a
    sibling directory whose name merely starts with the root's.
    """
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise UnsafeName(f"path resolves outside the project: {path}")
    return resolved


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
    rendered_fingerprint: str = ""

    @property
    def encoding(self) -> str:
        """How the source was read. Blank for books imported before this existed."""
        return self.meta.encoding.encoding if self.meta else ""

    @property
    def language_method(self) -> str:
        """Whether the language was detected, taken from metadata, or given."""
        return self.meta.language_decision.method if self.meta else ""

    @property
    def qa_is_stale(self) -> bool:
        """Whether the quality report describes audio that has since changed.

        A report outlives the render it was made from. Showing an old one as
        current is how a book gets published on the strength of a check that
        ran against different audio.
        """
        if not self.qa or not self.qa.audio_fingerprint:
            return False
        return self.qa.audio_fingerprint != self.rendered_fingerprint

    @property
    def qa_coverage(self) -> str:
        """`full`, `sampled`, or blank when there is no report."""
        if not self.qa:
            return ""
        if not self.qa.coverage.available:
            return "unknown"
        return "full" if self.qa.coverage.full else "sampled"

    @property
    def needs_review(self) -> bool:
        return bool(self.meta and self.meta.needs_review)

    @property
    def review_reasons(self) -> list[str]:
        return list(self.meta.review_reasons) if self.meta else []

    @property
    def state(self) -> str:
        """One word for the dashboard: what is happening to this book."""
        if self.progress and self.progress.running:
            return "stale" if self._stale else "rendering"
        # Checked before `done`, because a dry run produces outputs that look
        # finished in every way except that they are silent.
        if self.dry_run_audio:
            return "silence"
        # Also checked before `done`. A failed render leaves any earlier export
        # sitting in data/out/, so the presence of a file says nothing about
        # this book: the report is the only record of whether the audio behind
        # it is complete.
        if self.report and not self.report.ok:
            return "failed"
        if self.outputs:
            return "done"
        if self.report:
            return "rendered"
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
        # Tied to `state` so a stale export left by a failed render cannot
        # report a book as finished.
        return 100.0 if self.state == "done" else 0.0


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


def _inside(root: Path, path: Path) -> bool:
    try:
        contained(root, path)
    except UnsafeName:
        return False
    return True


def rendered_fingerprint(audio_dir: Path) -> str:
    """Digest of the audio currently on disk for this book.

    Mirrors what the transcriber records in its report, so the two can be
    compared. Reading the render manifest is enough: it names every fragment
    and what each was made from.
    """
    import hashlib

    path = audio_dir / "rendered.jsonl"
    if not path.exists():
        return ""
    pairs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("audio_path"):
            pairs.append((row["id"], row.get("fingerprint") or ""))
    if not pairs:
        return ""
    return hashlib.sha256(json.dumps(sorted(pairs)).encode("utf-8")).hexdigest()[:32]


def find_outputs(root: Path, slug: str) -> list[str]:
    out_dir = root / "data" / "out"
    if not out_dir.is_dir():
        return []
    return sorted(
        p.name for p in out_dir.iterdir()
        if p.is_file() and p.stem == slug and p.suffix in AUDIO_SUFFIXES
        and _inside(root, p)
    )


def imported_meta(book_dir: Path) -> "BookMeta | None":
    """What import recorded, for a book that has not been split into fragments.

    `book.json` is written by chunking, so between importing and chunking a
    book has a title, an encoding and a language decision on disk that nothing
    was reading. It showed on the dashboard as a bare slug with no encoding and
    no language, which is precisely the moment those facts matter most: it is
    when someone decides whether to commit the machine to narrating it.
    """
    path = book_dir / "chapters.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    meta = raw.get("meta")
    if not isinstance(meta, dict):
        return None
    try:
        # The chapters themselves are not carried: this stands in for a book
        # that has no fragments yet, and claiming chapter counts from a
        # different file would be inventing a shape that is not there.
        metadata = dict(meta) | {"chapters": []}
        if not metadata.get("language") and metadata.get("needs_review"):
            metadata["language"] = "und"
        return BookMeta.model_validate(metadata)
    except Exception:
        # Same reasoning as `_load`: a half-written file greys out one card
        # rather than taking the dashboard down.
        return None


def artifact_root(root: Path, slug: str) -> Path:
    """Selected narration of the current text, with legacy files as fallback."""
    from studio.database import database_path
    if database_path(root).is_file():
        from studio.catalog import Catalog
        rows = Catalog(root).rows(
            "SELECT r.root_key FROM audiobook_runs r JOIN books b ON b.id=r.book_id "
            "WHERE b.slug=? AND r.text_version_id=b.current_text_id ORDER BY r.created_at DESC LIMIT 1", (slug,))
        if rows:
            candidate = contained(root, root / rows[0]["root_key"])
            if (candidate / "data/book" / slug / "book.json").is_file():
                return candidate
    return root


def catalog_meta(root: Path, slug: str, selected_root: Path) -> BookMeta | None:
    from studio.database import database_path
    if not database_path(root).is_file():
        return None
    from studio.catalog import Catalog
    catalog = Catalog(root)
    books = catalog.rows("SELECT * FROM books WHERE slug=?", (slug,))
    if not books:
        return None
    book = books[0]
    plan_id = book["current_plan_id"]
    if selected_root != root:
        runs = catalog.rows("SELECT plan_id FROM audiobook_runs WHERE root_key=?", (selected_root.relative_to(root).as_posix(),))
        if runs:
            plan_id = runs[0]["plan_id"]
    if plan_id:
        return BookMeta.model_validate_json(catalog.one("SELECT meta_json FROM chunk_plans WHERE id=?", (plan_id,))["meta_json"])
    raw = json.loads(catalog.one("SELECT snapshot_json FROM text_versions WHERE id=?", (book["current_text_id"],))["snapshot_json"])["meta"]
    raw = dict(raw) | {"chapters": [], "chunk_count": 0}
    if not raw.get("language") and raw.get("needs_review"):
        raw["language"] = "und"
    return BookMeta.model_validate(raw)


def get_book(root: Path, slug: str, *, include_qa: bool = True) -> BookView | None:
    check_name(slug)
    selected = artifact_root(root, slug)
    saved_meta = catalog_meta(root, slug, selected)
    root = selected
    book_dir = root / "data" / "book" / slug
    if not book_dir.is_dir() and saved_meta is None:
        return None

    meta = saved_meta or (None if (book_dir / ".needs-chunking").exists() else _load(book_dir / "book.json", BookMeta)) or imported_meta(book_dir)
    audio_dir = root / "data" / "audio" / slug
    needs_chunking = (book_dir / ".needs-chunking").exists()
    if needs_chunking:
        audio_dir = book_dir / ".no-current-audio"
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
        qa=_load(audio_dir / "qa_report.json", QaReport) if include_qa else None,
        outputs=[] if needs_chunking else find_outputs(root, slug),
        dry_run_audio=is_dry_run_audio(audio_dir),
        rendered_fingerprint=rendered_fingerprint(audio_dir),
    )
    return view


def list_books(root: Path) -> list[BookView]:
    from studio.database import database_path
    if database_path(root).is_file():
        from studio.catalog import Catalog
        catalogued = Catalog(root).books()
        if catalogued:
            return [view for row in catalogued if (view := get_book(root, row["slug"], include_qa=False)) is not None]
    books_root = root / "data" / "book"
    if not books_root.is_dir():
        return []
    views = []
    for d in sorted(books_root.iterdir()):
        if d.is_dir() and SAFE_NAME.match(d.name):
            view = get_book(root, d.name, include_qa=False)
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
    root = artifact_root(root, slug)
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
    root = artifact_root(root, slug)
    path = root / "data" / "audio" / slug / f"{chunk_id}.wav"
    if not path.is_file() or not _inside(root, path):
        return None
    return path


def output_file(root: Path, slug: str, filename: str) -> Path | None:
    check_name(slug)
    root = artifact_root(root, slug)
    if Path(filename).name != filename or Path(filename).suffix not in AUDIO_SUFFIXES:
        raise UnsafeName(f"unsafe output name: {filename!r}")
    path = root / "data" / "out" / filename
    if not path.is_file() or path.stem != slug or not _inside(root, path):
        return None
    return path
