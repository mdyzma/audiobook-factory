"""Driving a comparison, and refusing to turn it into a single number.

The judgement in a model comparison is a person listening. What can be
automated is giving every engine the same text, running it where it can
actually load, and counting what failed. Everything here is about not
corrupting that: not averaging across categories, not inventing settings, not
quietly skipping a model whose environment is missing.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from bookbinder.benchmark import (
    BenchmarkError,
    ModelRun,
    choice_payload,
    corpus_path,
    eligible,
    interpreter_for,
    report,
)
from bookbinder.models import Registry

REGISTRY = {
    "defaults": {"pl": "one", "en": "one"},
    "models": {
        "one": {"engine": "xtts", "environment": "narrator", "checkpoint": "c",
                "narration_languages": ["pl", "en"], "reference_languages": ["pl"],
                "controls": ["temperature", "speed"], "default_char_limit": 250,
                "char_limits": {"pl": 224},
                "settings": {"temperature": 0.7, "speed": 1.0}},
        "two": {"engine": "chatterbox", "environment": "chatterbox", "checkpoint": "d",
                "narration_languages": ["pl"], "reference_languages": ["pl"],
                "controls": ["exaggeration", "temperature"],
                "default_char_limit": 300,
                "settings": {"exaggeration": 0.5, "temperature": 0.8}},
        "english-only": {"engine": "qwen3-tts", "environment": "narrator", "checkpoint": "e",
                         "narration_languages": ["en"], "reference_languages": ["en"]},
    },
}


@pytest.fixture
def registry():
    return Registry.from_dict(REGISTRY)


class TestTheCorporaAreRealAndParallel:
    """They are checked in, so they can be wrong in the repository."""

    def _corpus(self, language):
        root = Path(__file__).resolve().parents[3]
        return tomllib.loads(
            (root / "benchmarks" / "corpora" / f"{language}.toml").read_text("utf-8"))

    @pytest.mark.parametrize("language", ["pl", "en"])
    def test_it_exists_and_declares_its_language(self, language):
        assert self._corpus(language)["language"] == language

    @pytest.mark.parametrize("language", ["pl", "en"])
    def test_every_passage_has_text_an_id_and_a_category(self, language):
        for passage in self._corpus(language)["passage"]:
            assert passage.get("id") and passage.get("category")
            assert (passage.get("text") or "").strip()

    @pytest.mark.parametrize("language", ["pl", "en"])
    def test_ids_are_unique(self, language):
        ids = [p["id"] for p in self._corpus(language)["passage"]]
        assert len(ids) == len(set(ids))

    @pytest.mark.parametrize("language", ["pl", "en"])
    def test_every_category_the_plan_asks_for_is_covered(self, language):
        # Narration, dialogue, numbers, abbreviations, proper names, short
        # headings, long sentences and chapter transitions. A comparison
        # missing one of these cannot say the thing it was run to say.
        found = {p["category"] for p in self._corpus(language)["passage"]}
        assert found >= {"narration", "dialogue", "numbers", "abbreviations",
                         "proper-names", "headings", "long-sentences", "transitions"}

    def test_the_two_languages_stay_comparable(self):
        # Not translations, but the same shape, so the sheets read side by side.
        pl = {p["category"] for p in self._corpus("pl")["passage"]}
        assert pl == {p["category"] for p in self._corpus("en")["passage"]}

    def test_polish_diacritics_are_actually_exercised(self):
        text = " ".join(p["text"] for p in self._corpus("pl")["passage"])
        assert set("ąćęłńóśźż") <= set(text.lower())

    def test_a_language_with_no_corpus_says_to_add_one(self, tmp_path):
        with pytest.raises(BenchmarkError, match="add one rather than generating"):
            corpus_path(tmp_path, "sw")


class TestWhoGetsRun:
    def test_every_model_that_narrates_the_language(self, registry):
        assert [m.id for m in eligible(registry, "pl")] == ["one", "two"]

    def test_and_only_those(self, registry):
        assert "two" not in [m.id for m in eligible(registry, "en")]

    def test_unvalidated_models_are_included(self, registry):
        # An unvalidated entry is exactly what a benchmark exists to move.
        assert all(m.validation == "unvalidated" for m in eligible(registry, "pl"))

    def test_the_order_does_not_depend_on_the_registry_file(self, registry):
        assert [m.id for m in eligible(registry, "pl")] == \
            sorted(m.id for m in eligible(registry, "pl"))


class TestEachEngineRunsAtItsOwnPin:
    """One global settings block cannot serve two engines.

    Their control names do not line up. Before the registry pinned them per
    engine, Chatterbox's `exaggeration` and `cfg_weight` were never set at all
    and it ran at whatever its library defaulted to, unrecorded, which is the
    one thing a comparison cannot rest on.
    """

    def test_a_backend_runs_at_its_pin_by_default(self, registry):
        assert choice_payload(registry.get("two"), "pl", {})["settings"] == \
            {"exaggeration": 0.5, "temperature": 0.8}

    def test_two_engines_get_different_pins(self, registry):
        one = choice_payload(registry.get("one"), "pl", {})["settings"]
        two = choice_payload(registry.get("two"), "pl", {})["settings"]
        assert one != two
        assert "exaggeration" in two and "exaggeration" not in one

    def test_an_override_wins_over_the_pin(self, registry):
        payload = choice_payload(registry.get("two"), "pl", {"temperature": 0.2})
        assert payload["settings"]["temperature"] == 0.2

    def test_an_override_does_not_wipe_the_rest_of_the_pin(self, registry):
        payload = choice_payload(registry.get("two"), "pl", {"temperature": 0.2})
        assert payload["settings"]["exaggeration"] == 0.5

    def test_an_override_for_a_control_it_lacks_is_still_reported(self, registry):
        payload = choice_payload(registry.get("two"), "pl", {"speed": 2.0})
        assert "speed" not in payload["settings"]
        assert payload["unsupported"] == ["speed"]

    def test_a_pin_naming_an_unknown_control_is_refused(self):
        # A typo that would otherwise sit in the file looking effective.
        from bookbinder.models import RegistryError

        broken = {
            "defaults": {"pl": "one"},
            "models": {"one": {
                "engine": "xtts", "environment": "narrator", "checkpoint": "c",
                "narration_languages": ["pl"], "reference_languages": ["pl"],
                "controls": ["temperature"],
                "settings": {"exaggeration": 0.5}}},
        }
        with pytest.raises(RegistryError, match="does not list under"):
            Registry.from_dict(broken)


class TestTheSettingsAreTheRegistrys:
    """A comparison run against settings no book would use measures nothing."""

    def test_the_models_own_char_limit_travels_with_it(self, registry):
        payload = choice_payload(registry.get("one"), "pl", {})
        assert payload["char_limit"] == 224

    def test_a_control_the_model_has_is_passed(self, registry):
        payload = choice_payload(registry.get("one"), "pl", {"temperature": 0.8})
        assert payload["settings"]["temperature"] == 0.8

    def test_a_control_it_lacks_is_named_rather_than_dropped(self, registry):
        payload = choice_payload(registry.get("one"), "pl", {"exaggeration": 0.6})
        assert "exaggeration" not in payload["settings"]
        assert payload["unsupported"] == ["exaggeration"]

    def test_two_engines_take_their_own_half_of_one_override(self, registry):
        settings = {"temperature": 0.8, "exaggeration": 0.6}
        assert choice_payload(registry.get("one"), "pl", settings)["settings"]["temperature"] == 0.8
        assert choice_payload(registry.get("two"), "pl", settings)["settings"]["exaggeration"] == 0.6


class TestAMissingEnvironment:
    def test_it_says_which_one_and_how_to_get_it(self, registry, tmp_path):
        with pytest.raises(BenchmarkError, match="just setup-chatterbox"):
            interpreter_for(tmp_path, registry.get("two"))


class TestTheReportRefusesToAverage:
    def _run(self, generations):
        return ModelRun(model="one", engine="xtts", environment="narrator",
                        generations=generations)

    def test_each_category_is_reported_separately(self):
        run = self._run([
            {"passage": "a", "category": "narration", "ok": True, "realtime": 0.4},
            {"passage": "b", "category": "numbers", "ok": False, "error": "boom"},
        ])
        text = report("pl", [run])
        assert "narration" in text and "numbers" in text

    def test_a_category_that_failed_is_visible_beside_one_that_did_not(self):
        # The failure this prevents: a model that narrates well and cannot say
        # a number scoring well on an average.
        run = self._run([
            {"passage": "a", "category": "narration", "ok": True, "realtime": 0.4},
            {"passage": "b", "category": "numbers", "ok": False, "error": "boom"},
        ])
        text = report("pl", [run])
        assert "1 failed" in text

    def test_silent_and_short_generations_are_called_out(self):
        run = self._run([
            {"passage": "a", "category": "headings", "ok": True, "silent": True},
            {"passage": "b", "category": "headings", "ok": True, "short": True},
        ])
        assert "2 silent or short" in report("pl", [run])

    def test_it_says_that_it_is_not_a_verdict(self):
        assert "Nothing here is a verdict" in report("pl", [self._run([])])

    def test_a_model_that_could_not_run_says_why(self):
        run = ModelRun(model="two", engine="chatterbox", environment="chatterbox",
                       error="not set up")
        assert "not set up" in report("pl", [run])
