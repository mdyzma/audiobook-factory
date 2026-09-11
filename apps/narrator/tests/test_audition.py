"""Judging a voice on the book it will actually read.

The audition made while cloning says one fixed sentence at one fixed
temperature and records neither. It answers "did the clone work", which is
worth knowing once. It says nothing about whether this voice will read this
book well enough to spend a night on it.

Rendering needs a model, so what is pinned here is everything around it: which
passage gets chosen, and that a sample can be told apart from every other
sample by what produced it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import typer

from narrator.audition import (
    PASSAGE_MAX_CHARS,
    PASSAGE_MIN_CHARS,
    key_for,
    passage,
)


def write_chunks(root, slug, chunks):
    folder = root / "data" / "book" / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "chunks.jsonl").write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks),
        encoding="utf-8")
    return root


def prose(n, role="narrator", chars=300):
    return {"id": f"ch001_{n:04d}", "chapter_title": "Przybysz", "language": "pl",
            "role": role, "text": "Ocean falował pod stacją. " * (chars // 25)}


class TestChoosingWhatToSay:
    def test_it_takes_something_from_the_middle(self, tmp_path):
        # Not the opening: that is a title or front matter as often as prose,
        # and neither says much about how a chapter will sound.
        write_chunks(tmp_path, "solaris", [prose(i) for i in range(9)])
        chosen = passage(tmp_path, "solaris")
        assert chosen["id"] not in ("ch001_0000", "ch001_0008")

    def test_it_prefers_a_passage_long_enough_to_judge(self, tmp_path):
        write_chunks(tmp_path, "solaris", [
            {"id": "a", "text": "Tak.", "language": "pl", "role": "narrator"},
            {"id": "b", "text": "Nie.", "language": "pl", "role": "narrator"},
            prose(3),
            {"id": "c", "text": "Może.", "language": "pl", "role": "narrator"},
        ])
        assert PASSAGE_MIN_CHARS <= len(passage(tmp_path, "solaris")["text"])

    def test_it_does_not_pick_half_a_chapter(self, tmp_path):
        write_chunks(tmp_path, "solaris", [prose(i) for i in range(5)])
        assert len(passage(tmp_path, "solaris")["text"]) <= PASSAGE_MAX_CHARS

    def test_a_dialogue_voice_is_heard_saying_dialogue(self, tmp_path):
        write_chunks(tmp_path, "solaris", [
            prose(0), prose(1),
            prose(2, role="dialogue"), prose(3, role="dialogue"),
        ])
        assert passage(tmp_path, "solaris", "dialogue")["role"] == "dialogue"

    def test_a_role_nothing_matches_falls_back_rather_than_failing(self, tmp_path):
        write_chunks(tmp_path, "solaris", [prose(0), prose(1), prose(2)])
        assert passage(tmp_path, "solaris", "narrator-2")["text"]

    def test_a_book_with_no_fragments_says_what_to_run(self, tmp_path):
        with pytest.raises(typer.BadParameter, match="just chunk"):
            passage(tmp_path, "never-chunked")

    def test_an_empty_fragment_file_says_so(self, tmp_path):
        write_chunks(tmp_path, "solaris", [])
        with pytest.raises(typer.BadParameter, match="no fragments"):
            passage(tmp_path, "solaris")

    def test_a_damaged_line_does_not_stop_it(self, tmp_path):
        folder = tmp_path / "data" / "book" / "solaris"
        folder.mkdir(parents=True)
        (folder / "chunks.jsonl").write_text(
            json.dumps(prose(0)) + "\n{ not json\n" + json.dumps(prose(1)) + "\n",
            encoding="utf-8")
        assert passage(tmp_path, "solaris")["text"]


class TestTellingSamplesApart:
    """A sample you cannot attribute is an opinion you cannot act on."""

    # Annotated, or the mixed value types make every spread call ambiguous.
    BASE: dict[str, Any] = dict(
        text="Ocean falował.", language="pl", voice="michal",
        revision="abc123", settings={"temperature": 0.7}, model="xtts-v2")

    def test_the_same_request_gives_the_same_name(self):
        assert key_for(**self.BASE) == key_for(**self.BASE)

    @pytest.mark.parametrize("field,value", [
        ("text", "Morze falowało."),
        ("language", "en"),
        ("voice", "ala"),
        ("revision", "def456"),
        ("settings", {"temperature": 0.9}),
        ("model", "chatterbox"),
    ])
    def test_anything_that_shaped_it_changes_the_name(self, field, value):
        assert key_for(**{**self.BASE, field: value}) != key_for(**self.BASE)

    def test_re_levelling_a_voice_gives_a_new_sample(self):
        # The revision covers the profile, and the level correction lives
        # there, so a re-levelled voice is heard again rather than reused.
        assert key_for(**{**self.BASE, "revision": "after-levelling"}) != \
            key_for(**self.BASE)

    def test_settings_order_does_not_matter(self):
        one = key_for(**{**self.BASE, "settings": {"temperature": 0.7, "speed": 1.0}})
        other = key_for(**{**self.BASE, "settings": {"speed": 1.0, "temperature": 0.7}})
        assert one == other
