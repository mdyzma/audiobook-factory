"""Text normalisation decides how the narrator pronounces things.

An em-dash read literally, or a footnote marker read as a number, is audible in
the finished audiobook, so these are correctness tests rather than cosmetics.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import bookbinder.ingest as ingest_mod
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

    def test_text_before_the_first_heading_is_kept(self, tmp_path):
        # A dedication, an epigraph or an untitled opening chapter. Splitting on
        # headings used to discard everything preceding the first one.
        f = tmp_path / "b.txt"
        f.write_text("Dedykacja dla żony.\n\nMotto rozdziału.\n\n"
                     "# Jeden\n\nTreść pierwsza.\n", encoding="utf-8")
        _meta, chapters = from_text(f, strip_footnotes=True)
        assert [c["title"] for c in chapters] == ["b", "Jeden"]
        assert chapters[0]["paragraphs"] == ["Dedykacja dla żony.", "Motto rozdziału."]

    def test_no_preamble_chapter_when_the_file_opens_with_a_heading(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text("# Jeden\n\nTreść pierwsza.\n", encoding="utf-8")
        _meta, chapters = from_text(f, strip_footnotes=True)
        assert [c["title"] for c in chapters] == ["Jeden"]

    def test_wrapped_hyphenation_rejoined_through_the_real_path(self, tmp_path):
        # normalise() has always handled this, but _paragraphs() collapsed the
        # newlines first, so the rule could never match on a real file.
        f = tmp_path / "b.txt"
        f.write_text("Treść roz-\ndziału pierwszego trwa dalej.\n", encoding="utf-8")
        _meta, chapters = from_text(f, strip_footnotes=True)
        assert chapters[0]["paragraphs"] == ["Treść rozdziału pierwszego trwa dalej."]

    def test_wrapped_line_without_hyphen_becomes_one_space(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text("Pierwsza linia\ndruga linia.\n", encoding="utf-8")
        _meta, chapters = from_text(f, strip_footnotes=True)
        assert chapters[0]["paragraphs"] == ["Pierwsza linia druga linia."]


class TestFromEpub:
    """Every EPUB carries a navigation document. Narrating it means reading the
    table of contents aloud, usually as a final chapter."""

    def _book(self, tmp_path):
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("t"); book.set_title("Solaris"); book.set_language("pl")
        book.add_author("Lem")
        c1 = epub.EpubHtml(title="Przybysz", file_name="c1.xhtml", lang="pl")
        c1.content = "<h1>Przybysz</h1><p>Zszedłem po drabince.</p>"
        c2 = epub.EpubHtml(title="Lustra", file_name="c2.xhtml", lang="pl")
        c2.content = "<h1>Lustra</h1><p>Potrzeba nam luster.</p>"
        for c in (c1, c2):
            book.add_item(c)
        # ebooklib annotates toc more narrowly than it accepts at runtime.
        book.toc = (c1, c2)  # type: ignore[assignment]
        book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav())
        book.spine = ["nav", c1, c2]
        path = tmp_path / "b.epub"
        epub.write_epub(str(path), book)
        return path

    def test_reads_metadata(self, tmp_path):
        from bookbinder.ingest import from_epub

        meta, _ = from_epub(self._book(tmp_path), True, True)
        assert meta["title"] == "Solaris"
        assert meta["author"] == "Lem"
        assert meta["language"] == "pl"

    def test_skips_the_navigation_document(self, tmp_path):
        from bookbinder.ingest import from_epub

        _meta, chapters = from_epub(self._book(tmp_path), True, True)
        titles = [c["title"] for c in chapters]
        assert titles == ["Przybysz", "Lustra"]
        assert not any("nav" in c["source_ref"] for c in chapters)

    def test_extracts_paragraphs(self, tmp_path):
        from bookbinder.ingest import from_epub

        _meta, chapters = from_epub(self._book(tmp_path), True, True)
        assert chapters[0]["paragraphs"] == ["Zszedłem po drabince."]


class TestEpubStructureIsPreserved:
    """Three ways a converter-produced EPUB used to lose or scramble text.

    None of them raised: the book simply came out shuffled, doubled or short,
    and the only way to notice was to listen to the finished audiobook.
    """

    def _awkward_book(self, tmp_path):
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("t"); book.set_title("Solaris"); book.set_language("pl")
        book.add_author("Lem")

        c1 = epub.EpubHtml(title="Jeden", file_name="c1.xhtml", lang="pl")
        c1.content = ("<h1>Jeden</h1><p>Pierwszy akapit.</p>"
                      "<blockquote><p>Cytat wewnętrzny.</p></blockquote>")
        # A converter that wraps prose in bare divs, with no block element.
        c2 = epub.EpubHtml(title="Dwa", file_name="c2.xhtml", lang="pl")
        c2.content = "<h1>Dwa</h1><div>Tekst bez akapitu.</div>"
        c3 = epub.EpubHtml(title="Trzy", file_name="c3.xhtml", lang="pl")
        c3.content = "<h1>Trzy</h1><p>Trzeci akapit.</p>"

        # Manifest order deliberately differs from reading order, which is what
        # real EPUBs do and what `get_items_of_type` alone cannot see.
        for c in (c3, c2, c1):
            book.add_item(c)
        book.toc = (c1, c2, c3)  # type: ignore[assignment]
        book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav())
        book.spine = ["nav", c1, c2, c3]
        path = tmp_path / "awkward.epub"
        epub.write_epub(str(path), book)
        return path

    def test_chapters_follow_the_spine_not_the_manifest(self, tmp_path):
        from bookbinder.ingest import from_epub

        _meta, chapters = from_epub(self._awkward_book(tmp_path), True, True)
        assert [c["title"] for c in chapters] == ["Jeden", "Dwa", "Trzy"]

    def test_nested_block_is_read_once(self, tmp_path):
        from bookbinder.ingest import from_epub

        _meta, chapters = from_epub(self._awkward_book(tmp_path), True, True)
        assert chapters[0]["paragraphs"] == ["Pierwszy akapit.", "Cytat wewnętrzny."]

    def test_chapter_without_block_tags_survives(self, tmp_path):
        from bookbinder.ingest import from_epub

        _meta, chapters = from_epub(self._awkward_book(tmp_path), True, True)
        assert chapters[1]["paragraphs"] == ["Tekst bez akapitu."]

    def test_fallback_does_not_repeat_the_chapter_title(self, tmp_path):
        # Chunking narrates the title as its own fragment already.
        from bookbinder.ingest import from_epub

        _meta, chapters = from_epub(self._awkward_book(tmp_path), True, True)
        assert "Dwa" not in chapters[1]["paragraphs"]


class TestIsNavigation:
    @pytest.mark.parametrize("name", ["nav.xhtml", "toc.xhtml", "TOC.html",
                                      "OEBPS/nav.xhtml", "contents.xhtml"])
    def test_recognises_nav_filenames(self, name):
        from bookbinder.ingest import is_navigation

        assert is_navigation(SimpleNamespace(get_name=lambda: name, properties=[]))

    @pytest.mark.parametrize("name", ["c1.xhtml", "chapter-nav-story.xhtml", "index.xhtml"])
    def test_leaves_content_alone(self, name):
        from bookbinder.ingest import is_navigation

        assert not is_navigation(SimpleNamespace(get_name=lambda: name, properties=[]))

    def test_recognises_the_epub3_manifest_property(self):
        from bookbinder.ingest import is_navigation

        assert is_navigation(SimpleNamespace(get_name=lambda: "x.xhtml", properties=["nav"]))


class TestMetadataOverrides:
    """Plain text carries no metadata, so the title comes from the filename.

    It cannot be corrected afterwards by editing book.json, because chunking
    rebuilds that file from chapters.json every time it runs.
    """

    def test_title_and_author_can_be_given(self, tmp_path, monkeypatch):
        src = tmp_path / "some_scanned_file_682.txt"
        src.write_text("# Rozdział\n\nZdanie pierwsze.\n", encoding="utf-8")
        (tmp_path / "justfile").write_text("", encoding="utf-8")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "pipeline.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(tmp_path))

        result = CliRunner().invoke(ingest_mod.app, [
            str(src), "--slug", "book", "--language", "pl",
            "--title", "Kroniki Jakuba Wędrowycza", "--author", "Andrzej Pilipiuk",
        ])
        assert result.exit_code == 0, result.output

        meta = json.loads(
            (tmp_path / "data" / "book" / "book" / "chapters.json").read_text(encoding="utf-8")
        )["meta"]
        assert meta["title"] == "Kroniki Jakuba Wędrowycza"
        assert meta["author"] == "Andrzej Pilipiuk"

    def test_without_them_the_filename_still_wins(self, tmp_path, monkeypatch):
        src = tmp_path / "some_scanned_file_682.txt"
        src.write_text("# Rozdział\n\nZdanie pierwsze.\n", encoding="utf-8")
        (tmp_path / "justfile").write_text("", encoding="utf-8")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "pipeline.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(tmp_path))

        result = CliRunner().invoke(ingest_mod.app, [str(src), "--slug", "book"])
        assert result.exit_code == 0, result.output

        meta = json.loads(
            (tmp_path / "data" / "book" / "book" / "chapters.json").read_text(encoding="utf-8")
        )["meta"]
        assert meta["title"] == "some_scanned_file_682"
        assert meta["author"] == "Unknown"
