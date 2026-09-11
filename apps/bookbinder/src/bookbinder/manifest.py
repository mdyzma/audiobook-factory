"""The contract every stage agrees on.

The chunk manifest is a JSONL file at ``data/book/<slug>/chunks.jsonl``.
`bookbinder` writes it, `narrator` reads it to synthesise, and `bookbinder`
reads it again with the rendered wavs to mux the audiobook. Nothing else
crosses the environment boundary, so the two Python stacks never have to
agree on a library version.

The models are validated rather than plain dataclasses because a malformed
chunk should fail at write time, not thousands of fragments into a render.
`just schemas` exports them to docs/schemas/ so the shape is reviewable and
CI can catch a change nobody meant to make.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

# 2 added the decoding and language provenance to book.json. 3 added the
# extracted spelling and the substitutions behind each chunk's spoken text.
# 4 added the resolved synthesis backend, which is what makes book.json the
# request a narrator environment reads rather than a description of one.
# 5 added each fragment's fingerprint, so reusing audio stops being a guess
# based on a filename. 6 made the quality report say which languages it
# checked, how much of the book, and which audio it describes, and gave
# render failures a category and an attempt count, and carried
# per-role controls through to the renderer.
# The set is versioned as a unit so a reader only has to check one number.
# narrator and transcriber mirror this constant.
SCHEMA_VERSION = 7

# XTTS-v2 silently truncates text past these per-language limits.
# Source: Coqui TTS xtts.py char_limits.
XTTS_CHAR_LIMITS: dict[str, int] = {
    "en": 250, "es": 239, "fr": 273, "de": 253, "it": 213, "pt": 203,
    "pl": 224, "tr": 226, "ru": 182, "nl": 251, "cs": 186, "ar": 166,
    "zh-cn": 82, "ja": 71, "hu": 224, "ko": 95, "hi": 150,
}

# Rough narration speed, characters per second. Used for progress estimates and
# for the duration of dry-run silence.
CHARS_PER_SECOND = 15.0

ChunkKind = Literal["paragraph", "heading", "break"]

NARRATOR_ROLE = "narrator"

# A dry run fills data/audio/<slug>/ with silence that is byte-for-byte a
# plausible render: right sample rate, right duration, right filename. Stage 4
# resumes by skipping fragments that already have a wav, so without a marker on
# disk a dry run followed by a real render silently produces hours of nothing,
# and every downstream stage believes it. The marker is what tells them apart.
#
# It has to survive stage 4 rewriting report.json and progress.json, so it is
# its own file rather than a flag inside either of them. narrator mirrors the
# name; it cannot import this module.
DRY_RUN_MARKER = ".dry-run.json"

# What each rendered wav was made from, one JSON line per fragment. narrator
# appends to it as each fragment lands and mirrors the name; assembly reads it
# to tell current audio from audio left over from an earlier model or text.
FINGERPRINT_LEDGER = "fingerprints.jsonl"


def char_limit(language: str) -> int:
    return XTTS_CHAR_LIMITS.get(language, 250)


def publish(path: Path, write: "Callable[[Path], object]") -> Path:
    """Produce `path` by writing beside it and renaming into place.

    Every file here is read by something that decides what to do next: a
    manifest says what to render, a report says whether a render worked, an
    export is the deliverable. Written directly, a run killed part-way leaves a
    half-file that parses as a smaller book or reads as a finished one.

    A rename within a directory is atomic, so a reader sees either the previous
    file or the complete new one. `write` is handed the temporary path and must
    fill it; if it raises, nothing is replaced. Its return value is ignored,
    so `Path.write_text` can be passed straight through.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.part")
    try:
        write(staged)
        staged.replace(path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return path


def publish_text(path: Path, content: str) -> Path:
    """`publish` for a file that is one string."""
    return publish(path, lambda p: p.write_text(content, encoding="utf-8"))


def mark_dry_run(audio_dir: Path, slug: str, chunks: int = 0) -> Path:
    """Record that the wavs in `audio_dir` are silence, not narration."""
    path = audio_dir / DRY_RUN_MARKER
    path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "slug": slug, "chunks": chunks}, indent=2),
        encoding="utf-8",
    )
    return path


def is_dry_run_audio(audio_dir: Path) -> bool:
    return (audio_dir / DRY_RUN_MARKER).exists()


