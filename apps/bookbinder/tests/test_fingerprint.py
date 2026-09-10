"""What makes rendered audio stale, and the mirror that keeps two environments
agreeing about it.

Resume skips fragments that already have a wav. Before fingerprints that rule
was the filename alone, so changing the model and re-running kept every
existing fragment and reported a clean run. These pin the inputs that must
invalidate audio, and the ones that must not.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from bookbinder.fingerprint import (
    RENDERER_VERSION,
    fragment_fingerprint,
    voice_revision,
)

BASE: dict[str, Any] = dict(
    text="Ocean falował pod stacją.",
    language="pl",
    model="xtts-v2@abc123",
    voice="michal",
    voice_revision="deadbeef",
    settings={"temperature": 0.7, "speed": 1.0},
)


def fp(**overrides) -> str:
    return fragment_fingerprint(**{**BASE, **overrides})


class TestWhatMakesAudioStale:
    def test_the_same_request_gives_the_same_fingerprint(self):
        assert fp() == fp()

    @pytest.mark.parametrize("field,value", [
        ("text", "Ocean falował pod stacją badawczą."),
        ("language", "en"),
        ("voice", "kelvin"),
        ("voice_revision", "cafebabe"),
    ])
    def test_changing_the_request_invalidates_it(self, field, value):
        assert fp(**{field: value}) != fp()

    def test_changing_the_model_invalidates_it(self):
        # The one slice B made easy and this exists to make safe.
        assert fp(model="chatterbox-multilingual") != fp()

    def test_the_same_model_at_a_new_revision_is_a_different_model(self):
        assert fp(model="xtts-v2@abc123") != fp(model="xtts-v2@def456")

    def test_changing_a_setting_invalidates_it(self):
        assert fp(settings={"temperature": 0.9, "speed": 1.0}) != fp()

    def test_dropping_a_setting_invalidates_it(self):
        assert fp(settings={"temperature": 0.7}) != fp()

    def test_bumping_the_renderer_invalidates_everything(self):
        assert fp(renderer_version=RENDERER_VERSION + 1) != fp()


class TestWhatMustNotInvalidateAudio:
    """Re-rendering a twenty-hour book because a dict was ordered differently
    is its own kind of failure."""

    def test_setting_order_does_not_matter(self):
        assert fp(settings={"speed": 1.0, "temperature": 0.7}) == fp()

    def test_an_integer_and_its_float_are_one_value(self):
        assert fp(settings={"temperature": 0.7, "speed": 1}) == fp()

    def test_the_fingerprint_is_short_enough_to_read(self):
        assert len(fp()) == 32 and fp().isalnum()


class TestVoiceRevision:
    def test_a_voice_with_nothing_on_disk_still_has_an_identity(self, tmp_path):
        assert voice_revision(tmp_path, "absent")

    def test_re_cloning_a_voice_changes_it(self, tmp_path):
        voices = tmp_path / "data" / "voices" / "michal"
        voices.mkdir(parents=True)
        profile = tmp_path / "data" / "voices" / "michal.json"
        profile.write_text('{"name": "michal"}', encoding="utf-8")
        latents = voices / "latents.pt"
        latents.write_bytes(b"first clone")

        before = voice_revision(tmp_path, "michal")
        latents.write_bytes(b"second clone, different reference clips")
        assert voice_revision(tmp_path, "michal") != before

    def test_editing_the_profile_changes_it(self, tmp_path):
        (tmp_path / "data" / "voices").mkdir(parents=True)
        profile = tmp_path / "data" / "voices" / "michal.json"
        profile.write_text('{"name": "michal", "mode": "instant"}', encoding="utf-8")

        before = voice_revision(tmp_path, "michal")
        profile.write_text('{"name": "michal", "mode": "finetuned"}', encoding="utf-8")
        assert voice_revision(tmp_path, "michal") != before

    def test_two_voices_are_not_the_same(self, tmp_path):
        (tmp_path / "data" / "voices").mkdir(parents=True)
        for name in ("a", "b"):
            (tmp_path / "data" / "voices" / f"{name}.json").write_text(
                f'{{"name": "{name}"}}', encoding="utf-8")
        assert voice_revision(tmp_path, "a") != voice_revision(tmp_path, "b")


class TestTheNarratorMirrorAgrees:
    """narrator writes these fingerprints and bookbinder checks them.

    They are separate environments, so the only thing keeping the two copies
    of the algorithm honest is this. The module is stdlib-only precisely so
    that it can be loaded from here.
    """

    @pytest.fixture
    def mirror(self):
        # tests/ -> bookbinder/ -> apps/
        path = (Path(__file__).resolve().parents[2] / "narrator" / "src"
                / "narrator" / "fingerprint.py")
        assert path.exists(), f"no mirror at {path}"
        spec = importlib.util.spec_from_file_location("narrator_fingerprint", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_renderer_version_matches(self, mirror):
        assert mirror.RENDERER_VERSION == RENDERER_VERSION

    def test_both_sides_produce_the_same_fingerprint(self, mirror):
        assert mirror.fragment_fingerprint(**BASE) == fragment_fingerprint(**BASE)

    @pytest.mark.parametrize("field,value", [
        ("text", "Zupełnie inne zdanie, dłuższe niż poprzednie."),
        ("language", "en"),
        ("model", "chatterbox-multilingual@v1"),
        ("voice", "kelvin"),
        ("settings", {"temperature": 0.9}),
        ("settings", {}),
    ])
    def test_they_agree_across_varied_inputs(self, mirror, field, value):
        request = {**BASE, field: value}
        assert mirror.fragment_fingerprint(**request) == fragment_fingerprint(**request)

    def test_they_agree_on_non_ascii_text(self, mirror):
        # Encoding differences between the two copies would show up here first.
        request = {**BASE, "text": "Zażółć gęślą jaźń — „cytat” i wielokropek…"}
        assert mirror.fragment_fingerprint(**request) == fragment_fingerprint(**request)

    def test_voice_revision_agrees(self, mirror, tmp_path):
        (tmp_path / "data" / "voices").mkdir(parents=True)
        (tmp_path / "data" / "voices" / "michal.json").write_text(
            '{"name": "michal"}', encoding="utf-8")
        assert mirror.voice_revision(tmp_path, "michal") == \
            voice_revision(tmp_path, "michal")
