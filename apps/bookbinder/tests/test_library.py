"""Working out what is in a folder, and what is already here.

The dangerous cases are not the files that are obviously new. They are the
same book arriving twice under two names, an edited copy arriving under the
name already used, and two unrelated books sharing a title. Each of those, got
wrong, overwrites work that took hours to render.
"""

from __future__ import annotations

import json

import pytest

from bookbinder.library import (
    BOOK_SUFFIXES,
    classify,
    file_sha256,
    imported_books,
    report,
    scan,
    unique_slug,
)

# Long enough that the language can be established from the text alone. A
# shorter sample pauses for review, which is correct and is its own test.
POLISH = (
    "Ocean falował pod stacją, a wiatr wiał nieprzerwanie od trzech dni. "
    "Zszedłem po drabince do kabiny i zamknąłem właz za sobą, nasłuchując. "
    "Śnieg padał na łąki pod Łodzią, a mgła osiadła na rzece o świcie. "
    "Wczesnym rankiem wróciłem na pokład i długo patrzyłem w stronę brzegu. "
    "Woda była ciemna i gęsta, a nad nią unosiła się para, którą wiatr "
    "rozwiewał w długie smugi ciągnące się aż po widnokrąg bez końca. "
    "Nikt nie odpowiadał na wezwania, więc usiadłem przy pulpicie i czekałem."
)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "data" / "book").mkdir(parents=True)
    (tmp_path / "inbox").mkdir()
    return tmp_path


def book(project, name, text=POLISH):
    path = project / "inbox" / name
    path.write_text(text, encoding="utf-8")
    return path


def already_imported(project, slug, source_sha256, original_source=""):
    """A book that has been through ingestion, as the scan sees it.

    `original_source` is the path it was imported from, which is what tells a
    corrected copy of this book apart from a different book of the same name.
    """
    book_dir = project / "data" / "book" / slug
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "chapters.json").write_text(json.dumps(
        {"meta": {"slug": slug, "source_sha256": source_sha256,
                  "original_source": str(original_source)}, "chapters": []}),
        encoding="utf-8")


class TestWhatTheScanFinds:
    def test_it_finds_books(self, project):
        book(project, "solaris.txt")
        book(project, "lustra.txt", POLISH + " Drugi.")
        found = scan(project, project / "inbox")
        assert sorted(c.name for c in found.books) == ["lustra.txt", "solaris.txt"]

    @pytest.mark.parametrize("suffix", BOOK_SUFFIXES)
    def test_every_supported_extension_is_read(self, project, suffix):
        (project / "inbox" / f"b{suffix}").write_bytes(b"content")
        assert scan(project, project / "inbox").books

    def test_the_order_is_deterministic(self, project):
        for name in ("c.txt", "a.txt", "b.txt"):
            book(project, name, POLISH + name)
        names = [c.name for c in scan(project, project / "inbox").books]
        assert names == sorted(names)

    def test_unsupported_files_are_named_not_silently_skipped(self, project):
        (project / "inbox" / "book.mobi").write_bytes(b"x")
        found = scan(project, project / "inbox")
        assert [c.name for c in found.unsupported] == ["book.mobi"]
        assert "not read here" in found.unsupported[0].note

    def test_a_file_with_no_extension_is_reported(self, project):
        (project / "inbox" / "README").write_bytes(b"x")
        assert scan(project, project / "inbox").unsupported

    def test_hidden_files_are_ignored_entirely(self, project):
        (project / "inbox" / ".DS_Store").write_bytes(b"x")
        found = scan(project, project / "inbox")
        assert not found.books and not found.unsupported

    def test_a_missing_folder_says_so(self, project):
        with pytest.raises(NotADirectoryError):
            scan(project, project / "absent")

    def test_an_empty_folder_is_not_an_error(self, project):
        found = scan(project, project / "inbox")
        assert found.books == [] and found.unsupported == []


class TestRecursion:
    def test_subfolders_are_left_alone_by_default(self, project):
        nested = project / "inbox" / "originals"
        nested.mkdir()
        (nested / "solaris.txt").write_text(POLISH, encoding="utf-8")
        assert scan(project, project / "inbox").books == []

    def test_recursion_is_asked_for(self, project):
        nested = project / "inbox" / "originals"
        nested.mkdir()
        (nested / "solaris.txt").write_text(POLISH, encoding="utf-8")
        found = scan(project, project / "inbox", recursive=True)
        assert [c.name for c in found.books] == ["solaris.txt"]


class TestDuplicatesWithinOneScan:
    def test_the_same_bytes_twice_is_a_duplicate(self, project):
        book(project, "aaa.txt")
        book(project, "zzz.txt")
        found = scan(project, project / "inbox")
        # Deterministic order decides which one is the original.
        statuses = {c.name: c.status for c in found.books}
        assert statuses == {"aaa.txt": "new", "zzz.txt": "duplicate"}

    def test_a_duplicate_names_what_it_duplicates(self, project):
        book(project, "aaa.txt")
        book(project, "zzz.txt")
        duplicate = next(c for c in scan(project, project / "inbox").books
                         if c.status == "duplicate")
        assert duplicate.duplicate_of == "aaa.txt"

    def test_a_duplicate_is_not_offered_for_import(self, project):
        book(project, "a.txt")
        book(project, "b.txt")
        assert len(scan(project, project / "inbox").ready) == 1


