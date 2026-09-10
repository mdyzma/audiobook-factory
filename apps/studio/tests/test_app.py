"""Routes.

This server turns URL segments into filesystem paths and serves whatever it
finds, so the rejection cases matter more than the happy ones.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from studio.app import app


@pytest.fixture
def client(project):
    return TestClient(app)


class TestPages:
    def test_dashboard_lists_books_and_voices(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Sołaris" in r.text
        assert "michal" in r.text

    def test_dashboard_survives_an_empty_project(self, tmp_path, monkeypatch):
        from studio import data
        monkeypatch.setattr(data, "project_root", lambda: tmp_path)
        r = TestClient(app).get("/")
        assert r.status_code == 200
        assert "No books yet" in r.text

    def test_book_page(self, client):
        r = client.get("/book/solaris")
        assert r.status_code == 200
        assert "Przybysz" in r.text
        assert "narrator" in r.text

    def test_voice_page(self, client):
        r = client.get("/voice/michal")
        assert r.status_code == 200
        assert "18 labelled segments" in r.text

    def test_missing_book_is_404(self, client):
        assert client.get("/book/absent").status_code == 404

    def test_missing_voice_is_404(self, client):
        assert client.get("/voice/absent").status_code == 404


class TestApi:
    def test_books(self, client):
        body = client.get("/api/books").json()
        assert body[0]["slug"] == "solaris"
        assert body[0]["state"] == "prepared"

    def test_book_detail(self, client):
        body = client.get("/api/books/solaris").json()
        assert body["title"] == "Sołaris"
        assert body["cast"]["narrator"] == "michal"

    def test_progress_without_a_render(self, client):
        body = client.get("/api/books/solaris/progress").json()
        assert body["running"] is False

    def test_progress_during_a_render(self, client, project):
        from datetime import datetime, timezone
        (project / "data" / "audio" / "solaris" / "progress.json").write_text(json.dumps({
            "slug": "solaris", "running": True, "chunks_total": 10, "chunks_done": 4,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }), encoding="utf-8")
        body = client.get("/api/books/solaris/progress").json()
        assert body["state"] == "rendering"
        assert body["percent"] == 40.0

    def test_chunks_paginate(self, client):
        body = client.get("/api/books/solaris/chunks?limit=1").json()
        assert body["total"] == 1
        assert len(body["chunks"]) == 1

    def test_voices(self, client):
        assert client.get("/api/voices").json()[0]["name"] == "michal"


class TestAudio:
    def test_audition(self, client):
        r = client.get("/audio/audition/michal")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"

    def test_output(self, client, project):
        (project / "data" / "out" / "solaris.m4b").write_bytes(b"fake")
        r = client.get("/audio/out/solaris/solaris.m4b")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/mp4"

    def test_unrendered_fragment_is_404(self, client):
        assert client.get("/audio/fragment/solaris/ch001_0000").status_code == 404


class TestSecurity:
    """Nothing outside data/ may be reachable, whatever the URL says."""

    @pytest.mark.parametrize("url", [
        "/book/..%2F..%2Fetc",
        "/voice/..%2F..%2Fetc%2Fpasswd",
        "/api/books/..%2F..%2Fetc",
        "/audio/audition/..%2F..%2Fetc%2Fpasswd",
        "/audio/fragment/solaris/..%2F..%2Fjustfile",
        "/audio/out/solaris/..%2F..%2Fjustfile",
    ])
    def test_traversal_never_serves_a_file(self, client, url):
        assert client.get(url).status_code in (400, 404)

    def test_an_output_belonging_to_another_book_is_refused(self, client, project):
        (project / "data" / "out" / "other.m4b").write_bytes(b"x")
        assert client.get("/audio/out/solaris/other.m4b").status_code == 404

    def test_a_non_audio_output_is_refused(self, client, project):
        (project / "data" / "out" / "solaris.txt").write_text("secret")
        assert client.get("/audio/out/solaris/solaris.txt").status_code == 400


class TestJobRoutes:
    def test_actions_are_advertised(self, client):
        actions = client.get("/api/actions").json()
        assert "synth" in actions and "chunk" in actions
        # Nothing that would let a caller name its own command.
        assert "run" not in actions and "exec" not in actions

    def test_starting_an_unknown_action_is_refused(self, client):
        r = client.post("/api/jobs", json={"action": "rm", "args": {"slug": "solaris"}})
        assert r.status_code == 409
        assert "unknown action" in r.json()["detail"]

    def test_starting_with_an_unsafe_argument_is_refused(self, client):
        r = client.post("/api/jobs", json={"action": "chunk", "args": {"slug": "../etc"}})
        assert r.status_code == 409

    def test_missing_job_is_404(self, client):
        assert client.get("/api/jobs/deadbeef1234").status_code == 404

    def test_log_of_an_unknown_job_is_empty(self, client):
        r = client.get("/api/jobs/deadbeef1234/log")
        assert r.status_code == 200 and r.text == ""

    def test_cancelling_an_unknown_job_is_404(self, client):
        assert client.post("/api/jobs/deadbeef1234/cancel").status_code == 404

    def test_job_ids_are_validated(self, client):
        assert client.get("/api/jobs/..%2F..%2Fetc/log").status_code in (400, 404)

    def test_jobs_list_is_empty_before_anything_runs(self, client):
        assert client.get("/api/jobs").json() == []


class TestAuthoringRoutes:
    def test_library_page(self, client):
        r = client.get("/library")
        assert r.status_code == 200
        assert "Cast" in r.text

    def test_upload_and_list(self, client):
        r = client.post("/api/upload/book",
                        files={"file": ("solaris.txt", b"content", "text/plain")})
        assert r.status_code == 200
        assert r.json()["name"] == "solaris.txt"
        assert client.get("/api/raw/book").json()[0]["name"] == "solaris.txt"

    def test_upload_refuses_an_unexpected_extension(self, client):
        r = client.post("/api/upload/book",
                        files={"file": ("evil.sh", b"#!/bin/sh", "text/plain")})
        assert r.status_code == 400

    def test_upload_refuses_an_unknown_kind(self, client):
        r = client.post("/api/upload/system",
                        files={"file": ("a.txt", b"x", "text/plain")})
        assert r.status_code == 400

    def test_set_and_read_a_role_correction(self, client):
        r = client.post("/api/books/solaris/roles",
                        json={"source_ref": "c1.xhtml#p2", "role": "kelvin"})
        assert r.status_code == 200
        assert client.get("/api/books/solaris/roles").json() == {"c1.xhtml#p2": "kelvin"}

    def test_a_role_correction_is_validated(self, client):
        r = client.post("/api/books/solaris/roles",
                        json={"source_ref": "c1.xhtml#p2", "role": "../etc"})
        assert r.status_code == 400

    def test_a_correction_for_an_unknown_book_is_refused(self, client):
        r = client.post("/api/books/absent/roles",
                        json={"source_ref": "x#p1", "role": "kelvin"})
        assert r.status_code == 400

    def test_read_and_write_the_cast(self, client, project):
        (project / "config").mkdir(exist_ok=True)
        (project / "config" / "cast.yml").write_text(
            "roles:\n  narrator:\n    voice: michal\n", encoding="utf-8")
        assert client.get("/api/cast").json()["narrator"]["voice"] == "michal"

        r = client.post("/api/cast", json={"roles": {
            "narrator": {"voice": "michal", "speed": 1.0},
            "kelvin": {"voice": "michal", "speed": 0.98}}})
        assert r.status_code == 200
        assert "kelvin" in r.json()["roles"]

    def test_a_cast_without_a_narrator_is_refused(self, client):
        r = client.post("/api/cast", json={"roles": {"kelvin": {"voice": "michal"}}})
        assert r.status_code == 400

    def test_a_malformed_cast_payload_is_refused(self, client):
        assert client.post("/api/cast", json={"roles": "narrator"}).status_code == 400

    def test_ingest_action_only_accepts_an_uploaded_file(self, client):
        r = client.post("/api/jobs", json={"action": "ingest",
                                           "args": {"source": "../../justfile", "slug": "x"}})
        assert r.status_code == 409


class TestQualityReview:
    def test_page_without_a_report(self, client):
        r = client.get("/book/solaris/quality")
        assert r.status_code == 200
        assert "No quality check yet" in r.text

    def test_page_lists_findings_worst_first(self, client, project):
        (project / "data" / "audio" / "solaris" / "qa_report.json").write_text(json.dumps({
            "slug": "solaris", "model": "large-v3", "max_wer": 0.15,
            "chunks_checked": 3, "mean_wer": 0.2,
            "findings": [
                {"chunk_id": "ch001_0001", "expected": "a b", "heard": "a", "wer": 0.5},
                {"chunk_id": "ch001_0002", "expected": "c d", "heard": "", "wer": 1.0},
            ],
        }), encoding="utf-8")
        r = client.get("/book/solaris/quality")
        assert r.status_code == 200
        # The worst offender is the one worth looking at first.
        assert r.text.index("ch001_0002") < r.text.index("ch001_0001")

    def test_page_says_so_when_nothing_is_flagged(self, client, project):
        (project / "data" / "audio" / "solaris" / "qa_report.json").write_text(json.dumps({
            "slug": "solaris", "chunks_checked": 3, "mean_wer": 0.01, "findings": [],
        }), encoding="utf-8")
        assert "Nothing flagged" in client.get("/book/solaris/quality").text

    def test_missing_book_is_404(self, client):
        assert client.get("/book/absent/quality").status_code == 404

    def test_resynth_rejects_an_unsafe_fragment_id(self, client):
        r = client.post("/api/jobs", json={"action": "resynth",
                                           "args": {"slug": "solaris", "chunks": "../etc"}})
        assert r.status_code == 409


class TestVoiceCreationRoute:
    def test_library_offers_a_create_button_not_a_command(self, client, project):
        samples = project / "data" / "raw" / "voices"
        samples.mkdir(parents=True, exist_ok=True)
        (samples / "michal.wav").write_bytes(b"RIFF")
        r = client.get("/library")
        assert "Create voice" in r.text
        # The page used to tell people to go and run this themselves.
        assert "just voice" not in r.text

    def test_creating_a_voice_validates_its_arguments(self, client):
        r = client.post("/api/jobs", json={"action": "voice",
                                           "args": {"sample": "../etc/passwd.wav",
                                                    "name": "x", "language": "pl"}})
        assert r.status_code == 409
class TestSourceEvidenceIsShown:
    """Ingestion decides the encoding and the language, and both are judgements.

    Showing only the answer would hide a book that was guessed at, which is the
    case a reader most needs to see.
    """

    def _with_provenance(self, project, **overrides):
        import json
        path = project / "data" / "book" / "solaris" / "book.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["encoding"] = {
            "encoding": "cp1250", "method": "quality", "score": 1.0,
            "equivalent": ["cp1250"], "decoder_version": 1, "warnings": [],
        }
        payload["language_decision"] = {
            "method": "content", "confidence": 0.99, "detector": "lingua",
            "metadata_language": "en", "coverage_chars": 900,
            "samples": [{"where": "c1.xhtml", "chars": 900, "language": "pl",
                         "confidence": 0.99}],
            "warnings": ["metadata claims 'en' but the text reads as 'pl'"],
        }
        payload.update(overrides)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_the_dashboard_shows_the_encoding_and_language(self, client, project):
        self._with_provenance(project)
        body = client.get("/").text
        assert "cp1250" in body
        assert "pl" in body

    def test_the_book_page_shows_the_evidence(self, client, project):
        self._with_provenance(project)
        body = client.get("/book/solaris").text
        assert "read as cp1250" in body
        assert "chosen by quality" in body
        assert "c1.xhtml" in body
        assert "the file claimed en" in body

    def test_a_warning_is_surfaced(self, client, project):
        self._with_provenance(project)
        assert "metadata claims" in client.get("/book/solaris").text

    def test_a_book_needing_review_says_so_on_both_pages(self, client, project):
        self._with_provenance(
            project, needs_review=True,
            review_reasons=["cp1250 and iso-8859-2 are equally plausible"])
        assert "needs review" in client.get("/").text
        page = client.get("/book/solaris").text
        assert "needs review" in page
        assert "equally plausible" in page

    def test_a_book_without_provenance_still_renders(self, client):
        # Books imported before any of this existed have empty records.
        assert client.get("/book/solaris").status_code == 200
        assert client.get("/").status_code == 200


class TestQualityReportsSayWhatTheyCover:
    """A quality report outlives the audio it was made from.

    Shown without that context, a sampled pass over a render that has since
    been redone reads exactly like a full pass over the current one.
    """

    def _qa(self, project, **overrides):
        import json
        payload = {
            "schema_version": 6, "slug": "solaris", "model": "large-v3",
            "max_wer": 0.15, "chunks_checked": 1, "mean_wer": 0.02,
            "coverage": {"checked": 1, "available": 1, "sample": 0,
                         "languages": ["pl"]},
            "audio_fingerprint": "", "synthesis_model": "xtts-v2",
            "findings": [],
        }
        payload.update(overrides)
        (project / "data" / "audio" / "solaris" / "qa_report.json").write_text(
            json.dumps(payload), encoding="utf-8")

    def _rendered(self, project, fingerprint="abc"):
        import json
        row = {"id": "ch001_0000", "chapter_index": 1, "chapter_title": "Przybysz",
               "order": 0, "text": "Ocean falował.", "kind": "paragraph",
               "language": "pl", "role": "narrator", "is_dialogue": False,
               "pause_after_ms": 350, "chars": 14, "est_seconds": 0.9,
               "source_ref": "", "audio_path": "data/audio/solaris/ch001_0000.wav",
               "duration_sec": 1.0, "voice": "michal", "fingerprint": fingerprint}
        (project / "data" / "audio" / "solaris" / "rendered.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8")

    def test_a_full_check_says_so(self, client, project):
        self._rendered(project)
        self._qa(project)
        assert "every rendered fragment was checked" in client.get(
            "/book/solaris/quality").text

    def test_a_sampled_check_says_so(self, client, project):
        self._rendered(project)
        self._qa(project, coverage={"checked": 1, "available": 20, "sample": 20,
                                    "languages": ["pl"]})
        body = client.get("/book/solaris/quality").text
        assert "sampled" in body
        assert "1 of 20" in body

    def test_the_languages_heard_are_named(self, client, project):
        self._rendered(project)
        self._qa(project, coverage={"checked": 2, "available": 2, "sample": 0,
                                    "languages": ["en", "pl"]})
        assert "en, pl" in client.get("/book/solaris/quality").text

    def test_a_report_describing_older_audio_is_flagged(self, client, project):
        from studio.data import rendered_fingerprint

        self._rendered(project, fingerprint="first-render")
        self._qa(project, audio_fingerprint="a-completely-different-render")
        body = client.get("/book/solaris/quality").text
        assert "ran against different audio" in body

    def test_a_report_matching_the_current_audio_is_not_flagged(self, client, project):
        from studio.data import rendered_fingerprint

        self._rendered(project, fingerprint="current")
        current = rendered_fingerprint(project / "data" / "audio" / "solaris")
        self._qa(project, audio_fingerprint=current)
        assert "ran against different audio" not in client.get(
            "/book/solaris/quality").text

    def test_a_report_from_before_this_existed_is_not_flagged(self, client, project):
        # No fingerprint recorded, so nothing can be concluded either way.
        self._rendered(project)
        self._qa(project, audio_fingerprint="")
        assert "ran against different audio" not in client.get(
            "/book/solaris/quality").text


class TestTheBatchPage:
    """The screen a batch is committed from, and the queue it commits to."""

    def _book(self, project, slug="eden"):
        from test_batch import write_book

        return write_book(project, slug)

    def test_it_opens_with_nothing_imported(self, client):
        assert client.get("/batch").status_code == 200

    def test_it_lists_what_is_here(self, client, project):
        self._book(project)
        assert "Eden" in client.get("/batch").text

    def test_it_shows_how_the_source_was_read(self, client, project):
        self._book(project)
        page = client.get("/batch").text
        assert "read as" in page and "language" in page

    def test_the_review_is_available_as_data(self, client, project):
        self._book(project)
        rows = client.get("/api/batch/books").json()
        eden = next(r for r in rows if r["slug"] == "eden")
        assert eden["ready"] and eden["reader"] == "michal"

    def test_queueing_puts_steps_in_the_queue(self, client, project):
        self._book(project)
        result = client.post("/api/batch/queue",
                             json={"books": ["eden"], "steps": ["synth"]}).json()
        assert result["queued"] == ["eden/synth"]
        assert client.get("/api/queue").json()["counts"]["pending"] == 1

    def test_a_book_needing_a_decision_comes_back_with_its_reason(self, client, project):
        from test_batch import write_book

        write_book(project, "eden", voice="", cast={})
        result = client.post("/api/batch/queue", json={"books": ["eden"]}).json()
        assert "nobody would read it" in result["skipped"]["eden"]

    def test_scanning_a_folder_that_is_not_there_says_so(self, client):
        r = client.post("/api/batch/scan", json={"folder": "/no/such/folder"})
        assert r.status_code == 400

    def test_scanning_changes_nothing(self, client, project, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "notes.md").write_text("not a book", encoding="utf-8")
        result = client.post("/api/batch/scan", json={"folder": str(inbox)}).json()
        assert result["books"] == 0 and result["unsupported"] == 1
        assert not (project / "data" / "book" / "notes").exists()

    def test_an_unsafe_slug_is_refused(self, client):
        r = client.post("/api/batch/queue", json={"books": ["../etc"]})
        assert r.status_code == 400


class TestDrivingTheQueue:
    def _queued(self, client, project):
        from test_batch import write_book

        write_book(project, "eden")
        client.post("/api/batch/queue", json={"books": ["eden"], "steps": ["synth"]})
        return client.get("/api/queue").json()["items"][0]["id"]

    def test_a_step_can_be_held_and_released(self, client, project):
        item_id = self._queued(client, project)
        assert client.post(f"/api/queue/{item_id}/pause").json()["status"] == "paused"
        assert client.post(f"/api/queue/{item_id}/resume").json()["status"] == "pending"

    def test_a_step_can_be_dropped(self, client, project):
        item_id = self._queued(client, project)
        assert client.post(f"/api/queue/{item_id}/cancel").json()["status"] == "cancelled"

    def test_a_dropped_step_can_be_offered_again(self, client, project):
        item_id = self._queued(client, project)
        client.post(f"/api/queue/{item_id}/cancel")
        assert client.post(f"/api/queue/{item_id}/retry").json()["status"] == "pending"

    def test_a_whole_book_can_be_held(self, client, project):
        self._queued(client, project)
        assert client.post("/api/queue/book/eden/pause").json()["changed"] == 1

    def test_an_invented_verb_is_refused(self, client, project):
        item_id = self._queued(client, project)
        assert client.post(f"/api/queue/{item_id}/destroy").status_code == 400

    def test_retrying_something_that_has_not_failed_is_refused(self, client, project):
        item_id = self._queued(client, project)
        assert client.post(f"/api/queue/{item_id}/retry").status_code == 400

    def test_the_queue_shows_on_the_page(self, client, project):
        self._queued(client, project)
        page = client.get("/batch").text
        assert "The queue" in page and "eden" in page


class TestTheDrainSwitch:
    def test_the_dashboard_runs_the_queue_by_default(self, monkeypatch):
        from studio.app import NO_DRAIN, draining

        monkeypatch.delenv(NO_DRAIN, raising=False)
        assert draining()

    @pytest.mark.parametrize("value", ["1", "true", "YES"])
    def test_it_can_be_told_not_to(self, monkeypatch, value):
        from studio.app import NO_DRAIN, draining

        monkeypatch.setenv(NO_DRAIN, value)
        assert not draining()
