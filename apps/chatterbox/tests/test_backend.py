"""The second engine, tested for everything that does not need its weights.

Loading the model downloads it and wants a device, so what is pinned here is
the adapter: which reference clip a voice conditions on, which controls survive
the call, and that a language this engine cannot read is refused rather than
attempted. The synthesis itself belongs to B-5, on the machine that can run it.
"""

from __future__ import annotations

import json

import pytest

from chatterbox_backend import (
    CONTROLS,
    ENGINE,
    ChatterboxBackend,
    VoiceNotUsable,
    accepted,
    conditioning_key,
    ignored,
    reference_for,
)


def voice(root, name="michal", clips=(("seg_0000.wav", 2000),)):
    folder = root / "data" / "datasets" / name / "wavs"
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, size in clips:
        (folder / filename).write_bytes(b"RIFF" + b"\x00" * size)
        written.append(f"data/datasets/{name}/wavs/{filename}")
    (root / "data" / "voices").mkdir(parents=True, exist_ok=True)
    (root / "data" / "voices" / f"{name}.json").write_text(json.dumps({
        "name": name, "language": "pl", "reference_wavs": written,
    }), encoding="utf-8")
    return root


class TestChoosingWhatToCloneFrom:
    """Chatterbox conditions on one clip, so the choice is part of the voice."""

    def test_the_only_clip_is_the_one(self, tmp_path):
        voice(tmp_path)
        assert reference_for(tmp_path, "michal").name == "seg_0000.wav"

    def test_the_longest_clip_wins(self, tmp_path):
        voice(tmp_path, clips=(("a.wav", 1000), ("b.wav", 9000), ("c.wav", 3000)))
        assert reference_for(tmp_path, "michal").name == "b.wav"

    def test_a_tie_is_broken_the_same_way_every_time(self, tmp_path):
        # Stability matters more than which one: "the best clip" would drift
        # between runs and quietly change what the voice sounds like.
        voice(tmp_path, clips=(("z.wav", 5000), ("a.wav", 5000)))
        first = reference_for(tmp_path, "michal")
        assert first.name == "a.wav"
        assert reference_for(tmp_path, "michal") == first

    def test_a_voice_with_no_profile_says_so(self, tmp_path):
        with pytest.raises(VoiceNotUsable, match="no voice profile"):
            reference_for(tmp_path, "nobody")

    def test_a_profile_whose_clips_are_gone_says_what_to_run(self, tmp_path):
        voice(tmp_path)
        (tmp_path / "data/datasets/michal/wavs/seg_0000.wav").unlink()
        with pytest.raises(VoiceNotUsable, match="just label"):
            reference_for(tmp_path, "michal")


class TestTheConditioningIsPerEngine:
    """Two backends caching under one key is how a voice sounds like the other
    engine's idea of it."""

    def test_the_key_follows_the_audio(self, tmp_path):
        voice(tmp_path)
        before = conditioning_key(tmp_path, "michal")
        (tmp_path / "data/datasets/michal/wavs/seg_0000.wav").write_bytes(b"RIFFdifferent")
        assert conditioning_key(tmp_path, "michal") != before

    def test_the_same_audio_gives_the_same_key(self, tmp_path):
        voice(tmp_path)
        assert conditioning_key(tmp_path, "michal") == conditioning_key(tmp_path, "michal")

    def test_the_engine_is_part_of_the_key(self, tmp_path):
        # So that XTTS and this one cannot collide in a shared cache.
        voice(tmp_path)
        import hashlib

        reference = reference_for(tmp_path, "michal")
        without_engine = hashlib.sha256(
            reference.name.encode() + reference.read_bytes()).hexdigest()[:32]
        assert conditioning_key(tmp_path, "michal") != without_engine


class TestTheControlsThisEngineHas:
    def test_its_own_controls_are_passed(self):
        assert accepted({"exaggeration": 0.7, "cfg_weight": 0.3}) == \
            {"exaggeration": 0.7, "cfg_weight": 0.3}

    def test_a_control_it_does_not_have_is_not_passed(self):
        # A keyword it does not take would raise; silently dropping it would be
        # worse, so it is filtered here and named below.
        assert accepted({"temperature": 0.8, "speed": 1.2}) == {"temperature": 0.8}

    def test_what_was_left_out_is_named(self):
        # A setting quietly ignored looks like one that had no effect, and that
        # difference is the whole point of comparing two engines.
        assert ignored({"temperature": 0.8, "speed": 1.2, "length_penalty": 1.0}) == \
            ["length_penalty", "speed"]

    def test_nothing_is_left_out_when_nothing_should_be(self):
        assert ignored({c: 0.5 for c in CONTROLS}) == []

    def test_temperature_is_shared_with_xtts(self):
        # The one control both engines take, so a book moved between them keeps
        # at least that much meaning.
        assert "temperature" in CONTROLS


class TestRefusingWhatItCannotDo:
    def test_the_engine_names_itself(self):
        assert ChatterboxBackend.engine == ENGINE == "chatterbox"

    def test_both_of_this_projects_languages_are_supported(self):
        backend = ChatterboxBackend.__new__(ChatterboxBackend)
        assert backend.supports("pl") and backend.supports("en")

    def test_a_language_it_cannot_read_is_refused(self, tmp_path):
        voice(tmp_path)
        backend = ChatterboxBackend(tmp_path)
        with pytest.raises(VoiceNotUsable, match="does not read"):
            backend.speak("Cokolwiek.", "cy", "michal", {})