def clear_dry_run(audio_dir: Path) -> int:
    """Delete dry-run silence and its marker. Returns the number of wavs removed.

    Called by stage 4 before rendering for real: once there is narration to
    make, silence at the same paths is worse than nothing, because resume
    would keep it.
    """
    marker = audio_dir / DRY_RUN_MARKER
    if not marker.exists():
        return 0
    removed = 0
    for wav in audio_dir.glob("*.wav"):
        wav.unlink()
        removed += 1
    (audio_dir / "rendered.jsonl").unlink(missing_ok=True)
    # The ledger describes the silence being deleted, so it goes with it.
    # Leaving it would let a later render believe fragments it cannot see.
    (audio_dir / FINGERPRINT_LEDGER).unlink(missing_ok=True)
    marker.unlink()
    return removed


class StrictModel(BaseModel):
    """Reject unknown fields, so a typo in a hand-edited manifest is an error."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ReportModel(BaseModel):
    """Base for files written by the other two environments.

    Reports are emitted by narrator and transcriber, which cannot import these
    models and so mirror the shape by hand, including the computed summary
    fields. Ignoring extra keys rather than rejecting them means a report stays
    readable here even when the writer includes a derived value. Chunks stay
    strict, because bookbinder authors those itself and a typo there is a bug.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class SpokenSubstitution(StrictModel):
    """One place where the spoken text differs from the printed text.

    Both spans are kept so a fragment can be read in either direction: quality
    checking hears "doktor" and has to know it came from "dr." rather than
    report a mismatch.
    """

    kind: str = Field(default="", description="abbreviation | symbol | dictionary")
    source: str = ""
    spoken: str = ""
    start: int = Field(default=0, ge=0, description="Offset into `source_text`")
    end: int = Field(default=0, ge=0)
    spoken_start: int = Field(default=0, ge=0, description="Offset into `text`")
    spoken_end: int = Field(default=0, ge=0)


class Chunk(StrictModel):
    """One synthesis unit: the smallest piece handed to the TTS model."""

    id: str = Field(min_length=1, description="Stable, sortable, filename-safe")
    chapter_index: int = Field(ge=0)
    chapter_title: str = ""
    order: int = Field(ge=0, description="Position within the whole book")
    text: str = Field(min_length=1, description="Normalised, ready for the model")
    source_text: str = Field(
        default="",
        description="The printed spelling, stored only where it differs from `text`",
    )
    substitutions: list[SpokenSubstitution] = Field(default_factory=list)
    kind: ChunkKind = "paragraph"
    language: str = Field(default="pl", min_length=2)
    role: str = Field(
        default=NARRATOR_ROLE,
        min_length=1,
        description="Cast role; resolved to a voice through config/cast.yml",
    )
    is_dialogue: bool = False
    pause_after_ms: int = Field(default=350, ge=0)
    chars: int = Field(default=0, ge=0)
    est_seconds: float = Field(default=0.0, ge=0)
    source_ref: str = ""
    # Filled in by the narrator (or the dry-run renderer).
    audio_path: str | None = None
    duration_sec: float | None = Field(default=None, ge=0)
    voice: str = Field(
        default="",
        description="The voice that actually rendered this fragment, which a "
                    "--voice override makes different from the cast",
    )
    fingerprint: str = Field(
        default="",
        description="What the audio was rendered from; blank means it predates fingerprinting",
    )

    @model_validator(mode="after")
    def _derive(self) -> "Chunk":
        # Assigning inside a validator would recurse under validate_assignment.
        object.__setattr__(self, "chars", len(self.text))
        if not self.est_seconds:
            object.__setattr__(self, "est_seconds", round(self.chars / CHARS_PER_SECOND, 2))
        return self

    @property
    def exceeds_model_limit(self) -> bool:
        return self.chars > char_limit(self.language)


class ChapterRef(StrictModel):
    index: int = Field(ge=0)
    title: str
    first_chunk: int = Field(ge=0)
    chunk_count: int = Field(ge=0)


class EncodingRecord(StrictModel):
    """How the source bytes became text.

    Kept because the answer is a judgement, not a fact: a legacy Polish file
    decodes without error under several tables and only one of them is right.
    Recording the reasoning is what lets a reader disagree with it later.
    """

    encoding: str = ""
    method: str = Field(default="", description="override | bom | utf-8 | quality | ascii")
    score: float = 0.0
    equivalent: list[str] = Field(
        default_factory=list,
        description="Encodings that produced identical text, so nothing distinguishes them",
    )
    decoder_version: int = Field(
        default=1, description="Bumped when the decoding rules change, not the schema"
    )
    warnings: list[str] = Field(default_factory=list)


