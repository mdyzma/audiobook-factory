"""Queueing twenty books is one decision paid for over many hours.

So the review has to show what the decision rests on, and refuse to make any of
it silently. A book whose encoding was ambiguous or whose language was never
settled is held back, not guessed at: nineteen good audiobooks and one nobody
notices is wrong is the failure this prevents.
"""

from __future__ import annotations

import json

import pytest

from studio.batch import (
    DEFAULT_STEPS,
    STEPS,
    blocking_problems,
    outstanding_steps,
    queue_books,
    review,
    row_for,
    steps_for,
)
from studio.data import get_book
from studio.queue import Queue


def write_book(root, slug, **fields):
    book_dir = root / "data" / "book" / slug
    book_dir.mkdir(parents=True, exist_ok=True)
    (root / "data" / "audio" / slug).mkdir(parents=True, exist_ok=True)
    meta = {
        "schema_version": 6, "slug": slug, "title": slug.title(), "author": "Lem",
        "language": "pl", "source_file": f"data/sources/{slug}/{slug}.txt",
        "source_sha256": "", "voice": "michal",
        "cast": {"narrator": "michal", "dialogue": "michal"},
        "chapters": [{"index": 1, "title": "One", "first_chunk": 0, "chunk_count": 1}],
        "chunk_count": 1, "est_hours": 2.5,
    }
    meta.update(fields)
    (book_dir / "book.json").write_text(json.dumps(meta), encoding="utf-8")
    chunk = {
        "id": "ch001_0000", "chapter_index": 1, "chapter_title": "One", "order": 0,
        "text": "Ocean falował.", "kind": "paragraph", "language": "pl",
        "role": "narrator", "is_dialogue": False, "pause_after_ms": 350,
        "chars": 14, "est_seconds": 0.9, "source_ref": "",
        "audio_path": None, "duration_sec": None,
    }
    (book_dir / "chunks.jsonl").write_text(json.dumps(chunk) + "\n", encoding="utf-8")
    return book_dir


@pytest.fixture
def two_books(project):
    write_book(project, "eden")
    write_book(project, "solaris")
    return project


class TestWhatIsLeftToDo:
    def test_a_book_never_split_needs_everything(self, project):
        write_book(project, "eden", chunk_count=0)
        (project / "data" / "book" / "eden" / "chunks.jsonl").unlink()
        book = get_book(project, "eden")
        assert book is not None
        assert outstanding_steps(book) == ["chunk", "synth", "assemble"]

    def test_a_split_book_still_needs_narrating(self, two_books):
        book = get_book(two_books, "eden")
        assert book is not None
        assert outstanding_steps(book) == ["synth", "assemble"]

    def test_dry_run_silence_does_not_count_as_narrated(self, two_books):
        # It is byte-for-byte a finished render except that it makes no sound,
        # which is exactly why it has to be named rather than inferred.
        audio = two_books / "data" / "audio" / "eden"
        (audio / ".dry-run.json").write_text('{"slug": "eden"}', encoding="utf-8")
        (audio / "report.json").write_text(
            json.dumps({"schema_version": 6, "slug": "eden", "ok": True,
                        "chunks": 1, "rendered": 1, "failed": 0}), encoding="utf-8")
        book = get_book(two_books, "eden")
        assert book is not None
        assert "synth" in outstanding_steps(book)

    def test_asking_for_a_later_step_brings_the_earlier_ones(self, project):
        # Narrating a book that was never split silently produces nothing.
        write_book(project, "eden", chunk_count=0)
        (project / "data" / "book" / "eden" / "chunks.jsonl").unlink()
        book = get_book(project, "eden")
        assert book is not None
        assert steps_for(row_for(book), ["synth"]) == ["chunk", "synth"]

    def test_asking_for_less_does_not_add_what_comes_after(self, two_books):
        book = get_book(two_books, "eden")
        assert book is not None
        assert steps_for(row_for(book), ["synth"]) == ["synth"]

    def test_the_default_is_the_whole_pipeline_bar_verification(self):
        # Verification is a second pass over finished audio and doubles the
        # time on the device, so it is offered rather than assumed.
        assert "verify" in STEPS and "verify" not in DEFAULT_STEPS


class TestNothingIsDecidedSilently:
    def test_a_book_left_for_review_is_held_back(self, project):
        write_book(project, "eden", needs_review=True,
                   review_reasons=["cp1250 and iso-8859-2 are equally plausible"])
        book = get_book(project, "eden")
        assert book is not None
        assert "equally plausible" in " ".join(blocking_problems(book))

    def test_a_book_with_nobody_to_read_it_is_held_back(self, project):
        write_book(project, "eden", voice="", cast={})
        book = get_book(project, "eden")
        assert book is not None
        assert any("nobody would read it" in p for p in blocking_problems(book))

    def test_a_cast_counts_as_a_reader(self, two_books):
        book = get_book(two_books, "eden")
        assert book is not None
        assert blocking_problems(book) == []

    def test_a_held_back_book_is_not_ready(self, project):
        write_book(project, "eden", voice="", cast={})
        assert not review(project)[0].ready


