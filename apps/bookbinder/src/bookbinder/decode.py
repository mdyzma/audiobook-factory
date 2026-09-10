"""Turn source bytes into accepted text, or refuse and say why.

No model can recover a letter lost during import, so nothing here decodes with
`errors="replace"` or `"ignore"`. A file whose encoding cannot be established
is marked `needs_review` and left for a person, while the rest of a batch
carries on.

The decision is deliberately not left to a charset detector. On genuine
ISO-8859-2 Polish, `charset-normalizer` picks `iso8859_10`, which decodes
without error and silently changes the diacritics. A detector score is
evidence, not a verdict: every candidate is decoded strictly, the results are
scored for how plausible their characters are, and the detector only breaks
ties among candidates that are otherwise equal.

Accepted text is stored as NFC UTF-8.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# Tried in this order once BOMs and strict UTF-8 are ruled out. Windows-1250
# and ISO-8859-2 are the two that carry legacy Polish; Windows-1252 is here
# because a western-European export of an English book is common and decodes
# as something else entirely under the Polish tables.
LEGACY_CANDIDATES = ("cp1250", "iso-8859-2", "cp1252")

# Bumped when these rules change, so a stored decision says which version made
# it. Independent of the manifest schema version: the rules can improve without
# the file shape moving.
DECODER_VERSION = 1

# Byte-order marks, longest first: the UTF-32 marks start with the UTF-16 ones.
BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

# Polish letters that a wrong single-byte table turns into something else.
PL_LETTERS = frozenset("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")

# Non-ASCII characters that belong in a Polish or English book: the Polish
# letters, the accented letters that appear in names and loanwords, and the
# typography converters emit.
#
# Everything else non-ASCII is evidence of a wrong table, and this is the check
# that does the real work. Reading ISO-8859-2 Polish through Windows-1250 turns
# `ą` into `±` and `ś` into `¶`, which are punctuation rather than letters, so a
# test that only looked at alphabetic characters scored both readings perfect
# and picked whichever came first.
ALLOWED_NON_ASCII = frozenset(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ"
    "áàâäãåéèêëíìîïòôöõøúùûüçñýÿæœßÁÀÂÄÃÅÉÈÊËÍÌÎÏÒÔÖÕØÚÙÛÜÇÑÝÆŒ"
    "–—…„“”‘’‚«»€£"
    " ­﻿"
)

# Weight for a non-ASCII character that has no business in the text. Decisive
# on its own, because correct prose produces none at all.
FOREIGN_CHAR_WEIGHT = 20.0

# The C1 block. Real prose never contains these, but reading Windows-1250 bytes
# through ISO-8859-2 produces them, which is what makes them the strongest
# single signal that a single-byte decode used the wrong table.
C1_CONTROLS = frozenset(chr(c) for c in range(0x80, 0xA0))

# Weights for `text_quality`. Only their relative size matters: a control
# character is decisive, an unexpected letter is strong evidence, and the
# proportion of expected letters breaks what is left.
UNEXPECTED_LETTER_WEIGHT = 4.0
CONTROL_WEIGHT = 50.0
REPLACEMENT_WEIGHT = 100.0

# Two candidates scoring within this of each other are treated as equally
# plausible. It only matters when they also produce different text.
TIE_MARGIN = 0.05

# Below this, the penalties outweighed everything that looked like text, which
# real prose never manages: a correct reading scores close to 1. Reached only
# when the file is not a book, or is one this decoder cannot read.
POOR_QUALITY = 0.0


@dataclass
class Candidate:
    encoding: str
    text: str
    score: float
    detector_rank: int | None = None


@dataclass
class Decoded:
    """The decoding decision, kept whole so it can be shown and stored."""

    text: str
    encoding: str
    method: str                       # override | bom | utf-8 | quality | ascii
    score: float = 0.0
    sha256: str = ""
    equivalent: list[str] = field(default_factory=list)
    alternatives: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)


class UndecodableSource(ValueError):
    """No candidate encoding produced usable text."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def detect_bom(raw: bytes) -> str | None:
    """The encoding a byte-order mark declares, if there is one."""
    for mark, encoding in BOMS:
        if raw.startswith(mark):
            return encoding
    return None


def text_quality(text: str) -> float:
    """How plausible this text is as Polish or English prose.

    Higher is better. This is what separates Windows-1250 from ISO-8859-2 on
    the same bytes, which no amount of detector confidence does reliably.
    """
    if not text:
        return 0.0
    letters = [c for c in text if c.isalpha()]
    controls = sum(1 for c in text if c in C1_CONTROLS)
    replacements = text.count("�")
    foreign = sum(1 for c in text
                  if not c.isascii() and c not in ALLOWED_NON_ASCII
                  and c not in C1_CONTROLS and c != "�")

    penalty = (CONTROL_WEIGHT * controls
               + REPLACEMENT_WEIGHT * replacements
               + FOREIGN_CHAR_WEIGHT * foreign) / len(text)
    if not letters:
        return -penalty

    expected = sum(1 for c in letters if c.isascii() or c in PL_LETTERS)
    unexpected = len(letters) - expected
    return (expected / len(letters)
            - UNEXPECTED_LETTER_WEIGHT * (unexpected / len(letters))
            - penalty)