class LanguageSample(StrictModel):
    """One passage the detector was shown, and what it made of it."""

    where: str = ""
    chars: int = Field(default=0, ge=0)
    language: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)


class LanguageRecord(StrictModel):
    """How the book's language was chosen.

    `BookMeta.language` is the answer; this is the working. A detector score is
    evidence rather than a guarantee, so it is stored as what it is.
    """

    method: str = Field(default="", description="override | content | metadata | unresolved")
    confidence: float = Field(default=0.0, ge=0, le=1)
    detector: str = ""
    metadata_language: str = Field(
        default="", description="What the container claimed, believed or not"
    )
    coverage_chars: int = Field(default=0, ge=0)
    samples: list[LanguageSample] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ModelChoice(StrictModel):
    """The synthesis backend that will narrate this book.

    Resolved once, when the book is chunked, and read from here afterwards.
    That is deliberate: changing tomorrow's default in `config/models.toml`
    must not change a book that is already queued or half-rendered, and the
    chunk sizes were computed against this model's limit rather than another's.

    It is also the request a narrator environment reads. `environment` says
    which one can load it, so a book bound to a backend the running
    environment does not implement is refused rather than silently rendered
    with whatever is at hand.
    """

    id: str = Field(default="", description="Registry key, e.g. xtts-v2")
    engine: str = Field(default="", description="Which backend implements it")
    environment: str = Field(default="", description="The uv project that can load it")
    checkpoint: str = ""
    revision: str = Field(
        default="", description="Pinned weights; blank until validated on the target machine"
    )
    native_sample_rate: int = Field(default=24000, gt=0)
    char_limit: int = Field(
        default=0, ge=0, description="What the fragments below were packed against"
    )
    settings: dict[str, float] = Field(
        default_factory=dict, description="Effective controls this backend implements"
    )
    unsupported: list[str] = Field(
        default_factory=list,
        description="Configured controls this backend ignores, recorded rather than dropped",
    )
    source: str = Field(default="", description="default | override")

    @property
    def identity(self) -> str:
        """What has to match for rendered audio to be reusable."""
        return f"{self.id}@{self.revision}" if self.revision else self.id


class BookMeta(StrictModel):
    """What `book.json` holds. Chunks live beside it in chunks.jsonl."""

    schema_version: int = SCHEMA_VERSION
    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    author: str = "Unknown"
    language: str = Field(default="pl", min_length=2)
    source_file: str = Field(
        default="", description="The staged copy under data/sources/, relative to the root"
    )
    original_source: str = Field(
        default="", description="Where it was imported from, for reference only"
    )
    source_sha256: str = ""
    voice: str = ""
    cast: dict[str, str] = Field(
        default_factory=dict,
        description="role -> voice name, as resolved when the book was chunked",
    )
    cast_settings: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description="role -> controls that differ from the book's, such as a "
                    "slightly faster dialogue voice. Only controls the chosen "
                    "backend implements appear here",
    )
    chapters: list[ChapterRef] = Field(default_factory=list)
    chunk_count: int = Field(default=0, ge=0)
    est_hours: float = Field(default=0.0, ge=0)

    encoding: EncodingRecord = Field(default_factory=EncodingRecord)
    language_decision: LanguageRecord = Field(default_factory=LanguageRecord)
    model: ModelChoice = Field(default_factory=ModelChoice)

    # Hoisted out of the two records above so a folder of books can be shown
    # and filtered without opening each one.
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


class BookManifest(StrictModel):
    """Book-level metadata plus its chunks, as held in memory while chunking."""

    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    author: str = "Unknown"
    language: str = Field(default="pl", min_length=2)
    source_file: str = ""
    original_source: str = ""
    source_sha256: str = ""
    voice: str = ""
    cast: dict[str, str] = Field(default_factory=dict)
    cast_settings: dict[str, dict[str, float]] = Field(default_factory=dict)
    chapters: list[ChapterRef] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)
    encoding: EncodingRecord = Field(default_factory=EncodingRecord)
    language_decision: LanguageRecord = Field(default_factory=LanguageRecord)
    model: ModelChoice = Field(default_factory=ModelChoice)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def est_hours(self) -> float:
        return round(sum(c.est_seconds for c in self.chunks) / 3600, 2)

    @property
    def roles(self) -> set[str]:
        return {c.role for c in self.chunks}

    def to_meta(self) -> BookMeta:
        return BookMeta(
            slug=self.slug, title=self.title, author=self.author,
            language=self.language, source_file=self.source_file,
            original_source=self.original_source,
            source_sha256=self.source_sha256, voice=self.voice, cast=self.cast,
            cast_settings=self.cast_settings,
            chapters=self.chapters, chunk_count=len(self.chunks),
            est_hours=self.est_hours, encoding=self.encoding,
            language_decision=self.language_decision, model=self.model,
            needs_review=self.needs_review, review_reasons=self.review_reasons,
        )

    def write(self, out_dir: Path) -> tuple[Path, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)

        chunks_path = publish_text(
            out_dir / "chunks.jsonl",
            "".join(c.model_dump_json() + "\n" for c in self.chunks),
        )
        meta_path = publish_text(
            out_dir / "book.json", self.to_meta().model_dump_json(indent=2))
        return meta_path, chunks_path


