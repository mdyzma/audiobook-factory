"""Word error rate is how a silent synthesis defect gets caught.

XTTS truncates, skips and repeats without raising anything, so the only signal
is the audio disagreeing with the text.
"""

from __future__ import annotations

import pytest

from transcriber.verify import normalise_for_compare, word_error_rate


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
