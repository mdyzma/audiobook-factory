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
