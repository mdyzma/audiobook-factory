"""Reading the pipeline's output.

The dashboard turns URL segments into filesystem paths, so the rejection tests
below are the important ones.
"""

from __future__ import annotations

import json

import pytest

from studio import data
from studio.data import UnsafeName, check_name


class TestNameSafety:
    @pytest.mark.parametrize("bad", [
        "../etc", "a/b", "", ".", "..", "/absolute", "x" * 200, "a\x00b", "a b",
    ])
    def test_rejects_anything_that_could_escape(self, bad):
        with pytest.raises(UnsafeName):
            check_name(bad)

    @pytest.mark.parametrize("good", ["michal", "ch001_0000", "solaris-demo", "a.b_c-1"])
    def test_accepts_ordinary_names(self, good):
        assert check_name(good) == good


class TestVoices:
    def test_lists_voices_with_their_assets(self, project):
        voices = data.list_voices(project)
        assert [v.name for v in voices] == ["michal"]
        v = voices[0]
        assert v.segment_count == 18
        assert v.has_audition and v.has_dataset
        assert not v.has_latents

    def test_missing_voice(self, project):
        assert data.get_voice(project, "absent") is None

    def test_no_voices_directory(self, tmp_path):
        assert data.list_voices(tmp_path) == []


class TestBooks:
    def test_reads_metadata_and_cast(self, project):
        book = data.get_book(project, "solaris")
        assert book is not None
        assert book.title == "Sołaris"
        assert book.cast["narrator"] == "michal"
        assert book.chapter_count == 1

    def test_state_is_prepared_before_any_render(self, project):
        book = data.get_book(project, "solaris")
        assert book is not None and book.state == "prepared"

    def test_state_is_done_once_an_output_exists(self, project):
        (project / "data" / "out" / "solaris.m4b").write_bytes(b"x")
        book = data.get_book(project, "solaris")
        assert book is not None and book.state == "done"

    def test_state_is_rendering_while_progress_is_live(self, project):
        from datetime import datetime, timezone
        (project / "data" / "audio" / "solaris" / "progress.json").write_text(json.dumps({
            "slug": "solaris", "running": True, "chunks_total": 10, "chunks_done": 3,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }), encoding="utf-8")
        book = data.get_book(project, "solaris")
        assert book is not None
        assert book.state == "rendering"
        assert book.percent == 30.0

    def test_state_is_stale_when_progress_stopped_moving(self, project):
        # A killed render leaves running=true forever; the age is the giveaway.
        (project / "data" / "audio" / "solaris" / "progress.json").write_text(json.dumps({
            "slug": "solaris", "running": True, "chunks_total": 10, "chunks_done": 3,
            "updated_at": "2020-01-01T00:00:00+00:00",
        }), encoding="utf-8")
        book = data.get_book(project, "solaris")
        assert book is not None and book.state == "stale"

    def test_state_is_failed_when_the_report_says_so(self, project):
        (project / "data" / "audio" / "solaris" / "report.json").write_text(json.dumps({
            "slug": "solaris", "chunks_total": 2, "chunks_rendered": 1,
            "failures": [{"chunk_id": "ch001_0001", "error": "boom"}],
        }), encoding="utf-8")
        book = data.get_book(project, "solaris")
        assert book is not None and book.state == "failed"

    def test_state_is_silence_when_the_audio_came_from_a_dry_run(self, project):
        (project / "data" / "audio" / "solaris" / ".dry-run.json").write_text(
            json.dumps({"schema_version": 1, "slug": "solaris", "chunks": 2}),
            encoding="utf-8")
        book = data.get_book(project, "solaris")
        assert book is not None
        assert book.dry_run_audio is True
        assert book.state == "silence"

    def test_silence_outranks_done(self, project):
        """A dry run assembles a finished-looking file that plays as nothing.

        `done` would be a lie here, and it is the one state a reader trusts
        without listening.
        """
        (project / "data" / "out" / "solaris.m4b").write_bytes(b"x")
        (project / "data" / "audio" / "solaris" / ".dry-run.json").write_text(
            "{}", encoding="utf-8")
        book = data.get_book(project, "solaris")
        assert book is not None and book.state == "silence"

    def test_a_real_render_is_not_flagged(self, project):
        (project / "data" / "out" / "solaris.m4b").write_bytes(b"x")
        book = data.get_book(project, "solaris")
        assert book is not None
        assert book.dry_run_audio is False
        assert book.state == "done"

    def _report(self, project, *, ok: bool):
        failures = [] if ok else [{"chunk_id": "ch001_0001", "error": "boom"}]
        (project / "data" / "audio" / "solaris" / "report.json").write_text(json.dumps({
            "slug": "solaris", "chunks_total": 2,
            "chunks_rendered": 2 if ok else 1, "failures": failures,
        }), encoding="utf-8")

    def test_failure_outranks_a_leftover_output(self, project):
        """`done` used to mean nothing more than a file existing in data/out/.

        A render that failed leaves the previous export sitting there, so the
        book reported itself finished while its audio was incomplete.
        """
        (project / "data" / "out" / "solaris.m4b").write_bytes(b"x")
        self._report(project, ok=False)
        book = data.get_book(project, "solaris")
        assert book is not None and book.state == "failed"

    def test_percent_does_not_claim_a_failed_book_is_complete(self, project):
        (project / "data" / "out" / "solaris.m4b").write_bytes(b"x")
        self._report(project, ok=False)
        book = data.get_book(project, "solaris")
        assert book is not None and book.percent == 0.0

    def test_a_successful_report_with_an_output_is_done(self, project):
        (project / "data" / "out" / "solaris.m4b").write_bytes(b"x")
        self._report(project, ok=True)
        book = data.get_book(project, "solaris")
        assert book is not None
        assert book.state == "done"
        assert book.percent == 100.0

    def test_a_successful_report_without_an_output_is_only_rendered(self, project):
        self._report(project, ok=True)
        book = data.get_book(project, "solaris")
        assert book is not None and book.state == "rendered"

    def test_corrupt_json_greys_out_one_card_rather_than_crashing(self, project):
        (project / "data" / "audio" / "solaris" / "report.json").write_text(
            "{not json", encoding="utf-8")
        book = data.get_book(project, "solaris")
        assert book is not None and book.report is None

    def test_missing_book(self, project):
        assert data.get_book(project, "absent") is None


