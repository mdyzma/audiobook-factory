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


class TestRefusesStaleAudio:
    """A full set of readable files with the right names is not enough.

    Re-chunking after a text correction, or half-rendering under a different
    model, leaves exactly that. The completeness check above cannot see it;
    only the fingerprint can.
    """

    MODEL = {
        "id": "xtts-v2", "engine": "xtts", "environment": "narrator",
        "checkpoint": "c", "revision": "", "native_sample_rate": 24000,
        "char_limit": 224, "settings": {"temperature": 0.7}, "unsupported": [],
        "source": "default",
    }

    def _build(self, tmp_path, monkeypatch):
        from bookbinder.fingerprint import fragment_fingerprint, voice_revision

        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))

        book_json = root / "data" / "book" / "b" / "book.json"
        payload = json.loads(book_json.read_text(encoding="utf-8"))
        payload["model"] = self.MODEL
        book_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        # Fingerprint every fragment as a real render would have.
        revision = voice_revision(root, "v")
        path, rows = TestAssembleEndToEnd._rendered(root)
        for row in rows:
            row["voice"] = "v"
            row["fingerprint"] = fragment_fingerprint(
                text=row["text"], language=row["language"], model="xtts-v2",
                voice="v", voice_revision=revision,
                settings={"temperature": 0.7})
        TestAssembleEndToEnd._rewrite(path, rows)
        TestAssembleEndToEnd._rewrite(root / "data" / "book" / "b" / "chunks.jsonl", rows)

        from typer.testing import CliRunner
        return root, assemble, CliRunner()

    def test_a_fingerprinted_book_still_assembles(self, tmp_path, monkeypatch):
        root, assemble, runner = self._build(tmp_path, monkeypatch)
        assert runner.invoke(assemble.app, ["b"]).exit_code == 0

    def test_per_role_speed_matches_render_and_detects_changes(self, tmp_path, monkeypatch):
        from bookbinder.fingerprint import fragment_fingerprint, voice_revision
        root, assemble, runner = self._build(tmp_path, monkeypatch)
        book_json = root / "data/book/b/book.json"
        book = json.loads(book_json.read_text())
        path, rows = TestAssembleEndToEnd._rendered(root)
        roles = {row.get("role", "narrator") for row in rows}
        book["cast_settings"] = {role: {"speed": 0.88} for role in roles}
        book_json.write_text(json.dumps(book))
        for row in rows:
            row["fingerprint"] = fragment_fingerprint(
                text=row["text"], language=row["language"], model="xtts-v2",
                voice="v", voice_revision=voice_revision(root, "v"),
                settings={"temperature": 0.7, "speed": 0.88})
        TestAssembleEndToEnd._rewrite(path, rows)
        assert runner.invoke(assemble.app, ["b"]).exit_code == 0
        book["cast_settings"] = {role: {"speed": 0.95} for role in roles}
        book_json.write_text(json.dumps(book))
        assert runner.invoke(assemble.app, ["b"]).exit_code != 0

    def test_edited_text_blocks_export(self, tmp_path, monkeypatch):
        # A correction re-chunked without re-rendering: same ids, same files.
        root, assemble, runner = self._build(tmp_path, monkeypatch)
        plan = root / "data" / "book" / "b" / "chunks.jsonl"
        rows = [json.loads(l) for l in plan.read_text(encoding="utf-8").splitlines() if l.strip()]
        rows[1]["text"] = "Zupełnie inne zdanie niż to, które nagrano."
        TestAssembleEndToEnd._rewrite(plan, rows)

        result = runner.invoke(assemble.app, ["b"])
        assert result.exit_code != 0
        assert "different text" in result.output
        assert "ch001_0001" in result.output

    def test_a_changed_model_blocks_export(self, tmp_path, monkeypatch):
        root, assemble, runner = self._build(tmp_path, monkeypatch)
        book_json = root / "data" / "book" / "b" / "book.json"
        payload = json.loads(book_json.read_text(encoding="utf-8"))
        payload["model"]["id"] = "chatterbox-multilingual"
        book_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        result = runner.invoke(assemble.app, ["b"])
        assert result.exit_code != 0
        assert "different model" in result.output

    def test_changed_settings_block_export(self, tmp_path, monkeypatch):
        root, assemble, runner = self._build(tmp_path, monkeypatch)
        book_json = root / "data" / "book" / "b" / "book.json"
        payload = json.loads(book_json.read_text(encoding="utf-8"))
        payload["model"]["settings"] = {"temperature": 0.95}
        book_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        assert runner.invoke(assemble.app, ["b"]).exit_code != 0

    def test_a_re_cloned_voice_blocks_export(self, tmp_path, monkeypatch):
        root, assemble, runner = self._build(tmp_path, monkeypatch)
        voices = root / "data" / "voices" / "v"
        voices.mkdir(parents=True, exist_ok=True)
        (voices / "latents.pt").write_bytes(b"cloned again from new material")

        assert runner.invoke(assemble.app, ["b"]).exit_code != 0

    def test_audio_from_before_fingerprinting_is_let_through(self, tmp_path, monkeypatch):
        # Refusing every book already on disk would be its own failure.
        root, assemble, runner = self._build(tmp_path, monkeypatch)
        path, rows = TestAssembleEndToEnd._rendered(root)
        for row in rows:
            row["fingerprint"] = ""
        TestAssembleEndToEnd._rewrite(path, rows)

        assert runner.invoke(assemble.app, ["b"]).exit_code == 0