class TestAgainstWhatIsAlreadyImported:
    def test_the_same_bytes_already_here_reads_as_unchanged(self, project):
        path = book(project, "solaris.txt")
        already_imported(project, "solaris", file_sha256(path))

        candidate = scan(project, project / "inbox").books[0]
        assert candidate.status == "unchanged"
        assert not candidate.ready

    def test_it_is_recognised_under_a_different_filename(self, project):
        # The same book, renamed. Importing it again would be a second copy.
        path = book(project, "solaris-final-v2.txt")
        already_imported(project, "solaris", file_sha256(path))

        candidate = scan(project, project / "inbox").books[0]
        assert candidate.status == "unchanged"
        assert candidate.slug == "solaris"

    def test_edited_bytes_from_the_same_file_read_as_revised(self, project):
        path = book(project, "solaris.txt", POLISH + " Poprawiony akapit.")
        already_imported(project, "solaris", "a-different-hash-entirely", path)

        candidate = scan(project, project / "inbox").books[0]
        assert candidate.status == "revised"
        assert candidate.slug == "solaris"
        assert candidate.ready
        assert "invalidates its audio" in candidate.note

    def test_a_different_file_of_the_same_name_is_not_a_revision(self, project):
        # A second book that happens to be called solaris.txt, somewhere else.
        # Treating it as a revision would overwrite the first one's text.
        already_imported(project, "solaris", "the-first-books-hash",
                         project / "elsewhere" / "solaris.txt")
        book(project, "solaris.txt", "A different book about the sea entirely.")

        candidate = scan(project, project / "inbox").books[0]
        assert candidate.status == "new"
        assert candidate.slug != "solaris"

    def test_a_book_nothing_knows_about_is_new(self, project):
        book(project, "solaris.txt")
        already_imported(project, "inne-morze", "unrelated-hash")
        assert scan(project, project / "inbox").books[0].status == "new"

    def test_a_corrupt_manifest_does_not_stop_the_scan(self, project):
        book(project, "solaris.txt")
        book_dir = project / "data" / "book" / "broken"
        book_dir.mkdir(parents=True)
        (book_dir / "chapters.json").write_text("{not json", encoding="utf-8")
        assert scan(project, project / "inbox").books

    def test_no_imported_books_yet(self, project):
        assert imported_books(project) == {}


class TestNamingCannotOverwrite:
    def test_two_unrelated_books_with_one_title_get_separate_names(self, project):
        # The failure this exists to prevent: the second import replacing the
        # first, along with however many hours of audio it had.
        already_imported(project, "solaris", "the-first-books-hash",
                         project / "elsewhere" / "other.txt")
        book(project, "solaris.txt", "A completely different book about the sea.")

        candidate = scan(project, project / "inbox").books[0]
        assert candidate.slug != "solaris"
        assert candidate.slug.startswith("solaris-")

    def test_two_new_books_with_one_title_in_one_folder(self, project):
        sub = project / "inbox" / "second"
        sub.mkdir()
        book(project, "solaris.txt", "First book entirely.")
        (sub / "solaris.txt").write_text("Second book entirely.", encoding="utf-8")

        slugs = [c.slug for c in scan(project, project / "inbox", recursive=True).books]
        assert len(set(slugs)) == 2

    def test_an_unused_name_is_taken_as_is(self):
        assert unique_slug("solaris", set()) == "solaris"

    def test_a_name_already_held_is_numbered(self):
        assert unique_slug("solaris", {"solaris"}) == "solaris-2"

    def test_numbering_continues_past_what_is_taken(self):
        assert unique_slug("solaris", {"solaris", "solaris-2"}) == "solaris-3"


class TestClassification:
    def test_every_status_is_one_of_the_documented_ones(self, tmp_path):
        from bookbinder.library import KnownBook

        here = tmp_path / "a.txt"
        statuses = {"new", "unchanged", "revised", "duplicate"}
        cases = [
            ({}, {}, set()),
            ({"a": KnownBook("a", "h1", str(here))}, {}, {"a"}),
            ({"a": KnownBook("a", "h2", str(here))}, {}, {"a"}),
            ({}, {"h1": "earlier.txt"}, set()),
        ]
        for known, seen, taken in cases:
            assert classify(here, "h1", "a", known, seen, taken).status in statuses


class TestReport:
    def test_it_names_each_file_and_its_verdict(self, project):
        book(project, "solaris.txt")
        text = report(scan(project, project / "inbox"))
        assert "solaris.txt" in text and "new" in text

    def test_it_summarises_what_would_be_imported(self, project):
        book(project, "a.txt", "First.")
        book(project, "b.txt", "Second.")
        (project / "inbox" / "c.mobi").write_bytes(b"x")
        text = report(scan(project, project / "inbox"))
        assert "2 to import" in text and "1 skipped" in text

    def test_an_empty_folder_says_so(self, project):
        assert "nothing here to read" in report(scan(project, project / "inbox"))


