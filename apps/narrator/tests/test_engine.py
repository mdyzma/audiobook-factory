"""Voice-profile loading and device selection.

Model loading itself needs XTTS weights and is exercised by `just check-narrator`
rather than here; these cover the logic around it.
"""

from __future__ import annotations

import json

import pytest

from narrator.engine import VoiceProfile, pick_device


def write_profile(root, name="v", **overrides):
    voices = root / "data" / "voices"
    voices.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name, "engine": "xtts_v2", "language": "pl", "sample_rate": 24000,
        "mode": "instant", "model_dir": None,
        "reference_wavs": ["data/datasets/v/wavs/seg_0000.wav"],
    }
    payload.update(overrides)
    (voices / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return voices / f"{name}.json"


class TestVoiceProfile:
    def test_loads_and_absolutises_reference_paths(self, tmp_path):
        write_profile(tmp_path)
        p = VoiceProfile.load(tmp_path, "v")
        assert p.name == "v"
        assert p.language == "pl"
        # Stored relative, returned absolute, so the narrator can be run from
        # any working directory.
        assert p.reference_wavs[0] == str(tmp_path / "data/datasets/v/wavs/seg_0000.wav")

    def test_missing_profile_names_the_fix(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="just label"):
            VoiceProfile.load(tmp_path, "absent")

    def test_defaults_applied_for_absent_keys(self, tmp_path):
        voices = tmp_path / "data" / "voices"
        voices.mkdir(parents=True)
        (voices / "v.json").write_text(
            json.dumps({"name": "v", "reference_wavs": []}), encoding="utf-8")
        p = VoiceProfile.load(tmp_path, "v")
        assert (p.language, p.mode, p.sample_rate) == ("pl", "instant", 24000)

    def test_finetuned_profile_carries_model_dir(self, tmp_path):
        write_profile(tmp_path, mode="finetuned", model_dir="training/v")
        p = VoiceProfile.load(tmp_path, "v")
        assert p.mode == "finetuned"
        assert p.model_dir == "training/v"


class TestPickDevice:
    @pytest.mark.parametrize("requested", ["cuda", "mps", "cpu"])
    def test_explicit_choice_is_honoured(self, requested):
        assert pick_device(requested) == requested

    def test_auto_returns_an_available_backend(self):
        assert pick_device("auto") in {"cuda", "mps", "cpu"}

    def test_auto_prefers_cuda(self, monkeypatch):
        import torch
        monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
        assert pick_device("auto") == "cuda"

    def test_auto_falls_back_to_cpu(self, monkeypatch):
        import torch
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
        monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
        assert pick_device("auto") == "cpu"
