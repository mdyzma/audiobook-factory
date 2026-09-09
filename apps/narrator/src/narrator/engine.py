"""Shared XTTS-v2 loading. Runs in the `narrator` environment only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:  # heavy imports stay out of the runtime path
    from torch import Tensor
    from TTS.tts.models.xtts import Xtts

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


def checkpoint_key(profile: VoiceProfile) -> str:
    """Identity of the weights this profile loads.

    Every instant-cloned voice shares the stock checkpoint and differs only in
    its speaker latents, which is what lets one loaded model serve a whole
    cast. A fine-tuned voice carries its own weights, so it needs its own
    entry: caching by voice name alone would hand the first voice's checkpoint
    to every later one and narrate the book in the wrong trained voice.
    """
    if profile.mode == "finetuned" and profile.model_dir:
        return f"finetuned:{Path(profile.model_dir).resolve()}"
    return f"stock:{DEFAULT_MODEL}"


def load_model(profile: VoiceProfile, device: str) -> "Xtts":
    """Load stock XTTS-v2, or the fine-tuned checkpoint if the profile has one.

    Always returns the `Xtts` model itself, never the `TTS` API wrapper. Callers
    need `.inference()`, which lives on the model; unwrapping here keeps a single
    return type, so a type checker can follow it. Reaching through
    `.synthesizer.tts_model` at the call site cannot be checked, because
    `nn.Module.__getattr__` is annotated `Tensor | Module` and every dynamic
    attribute lookup widens to that.
    """
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

    api = TTS(DEFAULT_MODEL).to(device)
    synthesizer = api.synthesizer
    if synthesizer is None:  # pragma: no cover - only on a broken install
        raise RuntimeError(f"TTS returned no synthesizer for {DEFAULT_MODEL}")
    return cast("Xtts", synthesizer.tts_model)


def load_latents(root: Path, voice: str, device: str) -> "tuple[Tensor, Tensor] | None":
    """Return latents cached by `just clone`, if they exist.

    Deriving them costs a few seconds per voice. With a multi-voice cast that
    is paid once per voice rather than once per chunk, so the cache matters
    more than it did for a single narrator.
    """
    import torch

    path = root / "data" / "voices" / voice / "latents.pt"
    if not path.exists():
        return None
    cached = torch.load(path, map_location=device)
    return cached["gpt_cond_latent"].to(device), cached["speaker_embedding"].to(device)


def compute_latents(model: "Xtts", profile: VoiceProfile) -> "tuple[Tensor, Tensor]":
    """Average the reference clips into a speaker embedding once, up front.

    Recomputing this per chunk is the single biggest waste in a naive
    implementation; a full book is tens of thousands of chunks.
    """
    gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
        audio_path=profile.reference_wavs,
        gpt_cond_len=30,
        max_ref_length=60,
    )
    # XTTS returns a null embedding when it cannot read the references - silence,
    # a wrong sample rate, or a path that no longer exists. Catch it here rather
    # than letting inference fail thousands of chunks into a book.
    if gpt_cond_latent is None or speaker_embedding is None:
        raise RuntimeError(
            f"could not derive speaker latents for '{profile.name}' from "
            f"{len(profile.reference_wavs)} reference clips; check that they exist, "
            f"are {profile.sample_rate} Hz mono and are not silent"
        )
    return gpt_cond_latent, speaker_embedding
