"""Choosing the language, and knowing when not to.

A wrong language picks the wrong pronunciation, the wrong sentence splitter,
the wrong eligible models and the wrong ASR model to check the result with. An
abstention costs one question; a confident mistake costs a whole book.
"""

from __future__ import annotations

import pytest

from bookbinder.language import (
    CONFIDENCE_THRESHOLD,
    MIN_TOTAL_CHARS,
    SUPPORTED,
    classify,
    decide,
    gather_samples,
    normalise_code,
)

PL_BODY = (
    "Ocean falował pod stacją, a wiatr wiał nieprzerwanie od trzech dni. "
    "Zszedłem po drabince do kabiny i zamknąłem właz za sobą, nasłuchując. "
    "Śnieg padał na łąki pod Łodzią, a mgła osiadła na rzece o świcie. "
    "Wczesnym rankiem wróciłem na pokład i długo patrzyłem w stronę brzegu."
)

EN_BODY = (
    "The ocean heaved beneath the station and the wind had not stopped for "
    "three days. I climbed down the ladder and closed the hatch behind me. "
    "Snow fell across the meadows and a mist settled on the river at dawn. "
    "Early in the morning I went back on deck and looked towards the shore."
)

DE_BODY = (
    "Der Ozean wogte unter der Station, und der Wind wehte seit drei Tagen "
    "ununterbrochen. Ich stieg die Leiter hinunter und schloss die Luke. "
    "Schnee fiel auf die Wiesen und ein Nebel legte sich auf den Fluss. "
    "Am frühen Morgen ging ich wieder an Deck und blickte zum Ufer hinüber."
)


def book(*bodies: str, titles: bool = True) -> list[dict]:
    return [
        {"index": i + 1, "title": f"Rozdział {i + 1}" if titles else "",
         "source_ref": f"c{i + 1}.xhtml", "paragraphs": [text]}
        for i, text in enumerate(bodies)
    ]


class TestStraightforwardBooks:
    def test_a_polish_book_is_polish(self):
        out = decide(book(PL_BODY, PL_BODY, PL_BODY))
        assert out.language == "pl"
        assert out.method == "content"
        assert out.resolved

    def test_an_english_book_is_english(self):
        out = decide(book(EN_BODY, EN_BODY, EN_BODY))
        assert (out.language, out.method) == ("en", "content")
        assert out.resolved

    def test_confidence_is_recorded(self):
        out = decide(book(PL_BODY, PL_BODY))
        assert out.confidence >= CONFIDENCE_THRESHOLD

    def test_the_detector_is_named_with_the_answer(self):
        assert decide(book(PL_BODY)).detector


class TestAbstention:
    """The cases that used to silently become `pl`."""

    def test_too_little_text_is_not_guessed(self):
        out = decide(book("Tak.", titles=False))
        assert out.needs_review
        assert out.language == ""
        assert any("characters of text" in r for r in out.review_reasons)

    def test_an_empty_book_is_not_guessed(self):
        out = decide([])
        assert out.needs_review and out.language == ""

    def test_german_is_not_rounded_to_the_nearer_of_pl_and_en(self):
        out = decide(book(DE_BODY, DE_BODY, DE_BODY))
        assert out.needs_review
        assert out.language not in SUPPORTED
        assert any("outside" in r for r in out.review_reasons)

    def test_a_book_that_changes_language_partway_stops(self):
        out = decide(book(PL_BODY, PL_BODY, EN_BODY, EN_BODY))
        assert out.needs_review
        assert any("disagree" in r for r in out.review_reasons)

    def test_an_abstention_is_never_silently_resolved(self):
        assert not decide(book("Krótko.", titles=False)).resolved


class TestQuotationsDoNotSwitchTheBook:
    def test_a_polish_book_quoting_english_stays_polish(self):
        quoting = PL_BODY + ' Powiedział cicho: "I love you", i zamilkł.'
        out = decide(book(quoting, PL_BODY, quoting))
        assert out.language == "pl" and out.resolved

    def test_english_front_matter_does_not_decide_a_polish_book(self):
        # A publisher's English copyright page in front of a Polish novel.
        front = ("This book is a work of fiction. Names, characters and "
                 "incidents are the product of the author's imagination and "
                 "any resemblance to actual persons is entirely coincidental. "
                 "All rights reserved under international copyright law here.")
        out = decide(book(front, PL_BODY, PL_BODY, PL_BODY, PL_BODY, PL_BODY))
        # Either Polish outright, or a flagged disagreement. What it must never
        # be is a confident, unflagged English.
        assert out.language == "pl" or out.needs_review


