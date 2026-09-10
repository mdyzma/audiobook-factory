"""Choosing a synthesis backend, and refusing to choose wrongly.

Polish and English are independent decisions, so the registry has to support
one engine for both, different engines for each, and an override for a single
book. What it must never do is quietly narrate a book with something that
cannot read its language.
"""

from __future__ import annotations

import pytest

from bookbinder.models import (
    ModelUnavailable,
    Registry,
    RegistryError,
    load_registry,
)

TWO_ENGINES = {
    "defaults": {"pl": "engine-a", "en": "engine-b"},
    "models": {
        "engine-a": {
            "engine": "xtts", "environment": "narrator",
            "checkpoint": "a/checkpoint",
            "narration_languages": ["pl", "en"],
            "validation": "validated",
            "controls": ["temperature", "speed"],
            "char_limits": {"pl": 224, "en": 250},
        },
        "engine-b": {
            "engine": "chatterbox", "environment": "narrator-chatterbox",
            "checkpoint": "b/checkpoint",
            "narration_languages": ["en"],
            "reference_languages": ["en"],
            "needs_reference_transcript": True,
            "native_sample_rate": 22050,
            "default_char_limit": 400,
            "validation": "baseline",
        },
    },
}


def registry(**overrides) -> Registry:
    import copy
    raw = copy.deepcopy(TWO_ENGINES)
    raw.update(overrides)
    return Registry.from_dict(raw)


class TestTheShippedRegistry:
    """config/models.toml is read by the pipeline, so it has to parse."""

    def test_it_loads(self, tmp_path):
        from bookbinder.paths import project_root

        reg = load_registry(project_root())
        assert reg.models

    def test_polish_and_english_both_resolve(self):
        from bookbinder.paths import project_root

        reg = load_registry(project_root())
        assert reg.resolve("pl").narrates("pl")
        assert reg.resolve("en").narrates("en")

    def test_the_xtts_limits_match_what_chunking_used_to_hardcode(self):
        from bookbinder.manifest import XTTS_CHAR_LIMITS
        from bookbinder.paths import project_root

        spec = load_registry(project_root()).get("xtts-v2")
        for language, limit in XTTS_CHAR_LIMITS.items():
            assert spec.char_limit(language) == limit


class TestResolution:
    def test_each_language_gets_its_own_default(self):
        reg = registry()
        assert reg.resolve("pl").id == "engine-a"
        assert reg.resolve("en").id == "engine-b"

    def test_one_engine_may_serve_both(self):
        reg = registry(defaults={"pl": "engine-a", "en": "engine-a"})
        assert reg.resolve("pl").id == reg.resolve("en").id == "engine-a"

    def test_an_override_wins(self):
        assert registry().resolve("en", override="engine-a").id == "engine-a"

    def test_an_override_that_cannot_read_the_language_is_refused(self):
        # The failure this exists to prevent: a Polish book handed to an
        # English-only engine and rendered anyway.
        with pytest.raises(ModelUnavailable, match="does not narrate 'pl'"):
            registry().resolve("pl", override="engine-b")

    def test_the_refusal_names_what_would_work(self):
        with pytest.raises(ModelUnavailable, match="engine-a"):
            registry().resolve("pl", override="engine-b")

    def test_an_unknown_override_is_refused(self):
        with pytest.raises(ModelUnavailable, match="no model 'engine-z'"):
            registry().resolve("pl", override="engine-z")

    def test_a_language_with_no_default_is_refused(self):
        with pytest.raises(ModelUnavailable, match="no default model for 'de'"):
            registry().resolve("de")

    def test_candidates_are_listed_best_validated_first(self):
        reg = registry(defaults={"en": "engine-b"})
        assert [m.id for m in reg.for_language("en")] == ["engine-a", "engine-b"]

    def test_a_language_nothing_narrates_lists_nothing(self):
        assert registry().for_language("ja") == []


class TestCapabilities:
    def test_char_limits_come_from_the_model(self):
        reg = registry()
        assert reg.get("engine-a").char_limit("pl") == 224
        assert reg.get("engine-b").char_limit("pl") == 400   # its default

    def test_reference_languages_default_to_the_narration_set(self):
        assert registry().get("engine-a").accepts_reference_in("pl")

    def test_reference_languages_can_be_narrower(self):
        spec = registry().get("engine-b")
        assert spec.accepts_reference_in("en")
        assert not spec.accepts_reference_in("pl")

    def test_unsupported_controls_are_named_rather_than_dropped(self):
        spec = registry().get("engine-a")
        settings = {"temperature": 0.7, "speed": 1.0, "top_k": 50}
        assert spec.supported_controls(settings) == {"temperature": 0.7, "speed": 1.0}
        assert spec.unsupported_controls(settings) == ["top_k"]

    def test_identity_includes_the_revision_when_there_is_one(self):
        reg = Registry.from_dict({
            "models": {"m": {"engine": "xtts", "environment": "narrator",
                             "checkpoint": "c", "narration_languages": ["pl"],
                             "revision": "abc123"}}})
        assert reg.get("m").identity == "m@abc123"

    def test_identity_falls_back_to_the_id(self):
        assert registry().get("engine-a").identity == "engine-a"


class TestMalformedRegistries:
    """Every one of these used to be discoverable only mid-render."""

    def test_a_default_naming_an_undefined_model(self):
        with pytest.raises(RegistryError, match="not defined"):
            registry(defaults={"pl": "ghost"})

    def test_a_default_that_cannot_read_its_own_language(self):
        with pytest.raises(RegistryError, match="does not list it"):
            registry(defaults={"pl": "engine-b"})

    def test_an_engine_no_backend_implements(self):
        raw = {"models": {"m": {"engine": "imaginary", "environment": "narrator",
                                "checkpoint": "c", "narration_languages": ["pl"]}}}
        with pytest.raises(RegistryError, match="no backend implements"):
            Registry.from_dict(raw)

    def test_a_model_with_no_narration_languages(self):
        raw = {"models": {"m": {"engine": "xtts", "environment": "narrator",
                                "checkpoint": "c", "narration_languages": []}}}
        with pytest.raises(RegistryError, match="no narration languages"):
            Registry.from_dict(raw)

    def test_a_model_with_no_environment(self):
        raw = {"models": {"m": {"engine": "xtts", "checkpoint": "c",
                                "narration_languages": ["pl"]}}}
        with pytest.raises(RegistryError, match="no environment"):
            Registry.from_dict(raw)

    @pytest.mark.parametrize("field,value,message", [
        ("cloning", "magic", "expected one of"),
        ("validation", "excellent", "expected one of"),
    ])
    def test_an_unknown_enum_value(self, field, value, message):
        raw = {"models": {"m": {"engine": "xtts", "environment": "narrator",
                                "checkpoint": "c", "narration_languages": ["pl"],
                                field: value}}}
        with pytest.raises(RegistryError, match=message):
            Registry.from_dict(raw)

    def test_a_missing_registry_file(self, tmp_path):
        with pytest.raises(RegistryError, match="no model registry"):
            Registry.load(tmp_path / "absent.toml")