def read_chunks(path: Path) -> Iterator[Chunk]:
    """Stream chunks back in, validating each one."""
    with path.open(encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield Chunk.model_validate_json(line)
            except Exception as exc:  # noqa: BLE001 - re-raised with position
                raise ValueError(f"{path}:{line_number} is not a valid chunk: {exc}") from exc


def read_book(path: Path) -> BookMeta:
    return BookMeta.model_validate_json(path.read_text(encoding="utf-8"))


def json_schemas() -> dict[str, dict]:
    """The exported contract. Written to docs/schemas/ by `just schemas`.

    Serialization mode, not the default validation mode. These schemas describe
    files as they exist on disk, and computed fields such as `percent` and
    `realtime_factor` are written into those files but omitted from a validation
    schema. narrator and transcriber mirror these shapes by hand, so a schema
    that hid half the fields would be worse than useless to them.
    """
    return {
        f"chunk_v{SCHEMA_VERSION}": Chunk.model_json_schema(mode="serialization"),
        f"book_meta_v{SCHEMA_VERSION}": BookMeta.model_json_schema(mode="serialization"),
        f"render_report_v{SCHEMA_VERSION}": RenderReport.model_json_schema(mode="serialization"),
        f"render_progress_v{SCHEMA_VERSION}": RenderProgress.model_json_schema(mode="serialization"),
        f"qa_report_v{SCHEMA_VERSION}": QaReport.model_json_schema(mode="serialization"),
    }


class RenderFailure(ReportModel):
    chunk_id: str = Field(min_length=1)
    error: str
    attempts: int = Field(
        default=1, ge=1, description="How many times it was tried before giving up"
    )
    kind: str = Field(
        default="fragment",
        description="fragment | voice | model | unknown. A voice or model fault is "
                    "the run's, not this fragment's, and repeats for every one",
    )


class RenderReport(ReportModel):
    """Written after every synthesis run, real or dry.

    Failures used to go to a text file that was easy to miss. This is the
    record of what a run actually produced.
    """

    schema_version: int = SCHEMA_VERSION
    slug: str = Field(min_length=1)
    voice: str = ""
    cast: dict[str, str] = Field(default_factory=dict)
    # Which backend actually produced this audio. Without it, a book rendered
    # under one model and resumed under another looks like one clean run.
    model: str = Field(default="", description="Registry id, with revision where pinned")
    engine: str = ""
    sample_rate: int = Field(
        default=0, ge=0, description="The engine's native rate, before any resampling"
    )
    device: str = ""
    dry_run: bool = False
    # What levelling was applied while rendering, and what it cost. A cast
    # whose voices sit several dB apart is the most audible defect a book can
    # have, so the correction that fixed it belongs in the record of the run.
    gains_db: dict[str, float] = Field(
        default_factory=dict,
        description="voice -> level correction applied, from its profile",
    )
    clipped_samples: int = Field(
        default=0, ge=0,
        description="Samples the correction pushed past full scale and had to clamp",
    )
    clipped_voices: list[str] = Field(
        default_factory=list,
        description="Voices whose correction is too large for their loudest passages",
    )
    started_at: str = ""
    finished_at: str = ""
    elapsed_sec: float = Field(default=0.0, ge=0)
    chunks_total: int = Field(default=0, ge=0)
    chunks_rendered: int = Field(default=0, ge=0)
    chunks_skipped: int = Field(default=0, ge=0, description="Already had audio")
    chunks_retried: int = Field(
        default=0, ge=0, description="Succeeded only after a retry, worth a listen"
    )
    audio_sec: float = Field(default=0.0, ge=0)
    failures: list[RenderFailure] = Field(default_factory=list)

    @computed_field
    @property
    def realtime_factor(self) -> float:
        """Seconds of audio produced per second of wall clock."""
        return round(self.audio_sec / self.elapsed_sec, 2) if self.elapsed_sec else 0.0

    @computed_field
    @property
    def ok(self) -> bool:
        return not self.failures and self.chunks_rendered + self.chunks_skipped == self.chunks_total

    def write(self, path: Path) -> Path:
        return publish_text(path, self.model_dump_json(indent=2))


class RenderProgress(ReportModel):
    """Written repeatedly *during* a render, unlike RenderReport.

    A book is hours of work and `report.json` only appears at the end, so
    nothing could see inside a run. This is the live view: a UI polls it, and
    `just progress <slug>` prints it.

    Readers must treat `running` as a claim, not a fact. A killed process leaves
    it true forever, so compare `updated_at` against the clock: a render that
    has not moved in minutes is dead, whatever the file says.
    """

    schema_version: int = SCHEMA_VERSION
    slug: str = Field(min_length=1)
    running: bool = True
    pid: int = Field(default=0, ge=0, description="Writer's process id, to check liveness")
    dry_run: bool = False
    device: str = ""
    started_at: str = ""
    updated_at: str = ""
    elapsed_sec: float = Field(default=0.0, ge=0)
    chunks_total: int = Field(default=0, ge=0)
    chunks_done: int = Field(default=0, ge=0, description="Rendered plus skipped")
    chunks_rendered: int = Field(default=0, ge=0)
    chunks_skipped: int = Field(default=0, ge=0)
    chunks_failed: int = Field(default=0, ge=0)
    audio_sec: float = Field(default=0.0, ge=0)
    current_chunk_id: str = ""
    current_voice: str = ""
    last_error: str = ""

    @computed_field
    @property
    def percent(self) -> float:
        return round(100 * self.chunks_done / self.chunks_total, 1) if self.chunks_total else 0.0

    @computed_field
    @property
    def eta_sec(self) -> float:
        """Seconds remaining, from the rate achieved so far.

        Based on chunks actually rendered, not on chunks done: skipped ones cost
        nothing and would make the estimate wildly optimistic on a resumed run.
        """
        if not self.chunks_rendered or not self.elapsed_sec:
            return 0.0
        remaining = self.chunks_total - self.chunks_done
        return round(remaining * (self.elapsed_sec / self.chunks_rendered), 1) if remaining > 0 else 0.0

    def write(self, path: Path) -> Path:
        """Atomic, because a UI polls this file while it is being rewritten."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path


class QaFinding(ReportModel):
    chunk_id: str = Field(min_length=1)
    expected: str = Field(description="The spoken text the model was given")
    heard: str
    wer: float = Field(ge=0)
    language: str = Field(default="", description="Which ASR model heard it")
    source_text: str = Field(
        default="",
        description="The printed spelling, where it differs, so the passage can "
                    "be found in the book",
    )


class QaCoverage(ReportModel):
    """How much of the book this report actually describes.

    A sampled pass and a full pass are different claims, and one presented as
    the other is how a book gets published on the strength of every twentieth
    fragment.
    """

    checked: int = Field(default=0, ge=0)
    available: int = Field(default=0, ge=0, description="Fragments with audio")
    sample: int = Field(default=0, ge=0, description="Every Nth; 0 means all")
    languages: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def full(self) -> bool:
        return self.available > 0 and self.checked == self.available


class QaReport(ReportModel):
    """Output of re-transcribing rendered audio and comparing it to the source."""

    schema_version: int = SCHEMA_VERSION
    slug: str = Field(min_length=1)
    model: str = ""
    max_wer: float = Field(default=0.15, ge=0)
    thresholds: dict[str, float] = Field(
        default_factory=dict,
        description="Per-language limits, where they differ from max_wer",
    )
    chunks_checked: int = Field(default=0, ge=0)
    mean_wer: float = Field(default=0.0, ge=0)
    coverage: QaCoverage = Field(default_factory=QaCoverage)
    audio_fingerprint: str = Field(
        default="",
        description="Digest of the fragments checked, so a later render shows this "
                    "report as describing audio that no longer exists",
    )
    synthesis_model: str = Field(default="", description="Which backend made the audio")
    findings: list[QaFinding] = Field(default_factory=list)

    @computed_field
    @property
    def ok(self) -> bool:
        return not self.findings

    def write(self, path: Path) -> Path:
        return publish_text(path, self.model_dump_json(indent=2))
