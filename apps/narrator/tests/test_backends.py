"""The seam between stage 4 and whichever engine narrates a book.

Stage 4 used to be XTTS all the way down. What matters now is that a book
carries the backend it was chunked for, and that an environment which cannot
load that backend refuses rather than substituting whatever is installed.
"""

from __future__ import annotations

import pytest

from narrator.backends import UnsupportedEngine, backend_for
from narrator.choice import ModelChoice


class TestReadingTheChoice:
    def test_the_model_block_is_read_from_the_book(self):
        choice = ModelChoice.from_book({"model": {
            "id": "xtts-v2", "engine": "xtts", "environment": "narrator",
            "native_sample_rate": 24000, "char_limit": 224,
            "settings": {"temperature": 0.7},
        }})
        assert (choice.id, choice.engine) == ("xtts-v2", "xtts")
        assert choice.settings == {"temperature": 0.7}

    def test_a_book_chunked_before_this_existed_still_loads(self):
        # No model block at all. The defaults have to be usable, because the
        # alternative is refusing to render every book already on disk.
        choice = ModelChoice.from_book({"slug": "solaris", "cast": {}})
        assert choice.id == "" and choice.engine == ""
        assert choice.native_sample_rate == 24000

    def test_unknown_fields_are_ignored_rather_than_fatal(self):
        # bookbinder may add a field before narrator knows about it.
        choice = ModelChoice.from_book({"model": {"id": "x", "future_field": 1}})
        assert choice.id == "x"

    def test_identity_pins_the_revision_when_there_is_one(self):
        assert ModelChoice.from_book(
            {"model": {"id": "xtts-v2", "revision": "abc"}}).identity == "xtts-v2@abc"

    def test_identity_falls_back_to_the_id(self):
        assert ModelChoice.from_book({"model": {"id": "xtts-v2"}}).identity == "xtts-v2"


class TestDispatch:
    def test_xtts_resolves_to_the_xtts_backend(self, tmp_path):
        from narrator.backends.xtts import XttsBackend

        choice = ModelChoice(id="xtts-v2", engine="xtts", environment="narrator")
        assert isinstance(backend_for(choice, tmp_path, "cpu"), XttsBackend)

    def test_a_book_with_no_engine_falls_back_to_xtts(self, tmp_path):
        # Everything rendered before the registry existed was XTTS.
        from narrator.backends.xtts import XttsBackend

        assert isinstance(backend_for(ModelChoice(), tmp_path, "cpu"), XttsBackend)

    def test_an_engine_this_environment_cannot_load_is_refused(self, tmp_path):
        choice = ModelChoice(id="chatterbox-multilingual", engine="chatterbox",
                             environment="narrator-chatterbox")
        with pytest.raises(UnsupportedEngine, match="does not implement"):
            backend_for(choice, tmp_path, "cpu")

    def test_the_refusal_names_the_environment_that_could(self, tmp_path):
        choice = ModelChoice(id="qwen", engine="qwen3-tts",
                             environment="narrator-qwen")
        with pytest.raises(UnsupportedEngine, match="narrator-qwen"):
            backend_for(choice, tmp_path, "cpu")


class TestTheContract:
    def test_the_xtts_backend_satisfies_it(self, tmp_path):
        from narrator.backends.xtts import XttsBackend

        backend = XttsBackend(tmp_path, "cpu")
        assert backend.engine == "xtts"
        assert callable(backend.speak) and callable(backend.sample_rate)

    def test_the_sample_rate_comes_from_the_voice(self, tmp_path):
        import json
        from narrator.backends.xtts import XttsBackend

        voices = tmp_path / "data" / "voices"
        voices.mkdir(parents=True)
        (voices / "v.json").write_text(json.dumps({
            "name": "v", "language": "pl", "sample_rate": 22050,
            "mode": "instant", "model_dir": None, "reference_wavs": []}),
            encoding="utf-8")
        assert XttsBackend(tmp_path, "cpu").sample_rate("v") == 22050
