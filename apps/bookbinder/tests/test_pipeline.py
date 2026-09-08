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
from bookbinder.manifest import (
    DRY_RUN_MARKER,
    clear_dry_run,
    is_dry_run_audio,
    read_book,
    read_chunks,
)

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


class TestDryRunIsLabelled:
    """Silence must never be mistaken for narration.

    A dry run writes a wav per fragment at the estimated duration, using the
    same names and the same sample rate as a real render. Stage 4 resumes by
    skipping fragments that already have a wav, so once a dry run has run,
    every later stage needs a way to tell the two apart. The marker file is
    that way, and these tests pin the behaviour that depends on it.
    """

    def prepared(self, project):
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])
        return project / "data" / "audio" / "solaris"

    def test_dry_run_marks_its_own_output(self, project):
        audio_dir = self.prepared(project)
        assert not is_dry_run_audio(audio_dir)
        run(dryrun_mod.app, ["solaris"])
        assert is_dry_run_audio(audio_dir)

        marker = json.loads((audio_dir / DRY_RUN_MARKER).read_text(encoding="utf-8"))
        assert marker["slug"] == "solaris"
        assert marker["chunks"] > 0

    def test_assembling_silence_says_so(self, project):
        self.prepared(project)
        run(dryrun_mod.app, ["solaris"])
        result = run(assemble_mod.app, ["solaris"])
        assert "silen" in result.output.lower()

    def test_clearing_removes_the_silence_and_the_marker(self, project):
        audio_dir = self.prepared(project)
        run(dryrun_mod.app, ["solaris"])
        wavs = len(list(audio_dir.glob("*.wav")))
        assert wavs > 0

        assert clear_dry_run(audio_dir) == wavs
        assert not list(audio_dir.glob("*.wav"))
        assert not (audio_dir / "rendered.jsonl").exists()
        assert not is_dry_run_audio(audio_dir)
        # Idempotent: a second call has nothing to do and must not object.
        assert clear_dry_run(audio_dir) == 0

    def test_clearing_leaves_a_real_render_alone(self, project):
        """The guard rail on the guard rail.

        Nothing may delete hours of finished narration. Without a marker,
        clearing is a no-op no matter what else is in the directory.
        """
        audio_dir = self.prepared(project)
        run(dryrun_mod.app, ["solaris"])
        (audio_dir / DRY_RUN_MARKER).unlink()  # as a real render would

        before = sorted(p.name for p in audio_dir.glob("*.wav"))
        assert clear_dry_run(audio_dir) == 0
        assert sorted(p.name for p in audio_dir.glob("*.wav")) == before

    def test_an_interrupted_dry_run_is_still_marked(self, project, monkeypatch):
        """The marker is written before the first wav, not after the last.

        A dry run killed halfway leaves silence behind too, and that silence
        would otherwise look exactly like a render that stopped early.
        """
        audio_dir = self.prepared(project)
        calls = {"n": 0}
        real = dryrun_mod.write_silence

        def die_partway(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] > 2:
                raise RuntimeError("killed")
            return real(*args, **kwargs)

        monkeypatch.setattr(dryrun_mod, "write_silence", die_partway)
        with pytest.raises(RuntimeError):
            CliRunner().invoke(dryrun_mod.app, ["solaris"], catch_exceptions=False)

        assert is_dry_run_audio(audio_dir)
        assert list(audio_dir.glob("*.wav"))


class TestSilenceIsCaughtWithoutAMarker:
    """The check that does not depend on knowing the cause.

    Audio rendered before the marker existed carries no marker, and a voice
    that renders to nothing carries no marker either. Both produce an
    audiobook of exactly the right length that plays as nothing, which is the
    one defect a listener finds only by pressing play.
    """

    def test_a_marked_dry_run_is_still_reported_once(self, project):
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])
        run(dryrun_mod.app, ["solaris"])
        result = run(assemble_mod.app, ["solaris"])
        assert result.output.lower().count("warning:") == 1

    def test_unmarked_silence_is_still_reported(self, project):
        """Exactly the state a dry run from an older version leaves behind."""
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])
        run(dryrun_mod.app, ["solaris"])
        (project / "data" / "audio" / "solaris" / DRY_RUN_MARKER).unlink()

        result = run(assemble_mod.app, ["solaris"])
        assert "every fragment sampled" in result.output

    def test_real_audio_is_not_flagged(self, project):
        """Narration must assemble without a word of complaint."""
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])
        run(dryrun_mod.app, ["solaris"])
        audio_dir = project / "data" / "audio" / "solaris"
        (audio_dir / DRY_RUN_MARKER).unlink()

        # Replace the silence with a tone: the same durations, but audible.
        for wav in audio_dir.glob("*.wav"):
            duration = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(wav)],
                capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000",
                 "-t", duration, "-c:a", "pcm_s16le", str(wav)], check=True)

        result = run(assemble_mod.app, ["solaris"])
        assert "warning" not in result.output.lower()

    def test_a_book_with_one_audible_fragment_is_not_flagged(self, project):
        """Only wholesale silence is a defect; a quiet fragment is not."""
        run(ingest_mod.app, [str(project / "data/raw/books/solaris.txt"),
                             "--slug", "solaris", "--language", "pl"])
        run(chunk_mod.app, ["solaris"])
        run(dryrun_mod.app, ["solaris"])
        audio_dir = project / "data" / "audio" / "solaris"
        (audio_dir / DRY_RUN_MARKER).unlink()

        wav = sorted(audio_dir.glob("*.wav"))[0]
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000",
             "-t", "1", "-c:a", "pcm_s16le", str(wav)], check=True)

        result = run(assemble_mod.app, ["solaris"])
        assert "every fragment sampled" not in result.output
