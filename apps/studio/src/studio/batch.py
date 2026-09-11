"""What a batch looks like before it is committed to a day of work.

Queueing twenty books is a decision made once and paid for over many hours, so
the point of this module is to put everything that decision depends on in front
of a person first: how each file was read, what language it was taken to be and
whether that was detected or told, which model and which voices will read it,
how long it will take, and what is actually left to do.

Readiness is the column that matters. A book with an unresolved encoding or an
undecided language is not stopped here because it is broken, but because
choosing for it silently is how a batch produces nineteen audiobooks and one
that nobody notices is wrong until they listen to it.

Defaults apply to the whole batch and overrides apply to one book, in that
order. A folder that is all Polish read by one voice should take two choices,
not forty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from studio.data import BookView, list_books
from studio.queue import OPEN, Queue

# What queueing a book can be asked to do, in the order the pipeline runs them.
# Verification is offered but never assumed: it is a second pass over finished
# audio, and on a batch it doubles the time on the device.
STEPS = ("chunk", "synth", "assemble", "verify")
DEFAULT_STEPS = ("chunk", "synth", "assemble")


@dataclass
class Row:
    """One book, as a person needs to see it before saying yes."""

    slug: str
    title: str = ""
    author: str = ""
    source: str = ""
    encoding: str = ""
    language: str = ""
    language_method: str = ""
    model: str = ""
    voice: str = ""
    cast: dict[str, str] = field(default_factory=dict)
    est_hours: float = 0.0
    chunk_count: int = 0
    state: str = ""
    outstanding: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    queued: str = ""

    @property
    def ready(self) -> bool:
        """Whether this book can be queued without anyone deciding anything."""
        return not self.blocking and bool(self.outstanding)

    # True when nobody was named for this book and the cast in config/cast.yml
    # is what would read it. Worth saying out loud on the review screen.
    cast_is_default: bool = False

    @property
    def reader(self) -> str:
        """Who reads it: one voice, or the cast that divides the roles."""
        if self.voice:
            return self.voice
        distinct = sorted(set(v for v in self.cast.values() if v))
        if not distinct:
            return ""
        return distinct[0] if len(distinct) == 1 else ", ".join(distinct)

    @property
    def nothing_to_do(self) -> bool:
        return not self.outstanding


def outstanding_steps(book: BookView) -> list[str]:
    """What this book still needs, in pipeline order.

    Dry-run silence counts as not narrated. It is byte-for-byte a finished
    render in every way except that it makes no sound, which is exactly why it
    has to be named here rather than inferred from the presence of files.
    """
    steps: list[str] = []
    if not book.chunk_count:
        steps.append("chunk")

    narrated = bool(book.report and book.report.ok) and not book.dry_run_audio
    if not narrated:
        steps.append("synth")
    if "synth" in steps or not book.outputs:
        steps.append("assemble")
    return steps


def default_cast(root: Path) -> dict[str, str]:
    """The cast in `config/cast.yml`, which chunking gives a book with none.

    Read here because a freshly imported book carries no cast of its own: that
    is filled in when it is split, from this file. Treating such a book as
    having nobody to read it held back every book in a batch that was in fact
    perfectly ready.
    """
    from studio.authoring import read_cast

    roles = read_cast(root)
    return {role: str(spec.get("voice") or "") for role, spec in roles.items()}


def blocking_problems(book: BookView, fallback_cast: "dict[str, str] | None" = None
                      ) -> list[str]:
    """Why this book cannot simply be queued.

    Every one of these is a question only a person can answer. None is a
    failure; they are the decisions a batch would otherwise make silently.
    """
    problems: list[str] = []
    if book.needs_review:
        problems.extend(book.review_reasons or ["the import was left for review"])
    if not book.language:
        problems.append("no language was settled; choose one before queueing")
    if not readers(book, fallback_cast):
        problems.append("no voice or cast is set, so nobody would read it")
    return problems


def readers(book: BookView, fallback_cast: "dict[str, str] | None" = None
            ) -> list[str]:
    """Who would read this book: its own voice, its own cast, or the default."""
    if book.meta and book.meta.voice:
        return [book.meta.voice]
    own = [v for v in (book.cast or {}).values() if v]
    if own:
        return sorted(set(own))
    return sorted({v for v in (fallback_cast or {}).values() if v})


def row_for(book: BookView, queued: str = "",
            fallback_cast: "dict[str, str] | None" = None) -> Row:
    meta = book.meta
    own_cast = {k: v for k, v in (book.cast or {}).items() if v}
    borrowed = not (meta and meta.voice) and not own_cast and bool(fallback_cast)
    return Row(
        slug=book.slug,
        title=book.title,
        author=book.author,
        source=Path(meta.source_file).name if meta and meta.source_file else "",
        encoding=book.encoding,
        language=book.language,
        language_method=book.language_method,
        model=(meta.model.id if meta and meta.model else ""),
        voice=(meta.voice if meta else ""),
        cast=own_cast or (dict(fallback_cast or {}) if borrowed else {}),
        cast_is_default=borrowed,
        est_hours=book.est_hours,
        chunk_count=book.chunk_count,
        state=book.state,
        outstanding=outstanding_steps(book),
        blocking=blocking_problems(book, fallback_cast),
        queued=queued,
    )


def queued_state(queue: Queue) -> dict[str, str]:
    """The queue's opinion of each book, for the readiness column.

    A book with steps still in the queue must not be queued twice: the second
    copy would render the same fragments into the same directory as the first.
    """
    state: dict[str, str] = {}
    for item in queue.items():
        if item.status in OPEN:
            state[item.slug] = item.status
    return state


def review(root: Path) -> list[Row]:
    """Every book here, with what queueing it would mean."""
    queued = queued_state(Queue(root))
    fallback = default_cast(root)
    return [row_for(book, queued.get(book.slug, ""), fallback)
            for book in list_books(root)]


def chosen_steps(requested: "list[str] | tuple[str, ...]") -> list[str]:
    """Keep only real steps, in pipeline order, however they were asked for."""
    wanted = {s for s in requested if s in STEPS}
    return [s for s in STEPS if s in wanted]


def steps_for(row: Row, requested: "list[str] | tuple[str, ...]" = ()) -> list[str]:
    """The steps to queue for one book.

    Asking for synthesis on a book that has never been split silently produces
    nothing, so anything the book still needs earlier in the pipeline comes
    along with what was asked for.
    """
    asked = chosen_steps(requested) or list(DEFAULT_STEPS)
    needed = set(row.outstanding)
    last = max((STEPS.index(s) for s in asked), default=-1)
    return [s for s in STEPS
            if (s in asked or s in needed) and STEPS.index(s) <= last]


@dataclass
class Queued:
    """What one attempt to queue a batch actually did."""

    queued: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def total_steps(self) -> int:
        return len(self.queued)


def queue_books(
    root: Path,
    slugs: "list[str]",
    steps: "list[str] | tuple[str, ...]" = (),
    overrides: "dict[str, dict[str, str]] | None" = None,
    voice: str = "",
    fmt: str = "",
    batch: str = "",
) -> Queued:
    """Put the chosen books in the queue, and say what was left out and why.

    `voice` and `fmt` are the batch defaults; `overrides` carries the per-book
    answers that beat them. A book that is not ready is skipped with its reason
    rather than queued in the hope that it works out.
    """
    rows = {row.slug: row for row in review(root)}
    queue = Queue(root)
    result = Queued()

    for slug in slugs:
        row = rows.get(slug)
        if row is None:
            result.skipped[slug] = "no book of that name is here"
            continue
        if row.queued:
            result.skipped[slug] = f"already in the queue ({row.queued})"
            continue
        if row.nothing_to_do:
            result.skipped[slug] = "nothing left to do"
            continue
        if row.blocking:
            result.skipped[slug] = row.blocking[0]
            continue

        mine = (overrides or {}).get(slug, {})
        plan: list[tuple[str, dict[str, str]]] = []
        for step in steps_for(row, steps):
            args: dict[str, str] = {}
            if step == "synth":
                args["voice"] = mine.get("voice", voice)
            if step == "assemble":
                args["format"] = mine.get("format", fmt)
            plan.append((step, args))

        if plan:
            from studio.runs import prepare_run
            from studio.database import StorageError
            try:
                run = prepare_run(root, slug, voice=mine.get("voice", voice),
                                  model=mine.get("model", ""), reuse=True)
                queue.add_plan(slug, plan, batch=batch, run_id=run["id"])
            except (StorageError, OSError) as exc:
                result.skipped[slug] = str(exc)
                continue
            result.queued.extend(f"{slug}/{step}" for step, _args in plan)
    return result
