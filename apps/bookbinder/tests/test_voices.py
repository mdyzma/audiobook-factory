"""Judging a recording before anything is cloned from it.

Cloning takes minutes and downloads a model on the first run. A recording that
was never going to work should say so in seconds, not at the end of all that.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from bookbinder.voices import (
    CLIPPING_DBFS,
    MIN_USABLE_SECONDS,
    probe,
    report,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)


def tone(path, seconds=30.0, volume=0.3, rate=24000, channels=1):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=220:duration={seconds}:sample_rate={rate}",
         "-af", f"volume={volume}", "-ac", str(channels),
         "-c:a", "pcm_s16le", str(path)], check=True)
    return path


class TestUsableRecordings:
    def test_a_clean_recording_passes(self, tmp_path):
        result = probe(tone(tmp_path / "v.wav"))
        assert result.usable
        assert result.problems == []

    def test_it_reports_what_the_file_is(self, tmp_path):
        result = probe(tone(tmp_path / "v.wav", seconds=30.0, rate=22050))
        assert result.seconds == pytest.approx(30.0, abs=0.3)
        assert result.sample_rate == 22050
        assert result.channels == 1


class TestRecordingsThatWillNotWork:
    def test_a_missing_file_says_so(self, tmp_path):
        result = probe(tmp_path / "absent.wav")
        assert not result.usable
        assert "no file" in result.problems[0]

    def test_too_short_is_refused(self, tmp_path):
        result = probe(tone(tmp_path / "v.wav", seconds=5.0))
        assert not result.usable
        assert any("at least" in p for p in result.problems)

    def test_silence_is_refused(self, tmp_path):
        # A recording that captured nothing. Its duration looks fine.
        path = tmp_path / "v.wav"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "anullsrc=r=24000:cl=mono", "-t", "30",
             "-c:a", "pcm_s16le", str(path)], check=True)
        result = probe(path)
        assert not result.usable
        assert any("silence" in p for p in result.problems)

    def test_a_file_that_is_not_audio_is_refused(self, tmp_path):
        path = tmp_path / "v.wav"
        path.write_bytes(b"this is not a wav file at all")
        assert not probe(path).usable


class TestWarnings:
    """Things worth knowing that are not reasons to stop."""

    def test_clipping_is_flagged_without_refusing(self, tmp_path):
        # lavfi sine is quiet; this is the gain that actually saturates.
        result = probe(tone(tmp_path / "v.wav", volume=10.0))
        assert result.usable
        assert any("clipped" in w for w in result.warnings)
        assert result.peak_dbfs is not None and result.peak_dbfs >= CLIPPING_DBFS

    def test_a_short_but_workable_recording_is_flagged(self, tmp_path):
        result = probe(tone(tmp_path / "v.wav", seconds=MIN_USABLE_SECONDS + 5))
        assert result.usable
        assert any("enough to try" in w for w in result.warnings)

    def test_stereo_is_flagged(self, tmp_path):
        # Right for one speaker, wrong for an interview with two.
        result = probe(tone(tmp_path / "v.wav", channels=2))
        assert result.usable
        assert any("channels" in w for w in result.warnings)

    def test_a_long_clean_mono_recording_has_nothing_to_report(self, tmp_path):
        result = probe(tone(tmp_path / "v.wav", seconds=130.0))
        assert result.usable and result.warnings == []


class TestReport:
    def test_it_names_the_file_and_the_numbers(self, tmp_path):
        text = report(probe(tone(tmp_path / "sample.wav")))
        assert "sample.wav" in text
        assert "Hz" in text and "dBFS" in text

    def test_a_refusal_is_visible(self, tmp_path):
        text = report(probe(tone(tmp_path / "v.wav", seconds=3.0)))
        assert "will not work" in text

    def test_a_clean_recording_says_so(self, tmp_path):
        text = report(probe(tone(tmp_path / "v.wav", seconds=130.0)))
        assert "should clone cleanly" in text