class TestChunks:
    def test_falls_back_to_the_chunker_manifest(self, project):
        chunks = data.load_chunks(project, "solaris")
        assert len(chunks) == 1
        assert chunks[0].audio_path is None

    def test_prefers_the_rendered_manifest_when_it_exists(self, project):
        # Only rendered.jsonl carries audio paths; without this the page could
        # never offer per-fragment playback.
        rendered = dict(json.loads(
            (project / "data" / "book" / "solaris" / "chunks.jsonl").read_text()))
        rendered["audio_path"] = "data/audio/solaris/ch001_0000.wav"
        rendered["duration_sec"] = 1.2
        (project / "data" / "audio" / "solaris" / "rendered.jsonl").write_text(
            json.dumps(rendered) + "\n", encoding="utf-8")
        chunks = data.load_chunks(project, "solaris")
        assert chunks[0].audio_path is not None

    def test_limit(self, project):
        assert data.load_chunks(project, "solaris", limit=1) != []


class TestFileResolution:
    def test_output_must_belong_to_the_slug(self, project):
        (project / "data" / "out" / "other.m4b").write_bytes(b"x")
        assert data.output_file(project, "solaris", "other.m4b") is None

    def test_output_rejects_a_path(self, project):
        with pytest.raises(UnsafeName):
            data.output_file(project, "solaris", "../justfile")

    def test_output_rejects_an_unknown_suffix(self, project):
        with pytest.raises(UnsafeName):
            data.output_file(project, "solaris", "solaris.exe")

    def test_fragment_returns_none_when_not_rendered(self, project):
        assert data.rendered_audio(project, "solaris", "ch001_0000") is None


class TestContainment:
    """A legitimate name can still resolve outside the project.

    `check_name` rejects traversal in the URL, but not a symlink already on
    disk: `data/out/solaris.m4b` pointing at a file elsewhere has a perfectly
    valid name, and serving it follows the link.
    """

    @pytest.fixture
    def outside(self, tmp_path_factory):
        """A directory that is a sibling of the project, never inside it."""
        return tmp_path_factory.mktemp("elsewhere")

    def test_a_path_inside_the_project_is_returned_resolved(self, project):
        inside = project / "data" / "out" / "solaris.m4b"
        inside.write_bytes(b"x")
        assert data.contained(project, inside) == inside.resolve()

    def test_a_path_outside_the_project_is_refused(self, project, outside):
        secret = outside / "secret.m4b"
        secret.write_bytes(b"x")
        with pytest.raises(UnsafeName, match="outside the project"):
            data.contained(project, secret)

    def test_a_sibling_directory_sharing_a_prefix_is_refused(self, project):
        # A string prefix check would accept this; is_relative_to does not.
        sibling = project.parent / f"{project.name}-backup"
        sibling.mkdir(parents=True, exist_ok=True)
        leak = sibling / "leak.m4b"
        leak.write_bytes(b"x")
        with pytest.raises(UnsafeName):
            data.contained(project, leak)

    def test_a_symlinked_output_is_not_served(self, project, outside):
        secret = outside / "secret.m4b"
        secret.write_bytes(b"not yours")
        (project / "data" / "out" / "solaris.m4b").symlink_to(secret)

        assert data.output_file(project, "solaris", "solaris.m4b") is None

    def test_a_symlinked_output_is_not_listed(self, project, outside):
        secret = outside / "secret.m4b"
        secret.write_bytes(b"not yours")
        (project / "data" / "out" / "solaris.m4b").symlink_to(secret)

        assert data.find_outputs(project, "solaris") == []

    def test_a_symlinked_fragment_is_not_served(self, project, outside):
        secret = outside / "secret.wav"
        secret.write_bytes(b"not yours")
        (project / "data" / "audio" / "solaris" / "ch001_0000.wav").symlink_to(secret)

        assert data.rendered_audio(project, "solaris", "ch001_0000") is None

    def test_a_real_output_is_still_served(self, project):
        real = project / "data" / "out" / "solaris.m4b"
        real.write_bytes(b"x")
        assert data.output_file(project, "solaris", "solaris.m4b") == real
        assert data.find_outputs(project, "solaris") == ["solaris.m4b"]

    def test_a_real_fragment_is_still_served(self, project):
        real = project / "data" / "audio" / "solaris" / "ch001_0000.wav"
        real.write_bytes(b"RIFF")
        assert data.rendered_audio(project, "solaris", "ch001_0000") == real
