"""The whole structure, end to end, with silence instead of speech.

This is what the dry-run renderer exists for: ingest, chunk, render, assemble,
with no model weights and no torch. It catches the failures that are actually
common - a chapter mark in the wrong place, pauses that do not add up, a role
mapped to nothing - in under a second.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

import bookbinder.assemble as assemble_mod
import bookbinder.chunk as chunk_mod
import bookbinder.dryrun as dryrun_mod
import bookbinder.ingest as ingest_mod
from bookbinder.manifest import read_book, read_chunks

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)

BOOK = """# Przybysz

Zszedłem po metalowej drabince do wnętrza kabiny. Było ciasno.

Kelvin: — Nie wiem, po co tu przyleciałem.

„Ocean nie odpowiada", pomyślałem.

# Sołaris

Nie szukamy nikogo oprócz ludzi. Potrzeba nam luster.
"""

CAST = """
roles:
  narrator:
    voice: narrator_voice
  dialogue:
    voice: dialogue_voice
  kelvin:
    voice: kelvin_voice
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A miniature project tree, with each module's root pointed at it."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "cast.yml").write_text(CAST, encoding="utf-8")
    (tmp_path / "config" / "pipeline.toml").write_text(
        '[book]\nheading_pause_ms = 900\nparagraph_pause_ms = 350\n'
        'sentence_pause_ms = 120\n\n[chunk]\nmax_chars = 0\nmin_chars = 40\n\n'
        '[assemble]\nformat = "m4b"\nbitrate = "64k"\n'
        'sample_rate = 24000\nchannels = 1\n', encoding="utf-8")
    books = tmp_path / "data" / "raw" / "books"
    books.mkdir(parents=True)
    (books / "solaris.txt").write_text(BOOK, encoding="utf-8")

    # Point every module at this tree through the same environment variable the
    # containers use, which exercises the real discovery rather than a stand-in.
    monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(tmp_path))
    return tmp_path


def run(app, args):
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    return result


class TestStructuralPipeline:
    def test_ingest_to_audiobook_without_any_model(self, project):
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])
        run(dryrun_mod.app, ["solaris"])
        run(assemble_mod.app, ["solaris"])

        out = project / "data" / "out" / "solaris.m4b"
        assert out.exists()

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(out)],
            capture_output=True, text=True, check=True)
        titles = [c["tags"]["title"] for c in json.loads(probe.stdout)["chapters"]]
        assert titles == ["Przybysz", "Sołaris"]

    def test_cast_is_recorded_and_roles_assigned(self, project):
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])

        book = read_book(project / "data" / "book" / "solaris" / "book.json")
        assert book.cast["kelvin"] == "kelvin_voice"
        assert book.cast["narrator"] == "narrator_voice"

        chunks = list(read_chunks(project / "data" / "book" / "solaris" / "chunks.jsonl"))
        roles = {c.role for c in chunks}
        assert "kelvin" in roles
        assert any(c.is_dialogue for c in chunks)
        # Headings are always narration.
        assert all(c.role == "narrator" for c in chunks if c.kind == "heading")

    def test_single_voice_mode_ignores_the_cast(self, project):
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris", "--voice", "solo", "--single-voice"])
        book = read_book(project / "data" / "book" / "solaris" / "book.json")
        assert set(book.cast.values()) == {"solo"}

    def test_source_hash_recorded_and_stable(self, project):
        for _ in range(2):
            run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                                 "--slug", "solaris", "--language", "pl"])
            run(chunk_mod.app, ["solaris"])
        book = read_book(project / "data" / "book" / "solaris" / "book.json")
        assert len(book.source_sha256) == 64

    def test_chunking_is_idempotent(self, project):
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        chunks_path = project / "data" / "book" / "solaris" / "chunks.jsonl"
        run(chunk_mod.app, ["solaris"])
        first = chunks_path.read_text(encoding="utf-8")
        run(chunk_mod.app, ["solaris"])
        assert chunks_path.read_text(encoding="utf-8") == first

    def test_a_role_correction_survives_re_chunking(self, project):
        """The property the whole override mechanism exists for.

        Chunk ids encode position and are renumbered on every re-chunk, so a
        correction keyed to one would be stranded. source_ref names the
        paragraph in the source and outlives the manifest.
        """
        from bookbinder import overrides

        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])

        book_dir = project / "data" / "book" / "solaris"
        narration = next(c for c in read_chunks(book_dir / "chunks.jsonl")
                         if c.kind == "paragraph" and c.role == "narrator")
        overrides.set_role(book_dir, narration.source_ref, "kelvin")

        # Re-chunk twice: ids change, the correction must not.
        run(chunk_mod.app, ["solaris"])
        run(chunk_mod.app, ["solaris"])

        corrected = [c for c in read_chunks(book_dir / "chunks.jsonl")
                     if c.source_ref == narration.source_ref]
        assert corrected and all(c.role == "kelvin" for c in corrected)
        assert all(c.is_dialogue for c in corrected)

    def test_a_correction_reaches_the_recorded_cast(self, project):
        from bookbinder import overrides

        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])
        book_dir = project / "data" / "book" / "solaris"
        narration = next(c for c in read_chunks(book_dir / "chunks.jsonl")
                         if c.kind == "paragraph")
        overrides.set_role(book_dir, narration.source_ref, "kelvin")
        run(chunk_mod.app, ["solaris"])

        # An unknown role still resolves, falling back to the narrator's voice.
        assert "kelvin" in read_book(book_dir / "book.json").cast

    def test_clearing_a_correction_restores_detection(self, project):
        from bookbinder import overrides

        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])
        book_dir = project / "data" / "book" / "solaris"
        original = next(c for c in read_chunks(book_dir / "chunks.jsonl")
                        if c.kind == "paragraph")

        overrides.set_role(book_dir, original.source_ref, "kelvin")
        run(chunk_mod.app, ["solaris"])
        overrides.set_role(book_dir, original.source_ref, "")
        run(chunk_mod.app, ["solaris"])

        restored = next(c for c in read_chunks(book_dir / "chunks.jsonl")
                        if c.source_ref == original.source_ref)
        assert restored.role == original.role

    def test_render_report_describes_the_run(self, project):
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])
        run(dryrun_mod.app, ["solaris"])

        report = json.loads(
            (project / "data" / "audio" / "solaris" / "report.json").read_text(encoding="utf-8"))
        assert report["dry_run"] is True
        assert report["ok"] is True
        assert report["chunks_rendered"] == report["chunks_total"]
        assert report["failures"] == []
        assert report["audio_sec"] > 0

    def test_dry_run_leaves_a_finished_progress_file(self, project):
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])
        run(dryrun_mod.app, ["solaris"])

        progress = json.loads(
            (project / "data" / "audio" / "solaris" / "progress.json").read_text(encoding="utf-8"))
        assert progress["running"] is False
        assert progress["percent"] == 100.0
        assert progress["chunks_done"] == progress["chunks_total"]
        assert progress["dry_run"] is True
        # Nothing may be left behind mid-write.
        assert not list((project / "data" / "audio" / "solaris").glob("*.tmp"))

    def test_strict_dry_run_fails_on_an_uncloned_voice(self, project):
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])
        # No voice profiles exist in this tree, so --strict must object.
        result = CliRunner().invoke(dryrun_mod.app, ["solaris", "--strict"])
        assert result.exit_code == 1
        report = json.loads(
            (project / "data" / "audio" / "solaris" / "report.json").read_text(encoding="utf-8"))
        assert report["ok"] is False
        assert any("kelvin_voice" in f["error"] for f in report["failures"])