class TestTheCastAConfiguredDefaultProvides:
    """A freshly imported book carries no cast of its own.

    Chunking fills it in from `config/cast.yml`. Reading the empty field as
    "nobody would read it" held back every book in a batch that was in fact
    ready, which is how this was found: two imported books, both fine, both
    refused.
    """

    def _cast(self, project, voice="michal"):
        (project / "config").mkdir(parents=True, exist_ok=True)
        (project / "config" / "cast.yml").write_text(
            f"narrator:\n  voice: {voice}\ndialogue:\n  voice: {voice}\n",
            encoding="utf-8")

    def test_a_book_with_no_cast_of_its_own_is_ready(self, project):
        write_book(project, "eden", voice="", cast={})
        self._cast(project)
        row = next(r for r in review(project) if r.slug == "eden")
        assert row.ready and row.reader == "michal"

    def test_the_row_says_the_cast_is_borrowed(self, project):
        write_book(project, "eden", voice="", cast={})
        self._cast(project)
        assert next(r for r in review(project) if r.slug == "eden").cast_is_default

    def test_a_books_own_cast_is_not_called_borrowed(self, two_books):
        self._cast(two_books, voice="ala")
        row = next(r for r in review(two_books) if r.slug == "eden")
        assert not row.cast_is_default and row.reader == "michal"

    def test_with_no_cast_anywhere_it_is_still_held_back(self, project):
        write_book(project, "eden", voice="", cast={})
        assert not next(r for r in review(project) if r.slug == "eden").ready


class TestTheReviewRow:
    def test_it_names_the_file_the_book_came_from(self, two_books):
        row = next(r for r in review(two_books) if r.slug == "eden")
        assert row.source == "eden.txt"

    def test_one_voice_reads_as_one_name(self, two_books):
        assert next(r for r in review(two_books) if r.slug == "eden").reader == "michal"

    def test_a_split_cast_names_everyone(self, project):
        write_book(project, "eden", voice="",
                   cast={"narrator": "michal", "dialogue": "ala"})
        assert review(project)[0].reader == "ala, michal"

    def test_a_queued_book_says_so(self, two_books):
        Queue(two_books).add("eden", "synth")
        assert next(r for r in review(two_books) if r.slug == "eden").queued == "pending"


class TestQueueingABatch:
    def test_the_chosen_books_are_queued_in_pipeline_order(self, two_books):
        result = queue_books(two_books, ["eden"], steps=["synth", "assemble"])
        assert result.queued == ["eden/synth", "eden/assemble"]

    def test_the_batch_default_voice_reaches_every_book(self, two_books):
        queue_books(two_books, ["eden", "solaris"], steps=["synth"], voice="ala")
        voices = [i.args.get("voice") for i in Queue(two_books).items()]
        assert voices == ["ala", "ala"]

    def test_one_book_can_disagree_with_the_batch(self, two_books):
        queue_books(two_books, ["eden", "solaris"], steps=["synth"], voice="ala",
                    overrides={"solaris": {"voice": "michal"}})
        chosen = {i.slug: i.args.get("voice") for i in Queue(two_books).items()}
        assert chosen == {"eden": "ala", "solaris": "michal"}

    def test_a_book_that_needs_a_decision_is_skipped_with_its_reason(self, project):
        write_book(project, "eden", voice="", cast={})
        result = queue_books(project, ["eden"])
        assert result.queued == []
        assert "nobody would read it" in result.skipped["eden"]

    def test_a_book_already_queued_is_not_queued_twice(self, two_books):
        # The second copy would render the same fragments into the same
        # directory as the first.
        queue_books(two_books, ["eden"], steps=["synth"])
        again = queue_books(two_books, ["eden"], steps=["synth"])
        assert "already in the queue" in again.skipped["eden"]

    def test_a_book_with_nothing_left_is_skipped(self, two_books):
        audio = two_books / "data" / "audio" / "eden"
        (audio / "report.json").write_text(
            json.dumps({"schema_version": 6, "slug": "eden", "ok": True,
                        "chunks": 1, "rendered": 1, "failed": 0}), encoding="utf-8")
        (two_books / "data" / "out").mkdir(parents=True, exist_ok=True)
        (two_books / "data" / "out" / "eden.m4b").write_bytes(b"x")
        assert queue_books(two_books, ["eden"]).skipped["eden"] == "nothing left to do"

    def test_an_unknown_book_says_so_rather_than_failing_the_batch(self, two_books):
        result = queue_books(two_books, ["eden", "never-heard-of-it"])
        assert result.queued and "never-heard-of-it" in result.skipped

    def test_the_format_default_reaches_assembly(self, two_books):
        # Narrating comes along uninvited, because the book still needs it and
        # assembling without it would produce nothing.
        queue_books(two_books, ["eden"], steps=["assemble"], fmt="mp3")
        steps = {i.action: i.args for i in Queue(two_books).items()}
        assert steps["assemble"] == {"format": "mp3"}
        assert "synth" in steps

    def test_a_batch_name_is_carried_through(self, two_books):
        queue_books(two_books, ["eden"], steps=["synth"], batch="tuesday")
        assert Queue(two_books).items()[0].batch == "tuesday"
