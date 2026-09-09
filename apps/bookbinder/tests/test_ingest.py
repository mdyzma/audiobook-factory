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

# Enough Polish that the language is established from the text rather than
# abstained on. Short files stopping for review is deliberate; see
# TestIngestEstablishesEncodingAndLanguage below.
POLISH_PAGE = (
    "Ocean falował pod stacją, a wiatr wiał nieprzerwanie od trzech dni. "
    "Zszedłem po drabince do kabiny i zamknąłem właz za sobą, nasłuchując. "
    "Śnieg padał na łąki pod Łodzią, a mgła osiadła na rzece o świcie. "
    "Wczesnym rankiem wróciłem na pokład i długo patrzyłem w stronę brzegu. "
    "Woda była ciemna i gęsta, a nad nią unosiła się para, którą wiatr "
    "rozwiewał w długie smugi ciągnące się aż po widnokrąg. "
    "Nikt nie odpowiadał na wezwania, więc usiadłem przy pulpicie i czekałem, "
    "licząc kolejne minuty ciszy przerywanej tylko trzaskiem aparatury.\n"
)


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
        chapters = from_text(f, strip_footnotes=True).chapters
        assert [c["title"] for c in chapters] == ["Jeden", "Dwa"]

    def test_headingless_file_is_one_chapter(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text("Akapit jeden.\n\nAkapit dwa.\n", encoding="utf-8")
        chapters = from_text(f, strip_footnotes=True).chapters
        assert len(chapters) == 1
        assert len(chapters[0]["paragraphs"]) == 2

    def test_blank_paragraphs_dropped(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text("Akapit.\n\n\n\n   \n\nDrugi.\n", encoding="utf-8")
        chapters = from_text(f, strip_footnotes=True).chapters
        assert chapters[0]["paragraphs"] == ["Akapit.", "Drugi."]

    def test_text_before_the_first_heading_is_kept(self, tmp_path):
        # A dedication, an epigraph or an untitled opening chapter. Splitting on
        # headings used to discard everything preceding the first one.
        f = tmp_path / "b.txt"
        f.write_text("Dedykacja dla żony.\n\nMotto rozdziału.\n\n"
                     "# Jeden\n\nTreść pierwsza.\n", encoding="utf-8")
        chapters = from_text(f, strip_footnotes=True).chapters
        assert [c["title"] for c in chapters] == ["b", "Jeden"]
        assert chapters[0]["paragraphs"] == ["Dedykacja dla żony.", "Motto rozdziału."]

    def test_no_preamble_chapter_when_the_file_opens_with_a_heading(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text("# Jeden\n\nTreść pierwsza.\n", encoding="utf-8")
        chapters = from_text(f, strip_footnotes=True).chapters
        assert [c["title"] for c in chapters] == ["Jeden"]

    def test_wrapped_hyphenation_rejoined_through_the_real_path(self, tmp_path):
        # normalise() has always handled this, but _paragraphs() collapsed the
        # newlines first, so the rule could never match on a real file.
        f = tmp_path / "b.txt"
        f.write_text("Treść roz-\ndziału pierwszego trwa dalej.\n", encoding="utf-8")
        chapters = from_text(f, strip_footnotes=True).chapters
        assert chapters[0]["paragraphs"] == ["Treść rozdziału pierwszego trwa dalej."]

    def test_wrapped_line_without_hyphen_becomes_one_space(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text("Pierwsza linia\ndruga linia.\n", encoding="utf-8")
        chapters = from_text(f, strip_footnotes=True).chapters
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

        meta = from_epub(self._book(tmp_path), True, True).meta
        assert meta["title"] == "Solaris"
        assert meta["author"] == "Lem"
        assert meta["language"] == "pl"

    def test_skips_the_navigation_document(self, tmp_path):
        from bookbinder.ingest import from_epub

        chapters = from_epub(self._book(tmp_path), True, True).chapters
        titles = [c["title"] for c in chapters]
        assert titles == ["Przybysz", "Lustra"]
        assert not any("nav" in c["source_ref"] for c in chapters)

    def test_extracts_paragraphs(self, tmp_path):
        from bookbinder.ingest import from_epub

        chapters = from_epub(self._book(tmp_path), True, True).chapters
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

        chapters = from_epub(self._awkward_book(tmp_path), True, True).chapters
        assert [c["title"] for c in chapters] == ["Jeden", "Dwa", "Trzy"]

    def test_nested_block_is_read_once(self, tmp_path):
        from bookbinder.ingest import from_epub

        chapters = from_epub(self._awkward_book(tmp_path), True, True).chapters
        assert chapters[0]["paragraphs"] == ["Pierwszy akapit.", "Cytat wewnętrzny."]

    def test_chapter_without_block_tags_survives(self, tmp_path):
        from bookbinder.ingest import from_epub

        chapters = from_epub(self._awkward_book(tmp_path), True, True).chapters
        assert chapters[1]["paragraphs"] == ["Tekst bez akapitu."]

    def test_fallback_does_not_repeat_the_chapter_title(self, tmp_path):
        # Chunking narrates the title as its own fragment already.
        from bookbinder.ingest import from_epub

        chapters = from_epub(self._awkward_book(tmp_path), True, True).chapters
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
        # Long enough for the language to be established from the text; a file
        # this short would otherwise stop for review, which is its own test.
        src.write_text("# Rozdział\n\n" + POLISH_PAGE, encoding="utf-8")
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


class TestIngestEstablishesEncodingAndLanguage:
    """Ingestion decides two things it used to assume.

    Plain text was read as UTF-8 with `errors="replace"` and declared `pl`.
    Both are now established from the file, and a file that cannot settle
    either one stops instead of producing confident nonsense.
    """

    def _project(self, tmp_path, monkeypatch):
        (tmp_path / "justfile").write_text("", encoding="utf-8")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "pipeline.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(tmp_path))
        return CliRunner()

    def _meta(self, tmp_path, slug="book"):
        return json.loads((tmp_path / "data" / "book" / slug / "chapters.json")
                          .read_text(encoding="utf-8"))["meta"]

    def test_a_legacy_polish_file_keeps_its_diacritics(self, tmp_path, monkeypatch):
        runner = self._project(tmp_path, monkeypatch)
        src = tmp_path / "solaris.txt"
        src.write_bytes(POLISH_PAGE.encode("cp1250"))

        assert runner.invoke(ingest_mod.app, [str(src), "--slug", "book"]).exit_code == 0
        meta = self._meta(tmp_path)
        assert meta["encoding"]["encoding"] == "cp1250"
        assert meta["language"] == "pl"

        text = (tmp_path / "data" / "book" / "book" / "chapters.json").read_text(encoding="utf-8")
        assert "ł" in text and "ą" in text and "�" not in text

    def test_an_english_file_is_not_declared_polish(self, tmp_path, monkeypatch):
        runner = self._project(tmp_path, monkeypatch)
        src = tmp_path / "orwell.txt"
        src.write_text(
            "It was a bright cold day in April and the clocks were striking "
            "thirteen. Winston Smith slipped quickly through the glass doors "
            "of Victory Mansions, though not quickly enough to prevent a swirl "
            "of gritty dust from entering along with him at the door. "
            "The hallway smelt of boiled cabbage and old rag mats, and at one "
            "end of it a coloured poster had been tacked to the wall. "
            "It depicted simply an enormous face, more than a metre wide, the "
            "face of a man of about forty-five, with a heavy black moustache "
            "and ruggedly handsome features that followed you as you moved.\n",
            encoding="utf-8")

        assert runner.invoke(ingest_mod.app, [str(src), "--slug", "book"]).exit_code == 0
        assert self._meta(tmp_path)["language"] == "en"

    def test_the_source_hash_is_recorded(self, tmp_path, monkeypatch):
        import hashlib
        runner = self._project(tmp_path, monkeypatch)
        src = tmp_path / "solaris.txt"
        src.write_text(POLISH_PAGE, encoding="utf-8")

        runner.invoke(ingest_mod.app, [str(src), "--slug", "book"])
        assert self._meta(tmp_path)["source_sha256"] == \
            hashlib.sha256(src.read_bytes()).hexdigest()

    def test_the_language_evidence_is_kept(self, tmp_path, monkeypatch):
        runner = self._project(tmp_path, monkeypatch)
        src = tmp_path / "solaris.txt"
        src.write_text(POLISH_PAGE, encoding="utf-8")

        runner.invoke(ingest_mod.app, [str(src), "--slug", "book"])
        decision = self._meta(tmp_path)["language_decision"]
        assert decision["method"] == "content"
        assert decision["detector"] and decision["confidence"] > 0
        assert decision["samples"]

    def test_a_book_too_short_to_judge_stops_rather_than_guessing(self, tmp_path, monkeypatch):
        runner = self._project(tmp_path, monkeypatch)
        src = tmp_path / "short.txt"
        src.write_text("Zdanie pierwsze.\n", encoding="utf-8")

        result = runner.invoke(ingest_mod.app, [str(src), "--slug", "book"])
        assert result.exit_code != 0
        assert "needs review" in result.output
        assert not (tmp_path / "data" / "book" / "book").exists()

    def test_an_explicit_language_gets_a_short_book_through(self, tmp_path, monkeypatch):
        runner = self._project(tmp_path, monkeypatch)
        src = tmp_path / "short.txt"
        src.write_text("Zdanie pierwsze.\n", encoding="utf-8")

        result = runner.invoke(ingest_mod.app,
                               [str(src), "--slug", "book", "--language", "pl"])
        assert result.exit_code == 0, result.output
        assert self._meta(tmp_path)["language"] == "pl"

    def test_an_encoding_override_is_obeyed_and_recorded(self, tmp_path, monkeypatch):
        runner = self._project(tmp_path, monkeypatch)
        src = tmp_path / "solaris.txt"
        src.write_bytes(POLISH_PAGE.encode("iso-8859-2"))

        result = runner.invoke(ingest_mod.app, [
            str(src), "--slug", "book", "--encoding", "iso-8859-2"])
        assert result.exit_code == 0, result.output
        enc = self._meta(tmp_path)["encoding"]
        assert (enc["encoding"], enc["method"]) == ("iso-8859-2", "override")

    def test_review_reports_without_writing_anything(self, tmp_path, monkeypatch):
        runner = self._project(tmp_path, monkeypatch)
        src = tmp_path / "solaris.txt"
        src.write_bytes(POLISH_PAGE.encode("cp1250"))

        result = runner.invoke(ingest_mod.app, [str(src), "--slug", "book", "--review"])
        assert result.exit_code == 0, result.output
        assert "encoding" in result.output and "cp1250" in result.output
        assert "language" in result.output
        assert not (tmp_path / "data" / "book" / "book").exists()

    def test_an_unreadable_file_fails_without_writing(self, tmp_path, monkeypatch):
        runner = self._project(tmp_path, monkeypatch)
        src = tmp_path / "broken.txt"
        src.write_bytes(bytes([0x81, 0x8D, 0x8F, 0x90, 0x9D]) * 60)

        result = runner.invoke(ingest_mod.app, [str(src), "--slug", "book"])
        assert result.exit_code != 0
        assert not (tmp_path / "data" / "book" / "book").exists()


class TestSourceStaging:
    """An imported book must not change when its input file does.

    Everything downstream reads the staged copy, so editing, moving or
    replacing the file it came from cannot alter a book already in the queue.
    """

    def _project(self, tmp_path, monkeypatch):
        (tmp_path / "justfile").write_text("", encoding="utf-8")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "pipeline.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(tmp_path))
        return CliRunner()

    def _ingest(self, tmp_path, monkeypatch, name="solaris.txt", text=None):
        runner = self._project(tmp_path, monkeypatch)
        src = tmp_path / "inbox" / name
        src.parent.mkdir(exist_ok=True)
        src.write_text(text if text is not None else POLISH_PAGE, encoding="utf-8")
        result = runner.invoke(ingest_mod.app, [str(src), "--slug", "book"])
        assert result.exit_code == 0, result.output
        meta = json.loads((tmp_path / "data" / "book" / "book" / "chapters.json")
                          .read_text(encoding="utf-8"))["meta"]
        return src, meta

    def test_the_input_is_copied_under_data_sources(self, tmp_path, monkeypatch):
        _src, meta = self._ingest(tmp_path, monkeypatch)
        staged = tmp_path / meta["source_file"]
        assert staged.exists()
        assert staged.read_text(encoding="utf-8") == POLISH_PAGE

    def test_source_file_points_at_the_copy_not_the_original(self, tmp_path, monkeypatch):
        src, meta = self._ingest(tmp_path, monkeypatch)
        assert meta["source_file"].startswith("data/sources/")
        assert meta["original_source"] == str(src)

    def test_editing_the_original_afterwards_changes_nothing(self, tmp_path, monkeypatch):
        src, meta = self._ingest(tmp_path, monkeypatch)
        src.write_text("Zupełnie inna książka.\n", encoding="utf-8")

        staged = tmp_path / meta["source_file"]
        assert staged.read_text(encoding="utf-8") == POLISH_PAGE

    def test_deleting_the_original_afterwards_leaves_the_book_readable(
            self, tmp_path, monkeypatch):
        src, meta = self._ingest(tmp_path, monkeypatch)
        src.unlink()
        assert (tmp_path / meta["source_file"]).exists()

    def test_the_hash_describes_the_staged_copy(self, tmp_path, monkeypatch):
        import hashlib
        _src, meta = self._ingest(tmp_path, monkeypatch)
        staged = tmp_path / meta["source_file"]
        assert meta["source_sha256"] == hashlib.sha256(staged.read_bytes()).hexdigest()

    def test_re_ingesting_replaces_the_copy(self, tmp_path, monkeypatch):
        runner = self._project(tmp_path, monkeypatch)
        src = tmp_path / "inbox" / "solaris.txt"
        src.parent.mkdir(exist_ok=True)
        src.write_text(POLISH_PAGE, encoding="utf-8")
        runner.invoke(ingest_mod.app, [str(src), "--slug", "book"])

        revised = POLISH_PAGE + "Nowy akapit dopisany później do tej samej książki.\n"
        src.write_text(revised, encoding="utf-8")
        assert runner.invoke(ingest_mod.app, [str(src), "--slug", "book"]).exit_code == 0

        meta = json.loads((tmp_path / "data" / "book" / "book" / "chapters.json")
                          .read_text(encoding="utf-8"))["meta"]
        assert (tmp_path / meta["source_file"]).read_text(encoding="utf-8") == revised

    def test_no_partial_file_is_left_behind(self, tmp_path, monkeypatch):
        _src, meta = self._ingest(tmp_path, monkeypatch)
        staged_dir = (tmp_path / meta["source_file"]).parent
        assert [p.name for p in staged_dir.iterdir()] == ["solaris.txt"]
