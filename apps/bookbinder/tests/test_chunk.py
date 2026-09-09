"""Chunking is where a book can be silently corrupted.

XTTS truncates past its per-language character limit without warning, so an
oversize chunk loses text with no error anywhere. These tests pin the two
properties that matter: nothing exceeds the limit, and no character is lost.
Words stay whole except where a single word is longer than the limit and so
has no break point at all.
"""

from __future__ import annotations

import pytest

from bookbinder.chunk import hard_split, pack, split_sentences
from bookbinder.manifest import char_limit

PL = char_limit("pl")  # 224


def words(chunks: list[str]) -> list[str]:
    return " ".join(chunks).split()


class TestPack:
    def test_short_paragraph_stays_one_chunk(self):
        assert pack(["Ocean falował pod stacją."], PL, 40) == ["Ocean falował pod stacją."]

    def test_never_exceeds_limit(self):
        long = ["Bardzo długie zdanie o oceanie i stacji badawczej. " * 20]
        for chunk in pack(long, PL, 40):
            assert len(chunk) <= PL

    def test_preserves_every_word_in_order(self):
        sentences = [
            "Zszedłem po drabince do kabiny.",
            "Ocean falował pod stacją, a wiatr wiał nieprzerwanie od trzech dni.",
            "Krótko.",
        ]
        assert words(pack(sentences, PL, 40)) == words(sentences)

    def test_trailing_runt_merges_backwards(self):
        out = pack(["Zdanie wystarczająco długie by przekroczyć próg scalania.", "Tak."], PL, 40)
        assert len(out) == 1

    def test_leading_runt_merges_forwards(self):
        # A short opener has no predecessor to fold into; it must fold forward.
        out = pack(["Tak.", "Zdanie wystarczająco długie by przekroczyć próg scalania."], PL, 40)
        assert len(out) == 1
        assert out[0].startswith("Tak.")

    def test_paragraph_of_only_runts_survives(self):
        # Merging must not drop text just because nothing reaches min_chars.
        sentences = ["Tak.", "Nie.", "Może."]
        assert words(pack(sentences, PL, 40)) == words(sentences)

    def test_single_short_sentence_is_kept(self):
        assert pack(["Tak."], PL, 40) == ["Tak."]

    def test_empty_input(self):
        assert pack([], PL, 40) == []

    def test_overlong_token_survives_packing(self):
        # What the pipeline actually calls. A sentence carrying one unbreakable
        # token must keep every character and still respect the limit.
        sentences = ["Przed nim krótkie zdanie.", "b" * 600, "Po nim kolejne zdanie."]
        out = pack(sentences, PL, 40)
        joined = " ".join(out).replace(" ", "")
        assert joined == "".join(sentences).replace(" ", "")
        for chunk in out:
            assert len(chunk) <= PL

    @pytest.mark.parametrize("language", ["pl", "en", "ja", "zh-cn", "ru"])
    def test_respects_each_language_limit(self, language):
        limit = char_limit(language)
        for chunk in pack(["Sentence about the ocean and the station. " * 30], limit, 40):
            assert len(chunk) <= limit


class TestHardSplit:
    def test_short_sentence_untouched(self):
        assert hard_split("Krótkie zdanie.", PL) == ["Krótkie zdanie."]

    def test_splits_on_clause_punctuation(self):
        sentence = "Pierwsza część zdania, " * 20
        for piece in hard_split(sentence, PL):
            assert len(piece) <= PL

    def test_pathological_single_token(self):
        # One 500-char word cannot be split on punctuation or spaces.
        for piece in hard_split("a" * 500, PL):
            assert len(piece) <= PL

    def test_pathological_single_token_keeps_every_character(self):
        # Regression: the overlong-word branch used to truncate to the limit,
        # dropping 276 of these 500 characters with no error anywhere.
        assert "".join(hard_split("a" * 500, PL)) == "a" * 500

    def test_overlong_token_mid_sentence_keeps_every_character(self):
        # A URL is the realistic case: one token with no internal break point.
        sentence = "Zobacz https://example.com/" + "x" * 300 + " i wróć."
        pieces = hard_split(sentence, PL)
        assert "".join(pieces).replace(" ", "") == sentence.replace(" ", "")
        for piece in pieces:
            assert len(piece) <= PL

    def test_overlong_token_flushes_the_pending_chunk_first(self):
        # The words before the giant token must not be swallowed by it.
        sentence = "Krótkie słowa najpierw " + "y" * 400
        pieces = hard_split(sentence, PL)
        assert pieces[0].startswith("Krótkie słowa najpierw")
        assert "".join(pieces).replace(" ", "") == sentence.replace(" ", "")

    def test_never_splits_mid_word(self):
        sentence = " ".join(["wyrazwielosylabowy"] * 40)
        pieces = hard_split(sentence, PL)
        assert " ".join(pieces).split() == sentence.split()


class TestSplitSentences:
    def test_polish_sentence_boundaries(self):
        out = split_sentences("Pierwsze zdanie. Drugie zdanie! Trzecie?", "pl")
        assert len(out) == 3

    def test_unsupported_language_falls_back_to_english(self):
        # Must not raise; pysbd covers 23 languages and this is not one of them.
        assert split_sentences("First sentence. Second one.", "xx-nonexistent")
