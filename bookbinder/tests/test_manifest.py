"""The manifest is the contract between two environments that cannot import
each other. If it round-trips wrong, the narrator and the assembler disagree."""

from __future__ import annotations

import json

from bookbinder.manifest import (
    XTTS_CHAR_LIMITS,
    BookManifest,
    Chunk,
    char_limit,
    read_book,
    read_chunks,
)


class TestChunk:
    def test_derives_char_count(self):
        assert Chunk(id="a", chapter_index=1, chapter_title="T", order=0, text="abcde").chars == 5

    def test_estimates_duration_from_length(self):
        c = Chunk(id="a", chapter_index=1, chapter_title="T", order=0, text="x" * 150)
        assert c.est_seconds == 10.0

    def test_explicit_estimate_is_not_overwritten(self):
        c = Chunk(id="a", chapter_index=1, chapter_title="T", order=0, text="x", est_seconds=9.0)
        assert c.est_seconds == 9.0


class TestCharLimit:
    def test_known_languages(self):
        assert char_limit("pl") == 224
        assert char_limit("ja") == 71

    def test_unknown_language_defaults(self):
        assert char_limit("xx") == 250

    def test_every_limit_is_positive(self):
        assert all(v > 0 for v in XTTS_CHAR_LIMITS.values())


class TestRoundTrip:
    def _manifest(self) -> BookManifest:
        m = BookManifest(slug="s", title="Sołaris", author="Lem", language="pl")
        m.chunks = [
            Chunk(id="ch001_0000", chapter_index=1, chapter_title="Przybysz", order=0,
                  text="Przybysz", kind="heading", pause_after_ms=900),
            Chunk(id="ch001_0001", chapter_index=1, chapter_title="Przybysz", order=1,
                  text="Ocean falował pod stacją."),
        ]
        m.chapters = [{"index": 1, "title": "Przybysz", "first_chunk": 0, "chunk_count": 2}]
        return m

    def test_chunks_survive_write_and_read(self, tmp_path):
        m = self._manifest()
        _meta_path, chunks_path = m.write(tmp_path)
        back = list(read_chunks(chunks_path))
        assert [c.id for c in back] == [c.id for c in m.chunks]
        assert back[0].pause_after_ms == 900
        assert back[0].kind == "heading"

    def test_unicode_is_not_escaped_on_disk(self, tmp_path):
        m = self._manifest()
        _meta, chunks_path = m.write(tmp_path)
        assert "Sołaris" in (tmp_path / "book.json").read_text(encoding="utf-8")
        assert "falował" in chunks_path.read_text(encoding="utf-8")

    def test_book_metadata_round_trips(self, tmp_path):
        m = self._manifest()
        meta_path, _ = m.write(tmp_path)
        meta = read_book(meta_path)
        assert meta["title"] == "Sołaris"
        assert meta["chunk_count"] == 2
        assert meta["language"] == "pl"

    def test_estimated_hours_sums_chunks(self, tmp_path):
        m = BookManifest(slug="s", title="T")
        m.chunks = [Chunk(id=f"c{i}", chapter_index=1, chapter_title="T", order=i,
                          text="x" * 150) for i in range(360)]
        assert m.est_hours == 1.0

    def test_empty_manifest_writes_cleanly(self, tmp_path):
        meta_path, chunks_path = BookManifest(slug="s", title="T").write(tmp_path)
        assert json.loads(meta_path.read_text())["chunk_count"] == 0
        assert list(read_chunks(chunks_path)) == []