class TestAlreadyImportedOutranksDuplicate:
    """Every copy of a book already here deserves the same answer.

    Otherwise the file that was actually imported reads as a duplicate of its
    own copy, purely because of which one the scan reached first.
    """

    def test_both_copies_of_an_imported_book_read_as_unchanged(self, project):
        path = book(project, "aaa.txt")
        book(project, "zzz.txt")
        already_imported(project, "solaris", file_sha256(path), path)

        statuses = {c.status for c in scan(project, project / "inbox").books}
        assert statuses == {"unchanged"}

    def test_both_point_at_the_same_book(self, project):
        path = book(project, "aaa.txt")
        book(project, "zzz.txt")
        already_imported(project, "solaris", file_sha256(path), path)

        assert {c.slug for c in scan(project, project / "inbox").books} == {"solaris"}

    def test_neither_is_offered_for_import(self, project):
        path = book(project, "aaa.txt")
        book(project, "zzz.txt")
        already_imported(project, "solaris", file_sha256(path), path)

        assert scan(project, project / "inbox").ready == []


class TestImportingAWholeFolder:
    """Twenty books must not wait on the one that needs a decision.

    That is the whole reason to import a folder rather than a file at a time,
    so failure isolation is the property under test rather than a nicety.
    """

    @pytest.fixture
    def configured(self, project, monkeypatch):
        (project / "justfile").write_text("", encoding="utf-8")
        (project / "config").mkdir(exist_ok=True)
        (project / "config" / "pipeline.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(project))
        return project

    def test_every_ready_book_is_imported(self, configured):
        from bookbinder.library import import_folder

        book(configured, "solaris.txt", POLISH)
        book(configured, "lustra.txt", POLISH + " Zupełnie inny akapit tutaj.")

        batch = import_folder(configured, configured / "inbox", language="pl")
        assert len(batch.imported) == 2
        assert batch.paused == []

    def test_each_book_lands_in_its_own_place(self, configured):
        from bookbinder.library import import_folder

        book(configured, "solaris.txt", POLISH)
        book(configured, "lustra.txt", POLISH + " Zupełnie inny akapit tutaj.")

        import_folder(configured, configured / "inbox", language="pl")
        slugs = sorted(p.name for p in (configured / "data" / "book").iterdir())
        assert slugs == ["lustra", "solaris"]

    def test_one_book_needing_review_does_not_stop_the_others(self, configured):
        # The failure isolation the assessment asks for, stated as a test.
        from bookbinder.library import import_folder

        book(configured, "solaris.txt", POLISH)
        book(configured, "krotki.txt", "Za krótki.")

        batch = import_folder(configured, configured / "inbox")
        assert [r.source.name for r in batch.imported] == ["solaris.txt"]
        assert [r.source.name for r in batch.paused] == ["krotki.txt"]
        assert any("characters of text" in r for r in batch.paused[0].reasons)

    def test_an_unreadable_file_does_not_stop_the_others(self, configured):
        from bookbinder.library import import_folder

        book(configured, "solaris.txt", POLISH)
        (configured / "inbox" / "broken.txt").write_bytes(
            bytes([0x81, 0x8D, 0x8F, 0x90, 0x9D]) * 60)

        batch = import_folder(configured, configured / "inbox", language="pl")
        assert [r.source.name for r in batch.imported] == ["solaris.txt"]
        assert len(batch.paused) == 1
        assert batch.paused[0].reasons

    def test_a_paused_book_writes_nothing(self, configured):
        from bookbinder.library import import_folder

        book(configured, "krotki.txt", "Za krótki.")
        import_folder(configured, configured / "inbox")
        assert list((configured / "data" / "book").iterdir()) == []

    def test_a_bulk_language_gets_a_whole_folder_through(self, configured):
        # Twenty books known to be Polish should not need twenty answers.
        from bookbinder.library import import_folder

        book(configured, "a.txt", "Krótkie zdanie.")
        book(configured, "b.txt", "Inne krótkie zdanie.")

        batch = import_folder(configured, configured / "inbox", language="pl")
        assert len(batch.imported) == 2
        assert all(r.language == "pl" for r in batch.imported)

    def test_already_imported_books_are_not_imported_again(self, configured):
        from bookbinder.library import import_folder

        book(configured, "solaris.txt", POLISH)
        first = import_folder(configured, configured / "inbox", language="pl")
        second = import_folder(configured, configured / "inbox", language="pl")

        assert len(first.imported) == 1
        assert second.results == []

    def test_the_report_names_the_paused_books_and_why(self, configured):
        from bookbinder.library import import_folder, import_report

        book(configured, "solaris.txt", POLISH)
        book(configured, "krotki.txt", "Za krótki.")

        text = import_report(import_folder(configured, configured / "inbox"))
        assert "imported" in text and "paused" in text
        assert "krotki.txt" in text
        assert "need a decision" in text
