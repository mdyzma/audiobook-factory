"""Text normalisation decides how the narrator pronounces things.

An em-dash read literally, or a footnote marker read as a number, is audible in
the finished audiobook, so these are correctness tests rather than cosmetics.
"""

from __future__ import annotations

from bookbinder.ingest import from_text, normalise, slugify


class TestNormalise:
    def test_em_and_en_dashes_become_spoken_pauses(self):
        assert normalise("słowo — drugie") == "słowo - drugie"
        assert normalise("słowo – drugie") == "słowo - drugie"

    def test_curly_quotes_flattened(self):
        assert normalise("„cytat”") == '"cytat"'
        assert normalise("‘a’") == "'a'"

    def test_ellipsis_expanded(self):
        assert normalise("czekaj…") == "czekaj..."

    def test_soft_hyphen_removed(self):
        assert normalise("mo­że") == "może"

    def test_line_break_hyphenation_rejoined(self):
        assert normalise("kon-\nstrukcja") == "konstrukcja"

    def test_footnote_markers_stripped(self):
        assert normalise("słowo[12] dalej") == "słowo dalej"
        assert normalise("słowo{7} dalej") == "słowo dalej"

    def test_footnotes_kept_when_disabled(self):
        assert "[12]" in normalise("słowo[12]", strip_footnotes=False)

    def test_polish_diacritics_survive(self):
        text = "zażółć gęślą jaźń ŁÓDŹ"
        assert normalise(text) == text

    def test_whitespace_collapsed(self):
        assert normalise("a      b\t\tc") == "a b c"


class TestSlugify:
    def test_strips_decomposable_diacritics(self):
        assert slugify("Żmija w gąszczu") == "zmija-w-gaszczu"

    def test_transliterates_letters_nfkd_cannot_decompose(self):
        # These have no combining form, so a plain NFKD + ascii filter deletes
        # them: "Sołaris" would become "soaris" and "Łódź" would become "odz".
        assert slugify("Sołaris i Ocean") == "solaris-i-ocean"
        assert slugify("Łódź") == "lodz"
        assert slugify("Straße") == "strasse"

    def test_never_returns_empty(self):
        assert slugify("???") == "book"


class TestFromText:
    def test_splits_on_markdown_headings(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text("# Jeden\n\nTreść pierwsza.\n\n# Dwa\n\nTreść druga.\n", encoding="utf-8")
        _meta, chapters = from_text(f, strip_footnotes=True)
        assert [c["title"] for c in chapters] == ["Jeden", "Dwa"]

    def test_headingless_file_is_one_chapter(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text("Akapit jeden.\n\nAkapit dwa.\n", encoding="utf-8")
        _meta, chapters = from_text(f, strip_footnotes=True)
        assert len(chapters) == 1
        assert len(chapters[0]["paragraphs"]) == 2

    def test_blank_paragraphs_dropped(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text("Akapit.\n\n\n\n   \n\nDrugi.\n", encoding="utf-8")
        _meta, chapters = from_text(f, strip_footnotes=True)
        assert chapters[0]["paragraphs"] == ["Akapit.", "Drugi."]
