"""Decide whether a book is Polish or English, or decline to decide.

The old behaviour was a default of `pl` for plain text and whatever an EPUB's
metadata claimed otherwise. A folder holding both languages needs better than
that, and a wrong answer is expensive: it picks the pronunciation, the sentence
splitter, the eligible synthesis models and the ASR model used to check the
result.

Two rules shape everything here. Short text is not evidence, because a detector
asked to classify four words answers confidently and wrongly. And a language
outside the product's two is a reason to stop, never a reason to round to the
nearer of `pl` and `en`.

Detection runs on accepted text from `decode.py`, before any language-specific
preparation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SUPPORTED = ("pl", "en")

# lingua ships its models inside the wheel, so there is nothing to download at
# run time and nothing to pin separately. Recorded with each decision, because
# a later version may rank differently and stored answers must stay readable.
DETECTOR = "lingua-language-detector"

# Below this a sample is not evidence. Four words of Polish and four words of
# English are indistinguishable to any detector; the benchmark behind the
# threshold below shows confidence collapsing under roughly this length.
MIN_SAMPLE_CHARS = 200

# A book with less accepted text than this cannot be judged from content.
MIN_TOTAL_CHARS = 400

# Real prose scored 0.93 to 1.00 in the calibration set and short fragments
# scored 0.06 to 0.12, so this sits in the empty middle rather than on a slope.
CONFIDENCE_THRESHOLD = 0.65

# How many places to look. Front matter is often a different language from the
# body, so a single sample from the start is exactly the wrong choice.
SAMPLE_TARGET = 5


@dataclass
class Sample:
    where: str
    chars: int
    language: str = ""
    confidence: float = 0.0


@dataclass
class LanguageDecision:
    """Everything behind the answer, so a person can disagree with it."""

    language: str = ""
    method: str = "unresolved"        # override | content | metadata | unresolved
    confidence: float = 0.0
    detector: str = DETECTOR
    metadata_language: str = ""
    coverage_chars: int = 0
    samples: list[Sample] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.language in SUPPORTED and not self.needs_review


_detector = None


def _get_detector():
    """Built once. Construction plus the first call costs about a quarter second."""
    global _detector
    if _detector is None:
        from lingua import LanguageDetectorBuilder

        # All languages, deliberately. Restricting the set to Polish and
        # English would make German score as one of them, which is the failure
        # this is supposed to catch.
        _detector = LanguageDetectorBuilder.from_all_languages().build()
    return _detector


def classify(text: str) -> tuple[str, float]:
    """The most likely language of one passage, as an ISO 639-1 code."""
    values = _get_detector().compute_language_confidence_values(text)
    if not values:
        return "", 0.0
    best = values[0]
    return best.language.iso_code_639_1.name.lower(), round(best.value, 4)


def normalise_code(code: str) -> str:
    """`pl-PL` and `en_GB` are `pl` and `en`; anything else keeps its own name."""
    return (code or "").strip().replace("_", "-").split("-")[0].lower()


def gather_samples(chapters: list[dict]) -> list[tuple[str, str]]:
    """Passages worth classifying, as (where, text) pairs.

    Chapter titles and one-line paragraphs are skipped: they are short, they
    are often in the publisher's language rather than the book's, and a title
    like "Rozdzial 1" carries almost no signal.
    """
    bodies: list[tuple[str, str]] = []
    for chapter in chapters:
        text = " ".join(p for p in chapter.get("paragraphs", []) if len(p) > 40)
        if len(text) >= MIN_SAMPLE_CHARS:
            bodies.append((chapter.get("source_ref") or f"chapter {chapter.get('index')}",
                           text[: MIN_SAMPLE_CHARS * 5]))

    if not bodies:
        # Nothing chapter-sized. Fall back to the whole book as one passage, so
        # a short book is still judged rather than silently skipped.
        whole = " ".join(p for c in chapters for p in c.get("paragraphs", []))
        return [("whole book", whole)] if whole else []

    if len(bodies) <= SAMPLE_TARGET:
        return bodies

    # Spread across the book: the front, the back and evenly spaced middles.
    picks = {0, len(bodies) - 1}
    step = len(bodies) / (SAMPLE_TARGET - 1)
    picks.update(int(i * step) for i in range(1, SAMPLE_TARGET - 1))
    return [bodies[i] for i in sorted(picks)]


def decide(
    chapters: list[dict],
    metadata_language: str = "",
    override: str = "",
) -> LanguageDecision:
    """Choose the book's language from its own text, or ask for help."""
    meta_code = normalise_code(metadata_language)
    decision = LanguageDecision(metadata_language=meta_code)

    passages = gather_samples(chapters)
    decision.coverage_chars = sum(len(t) for _, t in passages)

    for where, text in passages:
        code, confidence = classify(text)
        decision.samples.append(Sample(where=where, chars=len(text),
                                       language=code, confidence=confidence))

    # An explicit choice wins outright, but the evidence stays visible so a
    # mistaken override is still discoverable later.
    if override:
        code = normalise_code(override)
        decision.language, decision.method, decision.confidence = code, "override", 1.0
        if code not in SUPPORTED:
            decision.warnings.append(
                f"'{code}' is outside the supported set {', '.join(SUPPORTED)}"
            )
        agreed = [s for s in decision.samples if s.language == code]
        if decision.samples and not agreed:
            decision.warnings.append(
                f"the text looks like "
                f"{', '.join(sorted({s.language for s in decision.samples if s.language}))}, "
                f"not the requested {code}"
            )
        return decision

    if not passages or decision.coverage_chars < MIN_TOTAL_CHARS:
        decision.needs_review = True
        decision.review_reasons.append(
            f"only {decision.coverage_chars} characters of text to judge from, "
            f"below the {MIN_TOTAL_CHARS} needed; set the language explicitly"
        )
        if meta_code in SUPPORTED:
            decision.warnings.append(f"metadata claims '{meta_code}'")
        return decision

    confident = [s for s in decision.samples if s.confidence >= CONFIDENCE_THRESHOLD]
    if not confident:
        decision.needs_review = True
        best = max(decision.samples, key=lambda s: s.confidence, default=None)
        decision.review_reasons.append(
            "no passage was clear enough to call"
            + (f"; the strongest was '{best.language}' at {best.confidence}"
               if best else "")
        )
        return decision

    found = {s.language for s in confident}
    if len(found) > 1:
        decision.needs_review = True
        decision.review_reasons.append(
            f"passages disagree: {', '.join(sorted(found))}. A book that changes "
            f"language partway needs an explicit choice"
        )
        return decision

    code = found.pop()
    if code not in SUPPORTED:
        decision.language, decision.needs_review = code, True
        decision.review_reasons.append(
            f"the text reads as '{code}', which is outside {', '.join(SUPPORTED)}. "
            f"Set the language explicitly to narrate it anyway"
        )
        return decision

    decision.language, decision.method = code, "content"
    decision.confidence = round(
        sum(s.confidence for s in confident) / len(confident), 4)

    # Metadata is cross-checked, never trusted over the book's own words: an
    # EPUB converted from a Polish source often keeps the template's `en`.
    if meta_code and meta_code != code:
        decision.warnings.append(
            f"metadata claims '{meta_code}' but the text reads as '{code}'; "
            f"the text was believed"
        )
    return decision