class TestOverrides:
    def test_an_override_wins(self):
        out = decide(book(EN_BODY, EN_BODY), override="pl")
        assert (out.language, out.method) == ("pl", "override")
        assert out.resolved

    def test_an_override_that_contradicts_the_text_keeps_the_evidence(self):
        out = decide(book(EN_BODY, EN_BODY), override="pl")
        assert any("not the requested" in w for w in out.warnings)
        assert [s.language for s in out.samples] == ["en", "en"]

    def test_an_override_rescues_a_book_that_would_otherwise_stop(self):
        out = decide(book("Tak.", titles=False), override="pl")
        assert out.resolved and out.language == "pl"

    def test_an_unsupported_override_is_flagged_but_obeyed(self):
        out = decide(book(DE_BODY), override="de")
        assert out.language == "de"
        assert any("outside the supported set" in w for w in out.warnings)

    @pytest.mark.parametrize("given,expected", [
        ("pl-PL", "pl"), ("en_GB", "en"), ("PL", "pl"), ("", ""), ("en", "en"),
    ])
    def test_region_tags_are_reduced_to_the_language(self, given, expected):
        assert normalise_code(given) == expected


class TestMetadata:
    def test_metadata_agreeing_with_the_text_passes_quietly(self):
        out = decide(book(PL_BODY, PL_BODY), metadata_language="pl")
        assert out.language == "pl" and out.warnings == []

    def test_wrong_metadata_loses_to_the_text_and_is_reported(self):
        # A converted EPUB often keeps the template's language.
        out = decide(book(PL_BODY, PL_BODY), metadata_language="en")
        assert out.language == "pl"
        assert any("metadata claims 'en'" in w for w in out.warnings)

    def test_metadata_alone_cannot_resolve_a_book_with_no_text(self):
        out = decide(book("Tak.", titles=False), metadata_language="en")
        assert out.needs_review


class TestSampling:
    def test_chapter_titles_are_not_sampled(self):
        samples = gather_samples(book(PL_BODY, PL_BODY))
        assert all("Rozdział" not in text for _, text in samples)

    def test_samples_are_spread_across_a_long_book(self):
        chapters = book(*([PL_BODY] * 20))
        wheres = [w for w, _ in gather_samples(chapters)]
        assert len(wheres) <= 5
        assert wheres[0] == "c1.xhtml" and wheres[-1] == "c20.xhtml"

    def test_a_short_book_falls_back_to_the_whole_text(self):
        chapters = [{"index": 1, "title": "", "source_ref": "a",
                     "paragraphs": ["Krótkie zdanie."]}]
        assert [w for w, _ in gather_samples(chapters)] == ["whole book"]

    def test_every_sample_is_recorded_with_its_location(self):
        out = decide(book(PL_BODY, PL_BODY))
        assert [s.where for s in out.samples] == ["c1.xhtml", "c2.xhtml"]
        assert all(s.chars > 0 for s in out.samples)

    def test_coverage_is_reported(self):
        out = decide(book(PL_BODY, PL_BODY))
        assert out.coverage_chars >= MIN_TOTAL_CHARS


class TestClassify:
    @pytest.mark.parametrize("text,expected", [
        (PL_BODY, "pl"), (EN_BODY, "en"), (DE_BODY, "de"),
    ])
    def test_recognises_prose(self, text, expected):
        code, confidence = classify(text)
        assert code == expected
        assert confidence >= CONFIDENCE_THRESHOLD

    @pytest.mark.parametrize("fragment", ["Tak.", "Yes.", "Nie."])
    def test_is_not_confident_about_fragments(self, fragment):
        # The property the abstention rule depends on.
        _code, confidence = classify(fragment)
        assert confidence < CONFIDENCE_THRESHOLD
