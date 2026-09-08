"""Word error rate is how a silent synthesis defect gets caught.

XTTS truncates, skips and repeats without raising anything, so the only signal
is the audio disagreeing with the text.
"""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

import transcriber.verify as verify_mod
from transcriber.verify import DRY_RUN_MARKER, normalise_for_compare, word_error_rate


class TestNormalise:
    def test_drops_punctuation_and_case(self):
        assert normalise_for_compare("Ocean, falował!") == ["ocean", "falował"]

    def test_keeps_polish_diacritics(self):
        # ą and a are different words in Polish; folding them would hide a
        # real mispronunciation.
        assert normalise_for_compare("stacją") == ["stacją"]

    def test_empty(self):
        assert normalise_for_compare("   ") == []


class TestWordErrorRate:
    def test_identical_text_scores_zero(self):
        assert word_error_rate("Ocean falował pod stacją.",
                               "ocean falował pod stacją") == 0.0

    def test_unicode_composition_does_not_count_as_an_error(self):
        import unicodedata
        assert word_error_rate("stacją", unicodedata.normalize("NFD", "stacją")) == 0.0

    def test_single_substitution(self):
        assert word_error_rate("Ocean falował pod stacją",
                               "Ocean szumiał pod stacją") == 0.25

    def test_truncation_is_caught(self):
        # The failure mode that matters: XTTS dropping the tail of a chunk.
        assert word_error_rate("Ocean falował pod stacją dziś", "Ocean falował") > 0.5

    def test_repetition_is_caught(self):
        assert word_error_rate("Ocean falował", "Ocean falował Ocean falował") == 1.0

    def test_silence_scores_one(self):
        assert word_error_rate("Ocean falował", "") == 1.0

    def test_both_empty_is_not_an_error(self):
        assert word_error_rate("", "") == 0.0

    def test_heard_text_with_nothing_expected(self):
        assert word_error_rate("", "cokolwiek") == 1.0

    @pytest.mark.parametrize("expected,heard", [
        ("a b c", "a b c"),
        ("a b c", "a x c"),
        ("a b c", "a c"),
        ("a b c", "a b b c"),
    ])
    def test_stays_within_bounds(self, expected, heard):
        assert 0.0 <= word_error_rate(expected, heard) <= 2.0


class TestDryRunGuard:
    """Silence transcribes as nothing, which looks like a broken voice.

    A dry run leaves wavs at every path a real render writes, so without this
    check `just verify` spends an hour on a large model and reports that every
    fragment failed. The cause is that nothing was ever narrated.
    """

    def project(self, tmp_path, monkeypatch, marked):
        audio_dir = tmp_path / "data" / "audio" / "solaris"
        audio_dir.mkdir(parents=True)
        (tmp_path / "justfile").write_text("", encoding="utf-8")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "pipeline.toml").write_text("", encoding="utf-8")
        (audio_dir / "rendered.jsonl").write_text(
            json.dumps({"id": "ch001_0000", "text": "Ocean falował.",
                        "audio_path": "data/audio/solaris/ch001_0000.wav"}) + "\n",
            encoding="utf-8")
        if marked:
            (audio_dir / DRY_RUN_MARKER).write_text("{}", encoding="utf-8")
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(tmp_path))
        return tmp_path

    def test_verifying_dry_run_silence_is_refused(self, tmp_path, monkeypatch):
        self.project(tmp_path, monkeypatch, marked=True)
        result = CliRunner().invoke(verify_mod.app, ["solaris"])
        assert result.exit_code != 0
        assert "dry-run silence" in result.output

    def test_the_refusal_costs_nothing(self, tmp_path, monkeypatch):
        """It must land before whisperx is imported, or the check is pointless."""
        self.project(tmp_path, monkeypatch, marked=True)
        monkeypatch.setitem(sys.modules, "whisperx", None)  # any use would raise
        result = CliRunner().invoke(verify_mod.app, ["solaris"])
        assert "dry-run silence" in result.output

    def test_a_real_render_is_not_refused(self, tmp_path, monkeypatch):
        self.project(tmp_path, monkeypatch, marked=False)
        monkeypatch.setitem(sys.modules, "whisperx", None)
        result = CliRunner().invoke(verify_mod.app, ["solaris"])
        # It gets past the guard and fails later, on the missing model.
        assert "dry-run silence" not in result.output