class TestExportIsPublishedAtomically:
    """A killed assembly used to leave a truncated file in data/out/.

    It plays, has a plausible length, and reads to the dashboard as a finished
    book, which is the one state a reader trusts without listening.
    """

    @pytest.mark.parametrize("fmt", ["m4b", "mp3", "wav"])
    def test_every_format_still_assembles(self, tmp_path, monkeypatch, fmt):
        # The staged file ends in `.part`, so ffmpeg can no longer guess the
        # container from the extension and each one has to be named.
        from typer.testing import CliRunner
        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))

        result = CliRunner().invoke(assemble.app, ["b", "--fmt", fmt])
        assert result.exit_code == 0, result.output
        out = root / "data" / "out" / f"b.{fmt}"
        assert out.exists() and out.stat().st_size > 0
        assert probe_duration(out) == pytest.approx(5.0, abs=0.3)

    def test_no_staged_file_is_left_behind(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))
        CliRunner().invoke(assemble.app, ["b"])

        leftovers = [p.name for p in (root / "data" / "out").iterdir()
                     if p.name.startswith(".") or p.name.endswith(".part")]
        assert leftovers == []

    def test_a_failed_assembly_leaves_the_previous_export_intact(
            self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))
        runner = CliRunner()
        assert runner.invoke(assemble.app, ["b"]).exit_code == 0

        good = (root / "data" / "out" / "b.m4b").read_bytes()

        # ffmpeg fails this time; the file already there must survive.
        import bookbinder.assemble as mod
        def boom(*_a, **_k):
            raise subprocess.CalledProcessError(1, "ffmpeg")
        monkeypatch.setattr(mod.subprocess, "run", boom)

        runner.invoke(assemble.app, ["b"])
        assert (root / "data" / "out" / "b.m4b").read_bytes() == good


class TestManifestIsPublishedAtomically:
    def test_a_failed_write_leaves_the_previous_manifest(self, tmp_path):
        from bookbinder.manifest import publish

        target = tmp_path / "book.json"
        target.write_text("previous", encoding="utf-8")

        def boom(_staged):
            raise RuntimeError("interrupted")

        with pytest.raises(RuntimeError):
            publish(target, boom)
        assert target.read_text(encoding="utf-8") == "previous"
        assert list(tmp_path.iterdir()) == [target]

    def test_a_successful_write_replaces_it(self, tmp_path):
        from bookbinder.manifest import publish_text

        target = tmp_path / "book.json"
        target.write_text("previous", encoding="utf-8")
        publish_text(target, "current")
        assert target.read_text(encoding="utf-8") == "current"


class TestSampleRateFollowsTheFragments:
    """The pauses are generated here and the concat demuxer needs one rate.

    24 kHz was a safe assumption only while XTTS was the only engine. Silence
    at the configured output rate against fragments at the engine's own rate is
    a click at every paragraph break, or a concat that will not run at all.
    """

    def test_the_report_says_what_rate_the_fragments_are(self, tmp_path, monkeypatch):
        from bookbinder.assemble import rendered_sample_rate

        audio = tmp_path / "audio"
        audio.mkdir()
        (audio / "report.json").write_text(
            json.dumps({"slug": "b", "sample_rate": 22050}), encoding="utf-8")
        assert rendered_sample_rate(audio, 24000) == 22050

    def test_without_a_report_it_probes_a_fragment(self, tmp_path):
        from bookbinder.assemble import rendered_sample_rate

        audio = tmp_path / "audio"
        audio.mkdir()
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=200:duration=0.2:sample_rate=16000",
             "-ac", "1", "-c:a", "pcm_s16le", str(audio / "ch001_0000.wav")],
            check=True)
        assert rendered_sample_rate(audio, 24000) == 16000

    def test_with_neither_it_falls_back(self, tmp_path):
        from bookbinder.assemble import rendered_sample_rate

        audio = tmp_path / "audio"
        audio.mkdir()
        assert rendered_sample_rate(audio, 24000) == 24000

    def test_a_malformed_report_does_not_stop_assembly(self, tmp_path):
        from bookbinder.assemble import rendered_sample_rate

        audio = tmp_path / "audio"
        audio.mkdir()
        (audio / "report.json").write_text("{not json", encoding="utf-8")
        assert rendered_sample_rate(audio, 24000) == 24000

    def test_a_book_rendered_at_another_rate_still_assembles(
            self, tmp_path, monkeypatch):
        # What a second backend will look like. The finished file is at the
        # configured rate; the pauses had to be made at the fragments' rate.
        from typer.testing import CliRunner
        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))

        audio_dir = root / "data" / "audio" / "b"
        for wav in sorted(audio_dir.glob("*.wav")):
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
                 "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le",
                 str(wav.with_suffix(".tmp.wav"))], check=True)
            wav.with_suffix(".tmp.wav").replace(wav)
        (audio_dir / "report.json").write_text(
            json.dumps({"slug": "b", "sample_rate": 22050}), encoding="utf-8")

        result = CliRunner().invoke(assemble.app, ["b"])
        assert result.exit_code == 0, result.output
        assert probe_duration(root / "data" / "out" / "b.m4b") == pytest.approx(5.0, abs=0.3)


