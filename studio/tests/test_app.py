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
