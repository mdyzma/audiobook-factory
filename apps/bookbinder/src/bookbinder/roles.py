"""Decide who speaks each paragraph.

A single narrator voice for an entire novel flattens every conversation. This
assigns a *role* to each paragraph, which `config/cast.yml` then maps to a
voice. Roles are inferred from typography, not from understanding: quotation
marks and dashes mark dialogue, and an explicit "Name:" prefix names a speaker.

Deliberately conservative. Mis-assigning narration to a character voice is far
more jarring than leaving dialogue in the narrator's voice, so anything
ambiguous stays with the narrator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NARRATOR = "narrator"
DEFAULT_DIALOGUE_ROLE = "dialogue"

# Dialogue openers across the conventions this is likely to meet. Polish and
# other European typography uses a dash; English uses quotation marks.
DIALOGUE_OPENERS = ('"', "'", "„", "“", "”", "«", "»", "—", "–", "- ", "‒")

# "ANNA:" or "Anna:" at the start of a line names its speaker outright. Allow
# Polish and other Latin diacritics; cap the length so a sentence that merely
# contains a colon is not mistaken for a speaker label.
SPEAKER_LABEL = re.compile(
    r"^(?P<name>[A-ZÀ-ÖØ-ÞĄĆĘŁŃÓŚŹŻ][\w'\-\. ]{0,30}?)\s*:\s+(?P<said>\S.*)$",
    re.UNICODE,
)

# A narration tag closing a line of dialogue: `— powiedziała Anna.`
ATTRIBUTION = re.compile(
    r"(powiedzia\w+|rzek\w+|odpar\w+|szepn\w+|krzykn\w+|zapyta\w+|mrukn\w+"
    r"|said|asked|replied|whispered|shouted|muttered)",
    re.IGNORECASE | re.UNICODE,
)


@dataclass(frozen=True)
class RoleAssignment:
    role: str
    is_dialogue: bool
    speaker: str | None = None
    text: str | None = None  # set when a speaker label was stripped


def is_dialogue(paragraph: str) -> bool:
    text = paragraph.strip()
    if not text:
        return False
    if text.startswith(DIALOGUE_OPENERS):
        return True
    return SPEAKER_LABEL.match(text) is not None


def normalise_speaker(name: str) -> str:
    """`Pani Kelvin` -> `pani-kelvin`, so cast.yml keys are predictable."""
    slug = re.sub(r"[^\w\s-]", "", name.strip().lower(), flags=re.UNICODE)
    return re.sub(r"[\s_]+", "-", slug).strip("-")


def assign_role(paragraph: str, known_roles: set[str] | None = None) -> RoleAssignment:
    """Work out who says this paragraph.

    `known_roles` is the cast from config. A speaker label naming someone in
    the cast gets their voice; anyone else falls back to the generic dialogue
    role rather than inventing a voice that does not exist.
    """
    text = paragraph.strip()
    if not text:
        return RoleAssignment(NARRATOR, False)

    label = SPEAKER_LABEL.match(text)
    if label:
        speaker = normalise_speaker(label.group("name"))
        said = label.group("said").strip()
        # Only treat it as a speaker label when the name is in the cast, or the
        # line also looks like dialogue. Otherwise "Uwaga: to nie dialog" would
        # become a character.
        if (known_roles and speaker in known_roles) or said.startswith(DIALOGUE_OPENERS):
            role = speaker if (known_roles and speaker in known_roles) else DEFAULT_DIALOGUE_ROLE
            return RoleAssignment(role, True, speaker=speaker, text=said)

    if text.startswith(DIALOGUE_OPENERS):
        # A line that is mostly a narration tag reads better in the narrator's
        # voice, even though it opens with a dash.
        if len(text) < 60 and ATTRIBUTION.search(text):
            return RoleAssignment(NARRATOR, False)
        return RoleAssignment(DEFAULT_DIALOGUE_ROLE, True)

    return RoleAssignment(NARRATOR, False)
