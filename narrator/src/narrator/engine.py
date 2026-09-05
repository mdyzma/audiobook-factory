"""Shared XTTS-v2 loading. Runs in the `narrator` environment only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"


def allow_xtts_globals() -> None:
    """Let torch.load unpickle XTTS checkpoints.

    PyTorch 2.6 flipped `torch.load` to `weights_only=True`. XTTS checkpoints
    pickle their config objects, so loading fails with an UnpicklingError. The
    fix is to allowlist exactly those classes rather than turning the safety
    check off wholesale - that keeps arbitrary-code protection for every other
    checkpoint the process might load.
    """
    import torch

    if not hasattr(torch.serialization, "add_safe_globals"):
        return  # torch < 2.6 never restricted this

    from TTS.config.shared_configs import BaseDatasetConfig
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import XttsArgs, XttsAudioConfig

    torch.serialization.add_safe_globals(
        [XttsConfig, XttsAudioConfig, XttsArgs, BaseDatasetConfig]
    )


def pick_device(requested: str = "auto") -> str:
    import torch

    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


@dataclass
class VoiceProfile:
    """Written by transcriber.auto_label, consumed here."""

    name: str
    language: str
    reference_wavs: list[str]
    mode: str = "instant"
    model_dir: str | None = None
    sample_rate: int = 24000

    @classmethod
    def load(cls, root: Path, name: str) -> "VoiceProfile":
        path = root / "data" / "voices" / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"no voice profile at {path}; run `just label {name}` first")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            name=raw["name"],
            language=raw.get("language", "pl"),
            reference_wavs=[str(root / p) for p in raw["reference_wavs"]],
            mode=raw.get("mode", "instant"),
            model_dir=raw.get("model_dir"),
            sample_rate=raw.get("sample_rate", 24000),
        )


def load_model(profile: VoiceProfile, device: str):
    """Load stock XTTS-v2, or the fine-tuned checkpoint if the profile has one."""
    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts

    allow_xtts_globals()

    if profile.mode == "finetuned" and profile.model_dir:
        model_dir = Path(profile.model_dir)
        config = XttsConfig()
        config.load_json(str(model_dir / "config.json"))
        model = Xtts.init_from_config(config)
        model.load_checkpoint(config, checkpoint_dir=str(model_dir), eval=True)
        model.to(device)
        return model

    # Instant cloning path: stock weights, speaker identity comes from latents.
    from TTS.api import TTS

    return TTS(DEFAULT_MODEL).to(device)


def compute_latents(model, profile: VoiceProfile):
    """Average the reference clips into a speaker embedding once, up front.

    Recomputing this per chunk is the single biggest waste in a naive
    implementation; a full book is tens of thousands of chunks.
    """
    inner = getattr(model, "synthesizer", None)
    tts_model = inner.tts_model if inner is not None else model
    gpt_cond_latent, speaker_embedding = tts_model.get_conditioning_latents(
        audio_path=profile.reference_wavs,
        gpt_cond_len=30,
        max_ref_length=60,
    )
    return tts_model, gpt_cond_latent, speaker_embedding
