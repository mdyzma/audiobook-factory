"""Stage 4's resume rule, and the one thing that can poison it.

Resume is `skip any fragment that already has a wav`. That rule is what makes
a twenty-hour book survivable, and it is also why a dry run must never be left
in place: dry-run silence occupies exactly those paths, with plausible names
and durations, so a render started on top of one adopts every fragment and
finishes in under a second having produced nothing.

These tests need no model weights.
"""

from __future__ import annotations

import json

import pytest

from narrator import synth
from narrator.synth import DRY_RUN_MARKER, discard_dry_run


def make_render(tmp_path, count=3, marked=False):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    for i in range(count):
        (audio_dir / f"ch001_{i:04d}.wav").write_bytes(b"RIFF....WAVE")
    (audio_dir / "rendered.jsonl").write_text("{}\n", encoding="utf-8")
    if marked:
        (audio_dir / DRY_RUN_MARKER).write_text(
            json.dumps({"schema_version": 1, "slug": "book", "chunks": count}),
            encoding="utf-8",
        )
    return audio_dir


class TestDiscardDryRun:
    def test_marked_silence_is_removed_before_a_render(self, tmp_path):
        audio_dir = make_render(tmp_path, count=3, marked=True)
        assert discard_dry_run(audio_dir) == 3
        assert not list(audio_dir.glob("*.wav"))
        assert not (audio_dir / "rendered.jsonl").exists()
        assert not (audio_dir / DRY_RUN_MARKER).exists()

    def test_an_unmarked_render_is_never_touched(self, tmp_path):
        """Hours of finished narration must survive this unconditionally."""
        audio_dir = make_render(tmp_path, count=3, marked=False)
        before = sorted(p.name for p in audio_dir.glob("*.wav"))

        assert discard_dry_run(audio_dir) == 0
        assert sorted(p.name for p in audio_dir.glob("*.wav")) == before
        assert (audio_dir / "rendered.jsonl").exists()

    def test_calling_it_twice_is_harmless(self, tmp_path):
        audio_dir = make_render(tmp_path, count=2, marked=True)
        assert discard_dry_run(audio_dir) == 2
        assert discard_dry_run(audio_dir) == 0

    def test_a_marker_with_no_audio_left_still_clears(self, tmp_path):
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        (audio_dir / DRY_RUN_MARKER).write_text("{}", encoding="utf-8")

        assert discard_dry_run(audio_dir) == 0
        assert not (audio_dir / DRY_RUN_MARKER).exists()


