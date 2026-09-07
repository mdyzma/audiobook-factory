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
