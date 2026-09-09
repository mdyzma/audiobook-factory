"""Assembly turns fragments into the deliverable.

These run ffmpeg for real against generated tones, because the failure mode
worth catching is a chapter mark landing at the wrong timestamp, which no
amount of mocking would reveal.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from bookbinder.assemble import format_timestamp, make_silence

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)


def probe_duration(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


class TestFormatTimestamp:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "00:00:00.000"),
        (61.5, "00:01:01.500"),
        (3661.25, "01:01:01.250"),
        (36000, "10:00:00.000"),
    ])
    def test_formats_hms(self, seconds, expected):
        assert format_timestamp(seconds) == expected


class TestMakeSilence:
    def test_generates_requested_duration(self, tmp_path):
        path = tmp_path / "sil.wav"
        make_silence(path, 350, 24000, 1)
        assert path.exists()
        assert probe_duration(path) == pytest.approx(0.35, abs=0.02)

    def test_respects_sample_rate_and_channels(self, tmp_path):
        path = tmp_path / "sil.wav"
        make_silence(path, 500, 22050, 2)
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        )
        assert out.stdout.strip() == "22050,2"


class TestAssembleEndToEnd:
    """Drive the real CLI over synthetic audio and read the result back."""

    def _build(self, tmp_path, monkeypatch):
        import bookbinder.assemble as assemble

        root = tmp_path
        (root / "config").mkdir()
        (root / "config" / "pipeline.toml").write_text(
            '[assemble]\nformat = "m4b"\nbitrate = "64k"\n'
            "sample_rate = 24000\nchannels = 1\n", encoding="utf-8")

        book_dir = root / "data" / "book" / "b"
        audio_dir = root / "data" / "audio" / "b"
        audio_dir.mkdir(parents=True)
        book_dir.mkdir(parents=True)

        book_dir.joinpath("book.json").write_text(json.dumps({
            "slug": "b", "title": "Sołaris", "author": "Lem", "language": "pl",
            "source_file": "", "voice": "v", "chapters": [], "chunk_count": 3,
            "est_hours": 0.0}, ensure_ascii=False), encoding="utf-8")

        # Two chapters: 1.0 s heading + 2.0 s body, then 1.0 s heading.
        spec = [("ch001_0000", 1, "Przybysz", 0, 1.0, 500),
                ("ch001_0001", 1, "Przybysz", 1, 2.0, 500),
                ("ch002_0002", 2, "Sołaris", 2, 1.0, 0)]
        lines = []
        for cid, ch, title, order, dur, pause in spec:
            wav = audio_dir / f"{cid}.wav"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", f"sine=frequency=200:duration={dur}:sample_rate=24000",
                 "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True)
            lines.append(json.dumps({
                "id": cid, "chapter_index": ch, "chapter_title": title, "order": order,
                "text": "x", "kind": "paragraph", "language": "pl",
                "pause_after_ms": pause, "chars": 1, "est_seconds": dur,
                "source_ref": "", "audio_path": str(wav), "duration_sec": dur}))
        audio_dir.joinpath("rendered.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        # The chunk plan assembly checks itself against.
        book_dir.joinpath("chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        return root, assemble

    @staticmethod
    def _rendered(root):
        path = root / "data" / "audio" / "b" / "rendered.jsonl"
        return path, [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    @staticmethod
    def _rewrite(path, rows):
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                        encoding="utf-8")

    def test_produces_m4b_with_correct_chapters(self, tmp_path, monkeypatch):
        root, assemble = self._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))

        from typer.testing import CliRunner
        result = CliRunner().invoke(assemble.app, ["b"])
        assert result.exit_code == 0, result.output

        out = root / "data" / "out" / "b.m4b"
        assert out.exists()

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(out)],
            capture_output=True, text=True, check=True)
        chapters = json.loads(probe.stdout)["chapters"]
        assert [c["tags"]["title"] for c in chapters] == ["Przybysz", "Sołaris"]

        # Chapter 2 starts after 1.0 + 0.5 + 2.0 + 0.5 seconds of chapter 1.
        assert float(chapters[1]["start_time"]) == pytest.approx(4.0, abs=0.15)

    def test_total_duration_includes_pauses(self, tmp_path, monkeypatch):
        root, assemble = self._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))
        from typer.testing import CliRunner
        assert CliRunner().invoke(assemble.app, ["b"]).exit_code == 0
        # 1.0 + 0.5 + 2.0 + 0.5 + 1.0 = 5.0 s
        assert probe_duration(root / "data" / "out" / "b.m4b") == pytest.approx(5.0, abs=0.2)


class TestRefusesIncompleteBooks:
    """A short audiobook is worse than a failed command.

    Every case here used to produce a normal-looking file: shorter than the
    book, with chapter marks at the wrong timestamps, and the only clue a line
    on stderr that scrolls past during a twenty-hour render.
    """

    def _run(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))
        return root, assemble, CliRunner()

    def test_deleted_fragment_blocks_export(self, tmp_path, monkeypatch):
        root, assemble, runner = self._run(tmp_path, monkeypatch)
        (root / "data" / "audio" / "b" / "ch001_0001.wav").unlink()

        result = runner.invoke(assemble.app, ["b"])
        assert result.exit_code != 0
        assert "no audio file on disk" in result.output
        assert "ch001_0001" in result.output
        assert not (root / "data" / "out" / "b.m4b").exists()

    def test_unrendered_fragment_blocks_export(self, tmp_path, monkeypatch):
        # A render that failed partway leaves exactly this: the manifest holds
        # only what succeeded, and every file it names is present.
        root, assemble, runner = self._run(tmp_path, monkeypatch)
        path, rows = TestAssembleEndToEnd._rendered(root)
        TestAssembleEndToEnd._rewrite(path, [r for r in rows if r["id"] != "ch001_0001"])

        result = runner.invoke(assemble.app, ["b"])
        assert result.exit_code != 0
        assert "never rendered" in result.output
        assert "ch001_0001" in result.output
        assert not (root / "data" / "out" / "b.m4b").exists()

    def test_missing_audio_path_blocks_export(self, tmp_path, monkeypatch):
        root, assemble, runner = self._run(tmp_path, monkeypatch)
        path, rows = TestAssembleEndToEnd._rendered(root)
        rows[1]["audio_path"] = ""
        TestAssembleEndToEnd._rewrite(path, rows)

        result = runner.invoke(assemble.app, ["b"])
        assert result.exit_code != 0
        assert "no audio file on disk" in result.output

    def test_duplicated_fragment_blocks_export(self, tmp_path, monkeypatch):
        root, assemble, runner = self._run(tmp_path, monkeypatch)
        path, rows = TestAssembleEndToEnd._rendered(root)
        TestAssembleEndToEnd._rewrite(path, rows + [rows[1]])

        result = runner.invoke(assemble.app, ["b"])
        assert result.exit_code != 0
        assert "more than once" in result.output

    def test_fragment_outside_the_plan_blocks_export(self, tmp_path, monkeypatch):
        # Re-chunking after a render renames fragments. Assembling the old audio
        # against the new plan would narrate the previous revision of the book.
        root, assemble, runner = self._run(tmp_path, monkeypatch)
        plan = root / "data" / "book" / "b" / "chunks.jsonl"
        rows = [json.loads(l) for l in plan.read_text(encoding="utf-8").splitlines() if l.strip()]
        rows[2]["id"] = "ch002_0099"
        TestAssembleEndToEnd._rewrite(plan, rows)

        result = runner.invoke(assemble.app, ["b"])
        assert result.exit_code != 0
        assert "not in the chunk plan" in result.output

    def test_complete_book_still_assembles(self, tmp_path, monkeypatch):
        # The guard must not refuse a book that is genuinely finished.
        root, assemble, runner = self._run(tmp_path, monkeypatch)
        assert runner.invoke(assemble.app, ["b"]).exit_code == 0
        assert (root / "data" / "out" / "b.m4b").exists()


class TestEscaping:
    """Paths and tag values reach ffmpeg through files it parses itself.

    The concat list quotes filenames and ffmetadata treats a newline as the end
    of an entry, so neither can be built by plain interpolation. Titles come
    from the ebook, which is not ours.
    """

    def test_concat_line_escapes_an_apostrophe(self):
        from bookbinder.assemble import concat_line
        from pathlib import Path

        assert concat_line(Path("/tmp/it's here.wav")) == r"file '/tmp/it'\''s here.wav'"

    def test_concat_line_leaves_ordinary_paths_alone(self):
        from bookbinder.assemble import concat_line
        from pathlib import Path

        assert concat_line(Path("/data/audio/b/ch001_0000.wav")) == \
            "file '/data/audio/b/ch001_0000.wav'"

    @pytest.mark.parametrize("raw,expected", [
        ("Ocean", "Ocean"),
        ("a=b", r"a\=b"),
        ("a;b", r"a\;b"),
        ("a#b", r"a\#b"),
        ("a\\b", r"a\\b"),
        ("Ocean\nartist=INJECTED", "Ocean\\\nartist\\=INJECTED"),
    ])
    def test_metadata_value_escapes_ffmetadata_syntax(self, raw, expected):
        from bookbinder.assemble import metadata_value

        assert metadata_value(raw) == expected

    def test_a_title_carrying_a_newline_cannot_add_tags(self, tmp_path, monkeypatch):
        # An EPUB is untrusted input. Without escaping, everything after the
        # newline became real tags on the finished audiobook.
        from typer.testing import CliRunner
        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))

        book_json = root / "data" / "book" / "b" / "book.json"
        payload = json.loads(book_json.read_text(encoding="utf-8"))
        payload["title"] = "Sołaris\nartist=INJECTED"
        book_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        assert CliRunner().invoke(assemble.app, ["b"]).exit_code == 0

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=artist",
             "-of", "default=nw=1", str(root / "data" / "out" / "b.m4b")],
            capture_output=True, text=True, check=True)
        assert "INJECTED" not in probe.stdout
        assert "Lem" in probe.stdout
