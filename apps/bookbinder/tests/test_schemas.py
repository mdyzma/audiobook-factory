"""The exported schemas are the contract narrator and transcriber write to.

They cannot import these models, so a field renamed here and not there is a
silent divergence. These pin the fields those two environments depend on.
"""

from __future__ import annotations

from bookbinder.manifest import SCHEMA_VERSION, json_schemas

V = SCHEMA_VERSION  # the set is versioned as a unit; these follow it


class TestExportedSchemas:
    def test_exports_every_cross_environment_model(self):
        assert set(json_schemas()) == {
            f"chunk_v{V}", f"book_meta_v{V}", f"render_report_v{V}",
            f"render_progress_v{V}", f"qa_report_v{V}",
        }

    def test_chunk_carries_what_the_narrator_reads_and_writes(self):
        props = json_schemas()[f"chunk_v{V}"]["properties"]
        # narrator reads these
        for field in ("id", "text", "language", "role", "order"):
            assert field in props
        # narrator writes these back
        for field in ("audio_path", "duration_sec"):
            assert field in props

    def test_book_meta_carries_the_cast(self):
        assert "cast" in json_schemas()[f"book_meta_v{V}"]["properties"]

    def test_schemas_describe_computed_fields_too(self):
        # Exported in serialization mode on purpose: a validation-mode schema
        # omits computed fields, which are present in the files on disk.
        assert "realtime_factor" in json_schemas()[f"render_report_v{V}"]["properties"]
        assert "percent" in json_schemas()[f"render_progress_v{V}"]["properties"]

    def test_render_report_matches_what_narrator_writes(self):
        # narrator/synth.py builds this dict by hand; keep the two in step.
        props = json_schemas()[f"render_report_v{V}"]["properties"]
        for field in ("slug", "voice", "cast", "device", "dry_run", "started_at",
                      "finished_at", "elapsed_sec", "chunks_total",
                      "chunks_rendered", "chunks_skipped", "audio_sec", "failures",
                      # Which backend produced the audio, and at what rate.
                      # Without these, a book rendered under one model and
                      # resumed under another reads as one clean run.
                      "model", "engine", "sample_rate"):
            assert field in props

    def test_render_progress_matches_what_narrator_writes(self):
        props = json_schemas()[f"render_progress_v{V}"]["properties"]
        for field in ("slug", "running", "pid", "dry_run", "device", "started_at",
                      "updated_at", "elapsed_sec", "chunks_total", "chunks_done",
                      "chunks_rendered", "chunks_skipped", "chunks_failed",
                      "audio_sec", "current_chunk_id", "current_voice",
                      "last_error", "percent", "eta_sec"):
            assert field in props

    def test_qa_report_matches_what_transcriber_writes(self):
        props = json_schemas()[f"qa_report_v{V}"]["properties"]
        for field in ("slug", "model", "max_wer", "chunks_checked",
                      "mean_wer", "findings"):
            assert field in props

    def test_schemas_are_serialisable(self):
        import json
        for name, schema in json_schemas().items():
            assert json.dumps(schema), name


class TestCrossEnvironmentShape:
    """narrator and transcriber write these files by hand, in environments
    that cannot import the models. This validates a copy of what they emit."""

    def test_narrator_render_report_validates(self):
        from bookbinder.manifest import RenderReport

        # Field for field what narrator/src/narrator/synth.py writes.
        written = {
            "schema_version": 1, "slug": "solaris", "voice": "",
            "cast": {"narrator": "michal"}, "device": "mps", "dry_run": False,
            "started_at": "2026-09-06T04:00:00+00:00",
            "finished_at": "2026-09-06T04:05:00+00:00",
            "elapsed_sec": 300.0, "chunks_total": 10, "chunks_rendered": 8,
            "chunks_skipped": 2, "audio_sec": 120.0, "failures": [],
            "realtime_factor": 0.4, "ok": True,
        }
        report = RenderReport.model_validate(written)
        # Computed fields must agree with what narrator calculated.
        assert report.realtime_factor == written["realtime_factor"]
        assert report.ok == written["ok"]

    def test_narrator_failure_entries_validate(self):
        from bookbinder.manifest import RenderReport

        report = RenderReport.model_validate({
            "schema_version": 1, "slug": "s", "chunks_total": 1,
            "failures": [{"chunk_id": "ch001_0000", "error": "RuntimeError: boom"}],
        })
        assert not report.ok

    def test_transcriber_qa_report_validates(self):
        from bookbinder.manifest import QaReport

        # Field for field what transcriber/src/transcriber/verify.py writes.
        report = QaReport.model_validate({
            "schema_version": 1, "slug": "solaris", "model": "large-v3",
            "max_wer": 0.15, "chunks_checked": 3, "mean_wer": 0.02,
            "findings": [{"chunk_id": "ch001_0004", "expected": "Ocean falował",
                          "heard": "Ocean", "wer": 0.5}],
        })
        assert not report.ok
        assert report.findings[0].wer == 0.5


class TestTheModelBlockIsTheEngineContract:
    """`BookMeta.model` is what a narrator environment reads to know what to
    load. narrator mirrors it in narrator/choice.py and cannot import it."""

    def test_book_meta_carries_the_resolved_backend(self):
        props = json_schemas()[f"book_meta_v{V}"]["properties"]
        assert "model" in props

    def test_the_model_block_names_what_narrator_needs(self):
        schema = json_schemas()[f"book_meta_v{V}"]
        model = schema["$defs"]["ModelChoice"]["properties"]
        for field in ("id", "engine", "environment", "checkpoint", "revision",
                      "native_sample_rate", "char_limit", "settings",
                      "unsupported", "source"):
            assert field in model

    def test_the_mirror_on_the_narrator_side_matches(self):
        # The two are separate environments, so this is the only place the
        # shapes are compared. A field added here and not there is silently
        # dropped when narrator reads the book.
        import re
        from pathlib import Path

        # tests/ -> bookbinder/ -> apps/
        mirror = (Path(__file__).resolve().parents[2] / "narrator" / "src"
                  / "narrator" / "choice.py")
        assert mirror.exists(), f"no mirror at {mirror}"

        declared = set(re.findall(r"^    (\w+): ", mirror.read_text(encoding="utf-8"),
                                  re.MULTILINE))
        exported = set(json_schemas()[f"book_meta_v{V}"]["$defs"]["ModelChoice"]["properties"])
        assert exported <= declared, f"narrator/choice.py is missing {exported - declared}"