class TestAWarningNamesWhereTheFilesAre:
    """A stage may be running against a data root that is not the project's.

    Under the catalog every run gets its own, so a message that hard-codes
    `data/audio/<slug>/` sends the reader to a directory the file is not in.
    """

    def test_the_dry_run_warning_names_the_directory_it_read(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from bookbinder.manifest import mark_dry_run

        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))
        audio = root / "data" / "audio" / "b"
        mark_dry_run(audio, "b", 2)

        result = CliRunner().invoke(assemble.app, ["b"])
        assert "is dry-run silence" in result.output
        assert str(audio) in result.output


class TestWhatAPlayerShows:
    """The export is the only thing that survives the pipeline.

    Everything else lives under `data/` and is read by this program alone. What
    a listener sees on their phone is these tags and this picture.
    """

    def _picture(self, path):
        import subprocess

        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "color=c=navy:s=120x120:d=1", "-frames:v", "1", str(path)],
            check=True)
        return path

    def _tags(self, path):
        import json
        import subprocess

        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(path)], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_the_narrator_is_named(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))
        assert CliRunner().invoke(assemble.app, ["b", "--fmt", "m4b"]).exit_code == 0

        tags = self._tags(root / "data" / "out" / "b.m4b")["format"]["tags"]
        # `composer` is where audiobook players look for the narrator.
        assert tags.get("composer")

    def test_a_cast_is_named_in_full(self):
        # A book read by two people is read by two people.
        from bookbinder import assemble

        class Book:
            voice = ""
            cast = {"narrator": "michal", "dialogue": "ala"}

        assert assemble.narrator_tag(Book()) == "ala, michal"

    def test_one_voice_is_named_alone(self):
        from bookbinder import assemble

        class Book:
            voice = "michal"
            cast = {"narrator": "michal"}

        assert assemble.narrator_tag(Book()) == "michal"

    def test_a_book_with_nobody_named_gets_no_tag(self):
        from bookbinder import assemble

        class Book:
            voice = ""
            cast = {}

        assert assemble.narrator_tag(Book()) == ""

    def test_the_cover_reaches_the_file(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))
        self._picture(root / "data" / "book" / "b" / "cover.png")

        assert CliRunner().invoke(assemble.app, ["b", "--fmt", "m4b"]).exit_code == 0
        streams = self._tags(root / "data" / "out" / "b.m4b")["streams"]
        pictures = [s for s in streams
                    if s.get("disposition", {}).get("attached_pic")]
        assert len(pictures) == 1

    def test_adding_a_cover_does_not_change_the_audio(self, tmp_path, monkeypatch):
        # A third input, an explicit stream mapping and a video codec are three
        # ways to end up exporting something other than the book. The duration
        # is the cheapest thing that would notice.
        from typer.testing import CliRunner

        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))
        without = CliRunner().invoke(assemble.app, ["b", "--fmt", "m4b"])
        assert without.exit_code == 0
        plain = float(self._tags(root / "data" / "out" / "b.m4b")["format"]["duration"])

        self._picture(root / "data" / "book" / "b" / "cover.png")
        assert CliRunner().invoke(assemble.app, ["b", "--fmt", "m4b"]).exit_code == 0
        illustrated = float(
            self._tags(root / "data" / "out" / "b.m4b")["format"]["duration"])

        assert illustrated == pytest.approx(plain, abs=0.2)

    def test_a_wav_export_has_nowhere_to_put_a_picture(self, tmp_path, monkeypatch):
        # And asking ffmpeg to try produces a file that is not a wav.
        from typer.testing import CliRunner

        root, assemble = TestAssembleEndToEnd()._build(tmp_path, monkeypatch)
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(root))
        self._picture(root / "data" / "book" / "b" / "cover.png")

        assert CliRunner().invoke(assemble.app, ["b", "--fmt", "wav"]).exit_code == 0
        assert (root / "data" / "out" / "b.wav").is_file()

    def test_a_supplied_cover_is_used(self, tmp_path):
        # For a plain text book that never had one.
        from bookbinder.assemble import cover_for

        book_dir = tmp_path / "book"
        assert cover_for(book_dir) is None
        self._picture(book_dir / "cover.jpg")
        found = cover_for(book_dir)
        assert found is not None and found.name == "cover.jpg"
