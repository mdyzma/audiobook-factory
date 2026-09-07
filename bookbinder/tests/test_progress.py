"""Progress is the only view into a render that takes hours.

report.json appears when the run ends; this file is rewritten as it goes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from bookbinder.manifest import RenderProgress
from bookbinder.progress import STALE_AFTER_SEC, age_seconds, format_duration


class TestPercentAndEta:
    def test_percent(self):
        p = RenderProgress(slug="s", chunks_total=200, chunks_done=50)
        assert p.percent == 25.0

    def test_percent_with_no_chunks_does_not_divide_by_zero(self):
        assert RenderProgress(slug="s").percent == 0.0

    def test_eta_from_the_rate_so_far(self):
        # 25 rendered in 50 s is 2 s each; 75 left is 150 s.
        p = RenderProgress(slug="s", chunks_total=100, chunks_done=25,
                           chunks_rendered=25, elapsed_sec=50)
        assert p.eta_sec == 150.0

    def test_eta_ignores_skipped_chunks(self):
        # A resumed run skips instantly. Counting those in the rate would make
        # the estimate wildly optimistic for the work that remains.
        p = RenderProgress(slug="s", chunks_total=100, chunks_done=90,
                           chunks_rendered=10, chunks_skipped=80, elapsed_sec=100)
        assert p.eta_sec == 100.0

    def test_eta_is_zero_before_anything_renders(self):
        assert RenderProgress(slug="s", chunks_total=100, elapsed_sec=5).eta_sec == 0.0

    def test_eta_is_zero_when_finished(self):
        p = RenderProgress(slug="s", chunks_total=10, chunks_done=10,
                           chunks_rendered=10, elapsed_sec=20)
        assert p.eta_sec == 0.0


class TestAtomicWrite:
    def test_writes_and_leaves_no_temp_file(self, tmp_path):
        path = tmp_path / "progress.json"
        RenderProgress(slug="s", chunks_total=3).write(path)
        assert json.loads(path.read_text())["slug"] == "s"
        # A reader polling this must never catch a half-written file.
        assert list(tmp_path.glob("*.tmp")) == []

    def test_overwrites_in_place(self, tmp_path):
        path = tmp_path / "progress.json"
        RenderProgress(slug="s", chunks_total=3, chunks_done=1).write(path)
        RenderProgress(slug="s", chunks_total=3, chunks_done=2).write(path)
        assert json.loads(path.read_text())["chunks_done"] == 2


class TestStaleness:
    def test_recent_timestamp_is_fresh(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        assert (age_seconds(now) or 0) < STALE_AFTER_SEC

    def test_old_timestamp_is_stale(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        assert (age_seconds(old) or 0) > STALE_AFTER_SEC

    def test_unparseable_timestamp(self):
        assert age_seconds("not a date") is None

    def test_naive_timestamp_is_treated_as_utc(self):
        assert age_seconds("2020-01-01T00:00:00") is not None


class TestFormatDuration:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "0s"), (45, "45s"), (90, "1m 30s"), (3600, "1h 00m"), (7325, "2h 02m"),
    ])
    def test_formats(self, seconds, expected):
        assert format_duration(seconds) == expected


class TestNarratorShape:
    """narrator/synth.py builds this dict by hand, in an environment that cannot
    import the model. Keep the two in step."""

    def test_what_narrator_writes_validates(self):
        written = {
            "schema_version": 1, "slug": "solaris", "running": True, "pid": 4242,
            "dry_run": False, "device": "mps",
            "started_at": "2026-09-07T10:00:00+00:00",
            "updated_at": "2026-09-07T10:05:00+00:00",
            "elapsed_sec": 300.0, "chunks_total": 100, "chunks_done": 40,
            "chunks_rendered": 30, "chunks_skipped": 10, "chunks_failed": 0,
            "audio_sec": 250.0, "current_chunk_id": "ch002_0040",
            "current_voice": "michal", "last_error": "",
            "percent": 40.0, "eta_sec": 600.0,
        }
        p = RenderProgress.model_validate(written)
        assert p.percent == written["percent"]
        assert p.eta_sec == written["eta_sec"]
        assert p.current_voice == "michal"
