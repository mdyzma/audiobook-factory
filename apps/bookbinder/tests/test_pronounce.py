"""Deciding whether a pronunciation fix is worth re-chunking a book for.

The dictionary already worked. What it lacked was any way to find out what a
rule would do without applying it, re-chunking several thousand fragments,
re-rendering some of them and listening. Previewing is free because the
substitution is pure text, and that is the whole argument for this module.
"""

from __future__ import annotations

import json

import pytest

from bookbinder.pronounce import (
    DictionaryError,
    check,
    excerpt,
    preview,
    read,
    report,
    save,
)

CHUNKS = [
    {"id": "ch001_0000", "chapter_title": "Przybysz", "language": "pl",
     "text": "Ocean falował pod stacją, a Kelvin patrzył w okno."},
    {"id": "ch001_0001", "chapter_title": "Przybysz", "language": "pl",
     "text": "Kelvin zszedł po drabince do kabiny."},
    {"id": "ch002_0000", "chapter_title": "Lustra", "language": "pl",
     "text": "Nikt nie odpowiadał na wezwania."},
]


@pytest.fixture
def book(tmp_path):
    folder = tmp_path / "data" / "book" / "solaris"
    folder.mkdir(parents=True)
    (folder / "chunks.jsonl").write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in CHUNKS),
        encoding="utf-8")
    return tmp_path


class TestSeeingWhatARuleWouldDo:
    def test_it_finds_every_occurrence(self, book):
        found = preview(book, "solaris", {"Kelvin": "Kelwin"})
        assert found.counts["Kelvin"] == 2

    def test_it_shows_the_passage_both_ways(self, book):
        found = preview(book, "solaris", {"Kelvin": "Kelwin"})
        first = found.occurrences[0]
        assert "Kelvin" in first.source and "Kelwin" in first.spoken

    def test_it_names_the_chapter(self, book):
        # Where in the book, because that is how somebody checks it in context.
        found = preview(book, "solaris", {"Kelvin": "Kelwin"})
        assert found.occurrences[0].chapter == "Przybysz"

    def test_a_rule_matching_nothing_is_called_out(self, book):
        # Nearly always a spelling or an accent, and otherwise invisible: the
        # book simply reads exactly as it did before.
        found = preview(book, "solaris", {"Snaut": "Snałt"})
        assert found.unused == ["Snaut"] and found.total == 0

    def test_the_book_is_not_touched(self, book):
        before = (book / "data/book/solaris/chunks.jsonl").read_bytes()
        preview(book, "solaris", {"Kelvin": "Kelwin"})
        assert (book / "data/book/solaris/chunks.jsonl").read_bytes() == before
        assert not (book / "data/book/solaris/pronunciation.yml").exists()

    def test_other_rules_do_not_crowd_out_the_one_being_judged(self, book):
        """Abbreviations and symbols are somebody else's rules.

        `prepare` expands those too, and showing their edits here would bury
        the entry the person is actually deciding about.
        """
        folder = book / "data/book/solaris"
        chunk = dict(CHUNKS[0], text="Kelvin mieszkał przy ul. Morskiej 15%.")
        (folder / "chunks.jsonl").write_text(
            json.dumps(chunk, ensure_ascii=False) + "\n", encoding="utf-8")
        found = preview(book, "solaris", {"Kelvin": "Kelwin"})
        assert [o.written for o in found.occurrences] == ["Kelvin"]

    def test_the_saved_dictionary_is_used_when_none_is_given(self, book):
        save(book, "solaris", {"Kelvin": "Kelwin"})
        assert preview(book, "solaris").counts["Kelvin"] == 2

    def test_a_book_with_no_fragments_yet_says_nothing(self, tmp_path):
        assert preview(tmp_path, "never-chunked", {"a": "b"}).total == 0


class TestRefusingEntriesThatWouldNotWork:
    """Each of these is a mistake somebody makes once and then hunts for."""

    def test_a_rule_that_changes_nothing_is_refused(self, book):
        with pytest.raises(DictionaryError, match="written the same"):
            check({"Kelvin": "Kelvin"})

    def test_an_empty_written_form_is_refused(self):
        # It would match everywhere.
        with pytest.raises(DictionaryError, match="needs a word"):
            check({"": "something"})

    def test_an_empty_spoken_form_is_refused(self):
        with pytest.raises(DictionaryError, match="nothing to say"):
            check({"Kelvin": ""})

    def test_a_line_break_is_refused(self):
        with pytest.raises(DictionaryError, match="line break"):
            check({"Kelvin": "Kel\nwin"})

    def test_surrounding_space_is_not_a_reason_to_refuse(self):
        assert check({"  Kelvin  ": " Kelwin "}) == {"Kelvin": "Kelwin"}


class TestWritingItDown:
    def test_it_round_trips(self, book):
        save(book, "solaris", {"Kelvin": "Kelwin", "Snaut": "Snałt"})
        assert read(book, "solaris") == {"Kelvin": "Kelwin", "Snaut": "Snałt"}

    def test_a_quote_in_an_entry_survives(self, book):
        # Written by hand rather than dumped, so this is worth pinning.
        save(book, "solaris", {'the "Ocean"': "the ocean"})
        assert read(book, "solaris") == {'the "Ocean"': "the ocean"}

    def test_longer_entries_come_first(self, book):
        # So a full name beats a surname, which is how `prepare` applies them.
        save(book, "solaris", {"Kelvin": "Kelwin", "Kris Kelvin": "Kris Kelwin"})
        body = (book / "data/book/solaris/pronunciation.yml").read_text(encoding="utf-8")
        assert body.index("Kris Kelvin") < body.index('"Kelvin"')

    def test_it_stays_readable_by_a_person(self, book):
        save(book, "solaris", {"Kelvin": "Kelwin"})
        body = (book / "data/book/solaris/pronunciation.yml").read_text(encoding="utf-8")
        assert body.startswith("# How this book says")
        assert "just chunk" in body

    def test_saving_replaces_rather_than_appends(self, book):
        save(book, "solaris", {"Kelvin": "Kelwin"})
        save(book, "solaris", {"Snaut": "Snałt"})
        assert read(book, "solaris") == {"Snaut": "Snałt"}

    def test_a_refused_entry_leaves_the_old_file_alone(self, book):
        save(book, "solaris", {"Kelvin": "Kelwin"})
        with pytest.raises(DictionaryError):
            save(book, "solaris", {"Snaut": "Snaut"})
        assert read(book, "solaris") == {"Kelvin": "Kelwin"}


class TestQuotingAPassage:
    def test_it_keeps_the_match_in_the_middle(self):
        text = "a" * 200 + "KELVIN" + "b" * 200
        out = excerpt(text, 200, 206)
        assert "KELVIN" in out and out.startswith("…") and out.endswith("…")

    def test_a_short_passage_is_quoted_whole(self):
        assert excerpt("Kelvin patrzył.", 0, 6) == "Kelvin patrzył."


class TestSayingIt:
    def test_it_says_how_many_and_what_next(self, book):
        text = report(preview(book, "solaris", {"Kelvin": "Kelwin"}))
        assert "2 occurrence" in text and "just chunk" in text

    def test_it_says_when_a_rule_matches_nothing(self, book):
        assert "match nothing" in report(preview(book, "solaris", {"Snaut": "S"}))

    def test_an_empty_dictionary_says_so(self, book):
        assert report(preview(book, "solaris", {})) == \
            "no pronunciation entries for this book"
