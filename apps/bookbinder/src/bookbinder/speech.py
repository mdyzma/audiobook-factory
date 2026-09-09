"""Turn extracted text into the words a narrator should actually say.

Three representations exist and each is needed. The source bytes are what
arrived. The extracted text is what the book says, and is what a reader
proofreads against. The spoken text is what the model is given, and differs
wherever an abbreviation, a number or a name is written one way and said
another.

Every change is recorded with its position, so the spoken form can always be
traced back to the spelling it came from. Quality checking needs that: an ASR
pass hears "doktor" and must not report it as a mismatch against "dr.".

The rules here are deliberately conservative. Polish inflects numerals for case
and gender, so "3 koty" and "o 3 kotach" need different words, and a rule that
cannot tell them apart makes the narration worse rather than better. Anything
that cannot be got right from the text alone is left for the per-book
dictionary, where a person decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Abbreviations that are read as a different word. Written without their
# trailing dot, which the pattern adds. Longest first at match time, so `m.in.`
# is not broken up by a shorter entry.
#
# Left out on purpose: `r.` and `w.` after a year, which are usually silent in
# Polish narration; `s.` and `nr`, which vary by publisher; and anything that
# is also an ordinary word.
ABBREVIATIONS: dict[str, dict[str, str]] = {
    "pl": {
        "dr": "doktor",
        "prof": "profesor",
        "inż": "inżynier",
        "mgr": "magister",
        "np": "na przykład",
        "itd": "i tak dalej",
        "itp": "i tym podobne",
        "m.in": "między innymi",
        "tzn": "to znaczy",
        "tzw": "tak zwany",
        "ok": "około",
        "godz": "godzina",
        "ul": "ulica",
        "al": "aleja",
        "św": "święty",
        "ks": "ksiądz",
        "por": "porównaj",
        "cd": "ciąg dalszy",
    },
    "en": {
        "Dr": "Doctor",
        "Mr": "Mister",
        "Mrs": "Missus",
        "Ms": "Miz",
        "Prof": "Professor",
        "St": "Saint",
        "etc": "et cetera",
        "e.g": "for example",
        "i.e": "that is",
        "vs": "versus",
        "approx": "approximately",
    },
}

# Written forms that are read as words rather than letters, and are unambiguous
# enough to expand without knowing the grammar around them.
SYMBOLS: dict[str, dict[str, str]] = {
    "pl": {"%": "procent", "&": "i", "§": "paragraf", "°C": "stopni Celsjusza"},
    "en": {"%": "percent", "&": "and", "§": "section", "°C": "degrees Celsius"},
}


@dataclass
class Substitution:
    """One change, with its span on both sides of it.

    `start`/`end` index the extracted text and `spoken_start`/`spoken_end` the
    prepared text, which is what makes the two representations navigable in
    either direction after the lengths have diverged.
    """

    kind: str                  # abbreviation | symbol | dictionary
    source: str
    spoken: str
    start: int
    end: int
    spoken_start: int = 0
    spoken_end: int = 0


@dataclass
class Prepared:
    """The spoken form of one passage, and how it got there."""

    text: str
    source_text: str
    substitutions: list[Substitution] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.text != self.source_text

    def source_offset(self, spoken_offset: int) -> int:
        """Where a position in the spoken text sits in the extracted text.

        Inside a substitution the whole replacement maps to the start of what
        it replaced: "doktor" came from "dr." as a unit and no finer
        correspondence exists.
        """
        drift = 0
        for sub in self.substitutions:
            if spoken_offset < sub.spoken_start:
                break
            if spoken_offset < sub.spoken_end:
                return sub.start
            drift += (sub.end - sub.start) - (sub.spoken_end - sub.spoken_start)
        return min(len(self.source_text), spoken_offset + drift)


def split_provenance(prepared: Prepared, pieces: list[str]) -> list[Prepared]:
    """Attribute a prepared passage's changes to the pieces it was split into.

    Chunking happens on the spoken text, because that is what the model has to
    fit. Each resulting fragment still needs to know the spelling it came from,
    which is this.
    """
    out: list[Prepared] = []
    cursor = 0
    for piece in pieces:
        found = prepared.text.find(piece, cursor)
        # Packing rejoins on single spaces, so a piece is always present. If a
        # future change breaks that, fall back to the piece standing alone
        # rather than mis-attributing someone else's substitutions to it.
        if found < 0:
            out.append(Prepared(text=piece, source_text=piece))
            continue
        start, end = found, found + len(piece)
        cursor = end

        inside = [s for s in prepared.substitutions
                  if s.spoken_start >= start and s.spoken_end <= end]
        source_start = prepared.source_offset(start)
        source_end = prepared.source_offset(end)
        out.append(Prepared(
            text=piece,
            source_text=prepared.source_text[source_start:source_end],
            substitutions=[
                Substitution(kind=s.kind, source=s.source, spoken=s.spoken,
                             start=s.start - source_start, end=s.end - source_start,
                             spoken_start=s.spoken_start - start,
                             spoken_end=s.spoken_end - start)
                for s in inside
            ],
        ))
    return out


def _abbreviation_pattern(table: dict[str, str]) -> re.Pattern[str]:
    """Match any abbreviation in `table`, longest first, followed by its dot.

    The trailing dot is required: `ok.` is "około" but `ok` on its own is a
    different word, and `St.` is "Saint" while `St` may be initials.
    """
    keys = sorted(table, key=len, reverse=True)
    alternation = "|".join(re.escape(k) for k in keys)
    # A preceding letter or digit rules out matching inside a longer word.
    return re.compile(rf"(?<![\w.])({alternation})\.", re.IGNORECASE)


_PATTERNS: dict[str, re.Pattern[str]] = {}


def _pattern_for(language: str) -> re.Pattern[str] | None:
    table = ABBREVIATIONS.get(language)
    if not table:
        return None
    if language not in _PATTERNS:
        _PATTERNS[language] = _abbreviation_pattern(table)
    return _PATTERNS[language]


def _match_case(source: str, spoken: str) -> str:
    """Keep a capitalised abbreviation capitalised once expanded."""
    if source[:1].isupper():
        return spoken[:1].upper() + spoken[1:]
    return spoken


def load_dictionary(path) -> dict[str, str]:
    """A per-book pronunciation dictionary: written form to spoken form.

    Read from `data/book/<slug>/pronunciation.yml` when it exists. This is
    where a name the rules cannot know about is settled by a person, and where
    anything the model reads badly can be corrected without touching the book.
    """
    from pathlib import Path

    path = Path(path)
    if not path.exists():
        return {}

    import yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must be a mapping of written form to spoken form")
    return {str(k): str(v) for k, v in loaded.items()}


def prepare(text: str, language: str, dictionary: dict[str, str] | None = None) -> Prepared:
    """Rewrite `text` as it should be spoken, recording every change.

    Positions in the returned substitutions refer to the original `text`, so
    they stay valid however much the replacements change its length.
    """
    edits: list[tuple[int, int, str, str, str]] = []  # start, end, source, spoken, kind

    # The book's own dictionary wins, so a name the rules would mangle can be
    # fixed once and stay fixed. Longest first, so a full name beats a surname.
    for written in sorted(dictionary or {}, key=len, reverse=True):
        spoken = (dictionary or {})[written]
        for match in re.finditer(rf"(?<!\w){re.escape(written)}(?!\w)", text):
            edits.append((match.start(), match.end(), match.group(), spoken, "dictionary"))

    pattern = _pattern_for(language)
    if pattern is not None:
        table = ABBREVIATIONS[language]
        lowered = {k.lower(): v for k, v in table.items()}
        for match in pattern.finditer(text):
            spoken = lowered.get(match.group(1).lower())
            if spoken is None:
                continue
            edits.append((match.start(), match.end(),
                          match.group(), _match_case(match.group(1), spoken),
                          "abbreviation"))

    for written, spoken in SYMBOLS.get(language, {}).items():
        for match in re.finditer(re.escape(written), text):
            # A symbol usually sits flush against what it qualifies, as in
            # "15%". Expanded without a space that becomes one unsayable word.
            before = text[match.start() - 1] if match.start() else " "
            padded = spoken if before.isspace() else f" {spoken}"
            edits.append((match.start(), match.end(), match.group(), padded, "symbol"))

    if not edits:
        return Prepared(text=text, source_text=text)

    # Apply left to right, dropping anything that overlaps an earlier edit so
    # a dictionary entry and an abbreviation cannot both rewrite one span.
    edits.sort(key=lambda e: (e[0], -(e[1] - e[0])))
    out: list[str] = []
    substitutions: list[Substitution] = []
    cursor = 0
    written = 0          # how much of the spoken text has been emitted
    for start, end, source, spoken, kind in edits:
        if start < cursor:
            continue
        out.append(text[cursor:start])
        written += start - cursor
        substitutions.append(Substitution(
            kind=kind, source=source, spoken=spoken, start=start, end=end,
            spoken_start=written, spoken_end=written + len(spoken),
        ))
        out.append(spoken)
        written += len(spoken)
        cursor = end
    out.append(text[cursor:])

    return Prepared(text="".join(out), source_text=text, substitutions=substitutions)
