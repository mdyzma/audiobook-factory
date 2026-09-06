"""Role assignment decides which voice speaks each paragraph.

The bias here is deliberate and worth pinning: narration mistaken for a
character voice is far more jarring than dialogue left in the narrator's
voice, so ambiguous cases must stay with the narrator.
"""

from __future__ import annotations

import pytest

from bookbinder.roles import (
    DEFAULT_DIALOGUE_ROLE,
    NARRATOR,
    assign_role,
    is_dialogue,
    normalise_speaker,
)


class TestIsDialogue:
    @pytest.mark.parametrize("text", [
        '"He said nothing."',
        "„Nie wiem.”",
        "— Witaj.",
        "- Witaj.",
        "«Bonjour»",
    ])
    def test_recognises_dialogue_openers(self, text):
        assert is_dialogue(text)

    @pytest.mark.parametrize("text", [
        "Zszedłem po drabince do kabiny.",
        "",
        "   ",
        "Ocean falował pod stacją.",
    ])
    def test_narration_is_not_dialogue(self, text):
        assert not is_dialogue(text)


class TestNormaliseSpeaker:
    def test_lowercases_and_hyphenates(self):
        assert normalise_speaker("Pani Kelvin") == "pani-kelvin"

    def test_strips_punctuation(self):
        assert normalise_speaker("Dr. Snaut") == "dr-snaut"

    def test_keeps_diacritics(self):
        assert normalise_speaker("Żmija") == "żmija"


class TestAssignRole:
    def test_plain_narration(self):
        a = assign_role("Zszedłem po drabince.")
        assert a.role == NARRATOR and not a.is_dialogue

    def test_unattributed_dialogue(self):
        a = assign_role("„Nie wiem, czy to możliwe.”")
        assert a.role == DEFAULT_DIALOGUE_ROLE and a.is_dialogue

    def test_named_speaker_in_cast_gets_their_voice(self):
        a = assign_role("Kelvin: — Wracam na Ziemię.", known_roles={"kelvin"})
        assert a.role == "kelvin"
        assert a.speaker == "kelvin"
        # The label is stripped so the voice does not read its own name aloud.
        assert a.text == "— Wracam na Ziemię."

    def test_named_speaker_not_in_cast_falls_back(self):
        a = assign_role("Snaut: — Nie ma nikogo.", known_roles={"kelvin"})
        assert a.role == DEFAULT_DIALOGUE_ROLE and a.is_dialogue

    def test_colon_in_narration_is_not_a_speaker(self):
        # "Uwaga: ..." must not become a character called `uwaga`.
        a = assign_role("Uwaga: to zdanie ma dwukropek, nie jest dialogiem.")
        assert a.role == NARRATOR and not a.is_dialogue

    def test_attribution_only_line_stays_with_narrator(self):
        # "— Witaj — powiedziała." is mostly narration despite the dash.
        a = assign_role("— Witaj — powiedziała.")
        assert a.role == NARRATOR

    def test_long_dialogue_with_attribution_is_still_dialogue(self):
        text = ("— Nie mam pojęcia, co się dzieje na tej stacji, i szczerze "
                "mówiąc wolałbym się nie dowiedzieć — powiedział cicho.")
        assert assign_role(text).is_dialogue

    def test_empty_paragraph(self):
        assert assign_role("").role == NARRATOR