def normalise_unicode(text: str) -> str:
    """NFC, and without the BOM that `utf-8-sig` leaves when it is mid-file."""
    return unicodedata.normalize("NFC", text.replace("﻿", ""))


def encoding_family(name: str) -> str:
    """`utf-8-sig` and `utf-8` are the same family; `utf-16` is a different one.

    A byte-order mark only conflicts with an override across families. Reading
    UTF-8-with-BOM as plain UTF-8 is not a disagreement, because the mark is
    stripped either way.
    """
    normalised = name.lower().replace("_", "-").replace("-sig", "")
    for family in ("utf-32", "utf-16", "utf-8"):
        if normalised.startswith(family):
            return family
    return normalised


def _try(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def _detector_ranking(raw: bytes) -> list[str]:
    """Encodings charset-normalizer considers plausible, best first.

    Used only to order otherwise-equal candidates. Its top pick is not trusted
    on its own; see the module docstring.
    """
    try:
        from charset_normalizer import from_bytes

        return [m.encoding for m in from_bytes(raw) if m.encoding]
    except Exception:  # a detector failure must not stop an import
        return []


def decode_bytes(raw: bytes, override: str = "") -> Decoded:
    """Decide how to read `raw`, preserving every character or refusing."""
    digest = sha256_bytes(raw)
    warnings: list[str] = []
    bom = detect_bom(raw)

    if override:
        text = _try(raw, override)
        if text is None:
            raise UndecodableSource(
                f"the file is not valid {override}; it was given explicitly, so "
                f"nothing was guessed in its place"
            )
        if bom and encoding_family(bom) != encoding_family(override):
            warnings.append(
                f"a {bom} byte-order mark disagrees with the requested {override}"
            )
        return Decoded(text=normalise_unicode(text), encoding=override,
                       method="override", sha256=digest, warnings=warnings)

    if bom:
        text = _try(raw, bom)
        if text is not None:
            return Decoded(text=normalise_unicode(text), encoding=bom,
                           method="bom", sha256=digest)
        warnings.append(f"a {bom} byte-order mark is present but the file is not {bom}")

    # Strict UTF-8. A file that decodes cleanly as UTF-8 is essentially never
    # anything else: the multi-byte sequences are too constrained to appear by
    # accident in single-byte text.
    utf8 = _try(raw, "utf-8")
    if utf8 is not None:
        if raw.isascii():
            return Decoded(text=normalise_unicode(utf8), encoding="utf-8",
                           method="ascii", sha256=digest,
                           equivalent=["utf-8", *LEGACY_CANDIDATES],
                           warnings=warnings)
        return Decoded(text=normalise_unicode(utf8), encoding="utf-8",
                       method="utf-8", sha256=digest, warnings=warnings)

    ranking = _detector_ranking(raw)
    candidates: list[Candidate] = []
    for encoding in LEGACY_CANDIDATES:
        text = _try(raw, encoding)
        if text is None:
            continue
        rank = next((i for i, e in enumerate(ranking)
                     if e.replace("_", "-") == encoding.replace("_", "-")), None)
        candidates.append(Candidate(encoding=encoding, text=normalise_unicode(text),
                                    score=text_quality(text), detector_rank=rank))

    if not candidates:
        raise UndecodableSource(
            "the file is not valid UTF-8 and none of "
            f"{', '.join(LEGACY_CANDIDATES)} could read it either"
        )

    # Best quality first; the detector's opinion only orders equals.
    candidates.sort(key=lambda c: (-c.score, c.detector_rank
                                   if c.detector_rank is not None else 99))
    best = candidates[0]

    # Candidates that produced exactly the same text are not a disagreement.
    # Plenty of files are ASCII apart from a handful of bytes, and claiming to
    # have identified one encoding out of several identical readings would be
    # inventing certainty.
    equivalent = [c.encoding for c in candidates if c.text == best.text]
    rivals = [c for c in candidates
              if c.text != best.text and best.score - c.score <= TIE_MARGIN]

    decoded = Decoded(
        text=best.text, encoding=best.encoding, method="quality",
        score=round(best.score, 4), sha256=digest, equivalent=equivalent,
        alternatives=[c for c in candidates if c.encoding not in equivalent],
        warnings=warnings,
    )
    if rivals:
        decoded.needs_review = True
        decoded.review_reasons.append(
            f"{best.encoding} and {', '.join(c.encoding for c in rivals)} are "
            f"equally plausible and disagree about the text; choose one with "
            f"--encoding"
        )

    # Being the only reading that did not raise is not the same as being right.
    # A file of bytes that only ISO-8859-2 accepts decodes to a page of control
    # characters, wins by default because nothing else is competing, and
    # otherwise sails through as a book.
    if best.score < POOR_QUALITY:
        decoded.needs_review = True
        decoded.review_reasons.append(
            f"read as {best.encoding} because nothing else could read it at all, "
            f"but the result does not look like text (quality {best.score:.2f}). "
            f"Check the file is a book and not something else"
        )
    return decoded


def decode_file(path: Path, override: str = "") -> Decoded:
    """Read one file, preserving its bytes' identity alongside the text."""
    return decode_bytes(path.read_bytes(), override)
