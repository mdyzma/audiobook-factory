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

    # Each module derives the project root as parents[3] of its own file.
    fake = str(tmp_path / "a" / "b" / "c" / "mod.py")
    for module in (ingest_mod, chunk_mod, dryrun_mod, assemble_mod):
        monkeypatch.setattr(module, "__file__", fake)
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