class TestVoicePoolCheckpointIsolation:
    """A cast must never be narrated by one voice's weights.

    The pool cached a single model, so whichever voice was rendered first
    loaded its checkpoint and every later voice was served that same one. With
    a fine-tuned narrator and instant-cloned dialogue voices, the whole book
    came out in the narrator's trained voice with nothing reported.
    """

    def _pool(self, tmp_path, monkeypatch, profiles):
        import narrator.backends.xtts as backend

        voices = tmp_path / "data" / "voices"
        voices.mkdir(parents=True)
        for name, extra in profiles.items():
            payload = {"name": name, "language": "pl", "sample_rate": 24000,
                       "mode": "instant", "model_dir": None, "reference_wavs": []}
            payload.update(extra)
            (voices / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")

        loaded: list[str] = []

        def fake_load_model(profile, device):
            loaded.append(profile.name)
            return f"model-for-{profile.name}"

        monkeypatch.setattr(backend, "load_model", fake_load_model)
        return backend.XttsBackend(tmp_path, "cpu"), loaded

    def test_instant_voices_share_one_loaded_model(self, tmp_path, monkeypatch):
        pool, loaded = self._pool(tmp_path, monkeypatch, {"a": {}, "b": {}})
        assert pool.model("a") is pool.model("b")
        assert loaded == ["a"]  # loaded once, which is the point of the pool

    def test_finetuned_voice_does_not_reuse_the_stock_model(self, tmp_path, monkeypatch):
        pool, loaded = self._pool(tmp_path, monkeypatch, {
            "narrator": {"mode": "finetuned", "model_dir": "training/narrator"},
            "dialogue": {},
        })
        assert pool.model("narrator") != pool.model("dialogue")
        assert loaded == ["narrator", "dialogue"]

    def test_stock_voice_rendered_first_does_not_capture_the_finetuned_one(
            self, tmp_path, monkeypatch):
        # Order must not decide which weights a voice gets.
        pool, loaded = self._pool(tmp_path, monkeypatch, {
            "dialogue": {},
            "narrator": {"mode": "finetuned", "model_dir": "training/narrator"},
        })
        assert pool.model("dialogue") == "model-for-dialogue"
        assert pool.model("narrator") == "model-for-narrator"

    def test_two_finetuned_voices_each_load_their_own(self, tmp_path, monkeypatch):
        pool, loaded = self._pool(tmp_path, monkeypatch, {
            "a": {"mode": "finetuned", "model_dir": "training/a"},
            "b": {"mode": "finetuned", "model_dir": "training/b"},
        })
        assert pool.model("a") != pool.model("b")
        assert loaded == ["a", "b"]


class TestFingerprintLedger:
    """What lets a resumed render tell current audio from stale audio.

    Appended per fragment rather than written at the end, because a render
    killed at hour six has to leave the first six hours reusable.
    """

    def test_nothing_recorded_yet_reads_as_empty(self, tmp_path):
        from narrator.synth import read_fingerprints

        assert read_fingerprints(tmp_path) == {}

    def test_what_was_appended_reads_back(self, tmp_path):
        from narrator.synth import append_fingerprint, read_fingerprints

        append_fingerprint(tmp_path, "ch001_0000", "aaa")
        append_fingerprint(tmp_path, "ch001_0001", "bbb")
        assert read_fingerprints(tmp_path) == {"ch001_0000": "aaa", "ch001_0001": "bbb"}

    def test_re_rendering_one_fragment_supersedes_its_entry(self, tmp_path):
        from narrator.synth import append_fingerprint, read_fingerprints

        append_fingerprint(tmp_path, "ch001_0000", "old")
        append_fingerprint(tmp_path, "ch001_0000", "new")
        assert read_fingerprints(tmp_path) == {"ch001_0000": "new"}

    def test_a_line_torn_in_half_does_not_lose_the_rest(self, tmp_path):
        # What a kill mid-write leaves behind.
        from narrator.synth import FINGERPRINTS, append_fingerprint, read_fingerprints

        append_fingerprint(tmp_path, "ch001_0000", "aaa")
        with (tmp_path / FINGERPRINTS).open("a", encoding="utf-8") as fh:
            fh.write('{"id": "ch001_0001", "fingerpr')
        assert read_fingerprints(tmp_path) == {"ch001_0000": "aaa"}

    def test_a_dry_run_takes_its_ledger_with_it(self, tmp_path):
        from narrator.synth import (
            DRY_RUN_MARKER, FINGERPRINTS, append_fingerprint, discard_dry_run,
        )

        audio = tmp_path / "audio"
        audio.mkdir()
        (audio / "ch001_0000.wav").write_bytes(b"RIFF")
        append_fingerprint(audio, "ch001_0000", "silence")
        (audio / DRY_RUN_MARKER).write_text("{}", encoding="utf-8")

        discard_dry_run(audio)
        assert not (audio / FINGERPRINTS).exists()


class TestFailureClassification:
    """Whose fault a failure is decides whether retrying it can help.

    A fragment fault is one piece of text the engine could not read. A voice or
    model fault repeats for every fragment, so grinding through a whole book to
    report it ten thousand times helps nobody.
    """

    def test_a_missing_voice_profile_is_the_voice(self):
        from narrator.synth import classify_failure

        assert classify_failure(
            FileNotFoundError("no voice profile at data/voices/absent.json")) == "voice"

    def test_a_latent_problem_is_the_voice(self):
        from narrator.synth import classify_failure

        assert classify_failure(
            RuntimeError("could not derive speaker latents for 'michal'")) == "voice"

    def test_running_out_of_memory_is_the_model(self):
        from narrator.synth import classify_failure

        assert classify_failure(RuntimeError("CUDA out of memory")) == "model"

    def test_a_checkpoint_problem_is_the_model(self):
        from narrator.synth import classify_failure

        assert classify_failure(RuntimeError("failed to load checkpoint")) == "model"

    def test_anything_else_is_the_fragment(self):
        from narrator.synth import classify_failure

        assert classify_failure(ValueError("token sequence too long")) == "fragment"

    def test_the_classification_is_one_of_the_documented_kinds(self):
        from narrator.synth import classify_failure

        for exc in (ValueError("x"), RuntimeError("CUDA"), FileNotFoundError("y")):
            assert classify_failure(exc) in {"fragment", "voice", "model"}


class TestLevellingAVoiceWhileItRenders:
    """The narrator applies a correction; it does not decide one.

    The number comes from the voice's profile, which is hashed into every
    fragment fingerprint, so the assembler derives the same thing from the same
    file and the two cannot disagree about loudness without disagreeing about
    identity first.
    """

    def _profile(self, tmp_path, **fields):
        path = tmp_path / "data" / "voices" / "michal.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"name": "michal", "language": "pl", "reference_wavs": [], **fields}),
            encoding="utf-8")
        return tmp_path

    def test_it_reads_the_correction_from_the_profile(self, tmp_path):
        root = self._profile(tmp_path, gain_db=-1.4)
        assert synth.voice_gain(root, "michal") == -1.4

    def test_a_voice_never_levelled_gets_no_correction(self, tmp_path):
        # Which is what keeps books rendered before this landing valid.
        root = self._profile(tmp_path)
        assert synth.voice_gain(root, "michal") == 0.0

    def test_a_missing_profile_is_not_an_error(self, tmp_path):
        assert synth.voice_gain(tmp_path, "nobody") == 0.0

    def test_a_damaged_profile_is_not_an_error(self, tmp_path):
        path = tmp_path / "data" / "voices" / "michal.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        assert synth.voice_gain(tmp_path, "michal") == 0.0

    def test_six_decibels_up_is_twice_the_amplitude(self):
        import numpy as np

        wav = np.full(100, 0.1, dtype="float32")
        louder, clipped = synth.apply_gain(wav, 6.0)
        assert float(louder[0]) == pytest.approx(0.2, abs=0.002)
        assert clipped == 0

    def test_six_decibels_down_is_half(self):
        import numpy as np

        quieter, _ = synth.apply_gain(np.full(100, 0.4, dtype="float32"), -6.0)
        assert float(quieter[0]) == pytest.approx(0.2, abs=0.002)

    def test_no_correction_leaves_the_samples_alone(self):
        import numpy as np

        wav = np.full(100, 0.3, dtype="float32")
        same, clipped = synth.apply_gain(wav, 0.0)
        assert same is wav and clipped == 0

    def test_two_voices_end_up_matched(self):
        """What the whole feature is for, on actual samples."""
        import numpy as np

        narrator, _ = synth.apply_gain(np.full(10, 0.5, dtype="float32"), -6.0)
        dialogue, _ = synth.apply_gain(np.full(10, 0.125, dtype="float32"), 6.0)
        assert float(narrator[0]) == pytest.approx(float(dialogue[0]), abs=0.005)

    def test_clipping_is_counted_rather_than_hidden(self):
        # A voice whose correction is too large for its loudest passage. The
        # clamp is the last resort; the count is how anyone finds out.
        import numpy as np

        wav = np.array([0.9, 0.2, -0.95], dtype="float32")
        clamped, clipped = synth.apply_gain(wav, 6.0)
        assert clipped == 2
        assert float(np.max(np.abs(clamped))) <= 1.0

    def test_nothing_is_clamped_when_nothing_overflows(self):
        import numpy as np

        wav = np.array([0.1, -0.2], dtype="float32")
        scaled, clipped = synth.apply_gain(wav, 6.0)
        assert clipped == 0
        assert float(np.max(np.abs(scaled))) == pytest.approx(0.4, abs=0.002)
