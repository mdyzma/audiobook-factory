"""Chatterbox Multilingual as stage 4 sees it.

The second engine, and the one the backend seam was written for. It answers the
same two questions XTTS does, which text to speak and at what rate the audio
comes back, and everything else about it is a registry entry.

Two things it does differently, both carried rather than hidden.

**Conditioning is a file, not a cached tensor.** XTTS derives speaker latents
once and reuses them; Chatterbox takes `audio_prompt_path` on every call. A
voice profile lists several reference clips, so one has to be chosen, and which
one is part of what the voice sounds like. It is chosen by the same rule every
time and recorded in the voice's conditioning cache, so the same voice is the
same voice across runs.

**The controls have different names.** `exaggeration`, `cfg_weight`,
`repetition_penalty`, `min_p` and `top_p` alongside `temperature`. A book
records the settings it was chunked against, and a setting this engine does not
implement is named rather than dropped: a control that was quietly ignored
looks like one that had no effect, and that difference is the whole point of
comparing two engines.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from numpy import ndarray

ENGINE = "chatterbox"

# What `generate` accepts. Anything else a book asks for is reported as ignored
# rather than passed through and silently dropped by a keyword mismatch.
CONTROLS = ("exaggeration", "cfg_weight", "temperature", "repetition_penalty",
            "min_p", "top_p")

# Chatterbox synthesises at the s3gen rate. Read from the loaded model rather
# than assumed, because getting a rate wrong changes pitch and every chapter
# timestamp with it; this is only the fallback for reporting before a load.
NATIVE_SAMPLE_RATE = 24000

# Enough reference audio to condition on. Below this the clone is a guess, and
# the probe at clone time already refuses recordings this short.
MIN_REFERENCE_SECONDS = 3.0


class VoiceNotUsable(RuntimeError):
    """The voice has no reference audio this engine can condition on."""


def reference_for(root: Path, voice: str) -> Path:
    """The clip this engine speaks with, chosen the same way every time.

    A profile lists every usable segment the labeller found. Chatterbox
    conditions on exactly one, so the choice matters and must not drift: the
    longest clip, and the earliest of those on a tie, because that is stable
    across runs where "the best one" would not be.
    """
    profile_path = root / "data" / "voices" / f"{voice}.json"
    if not profile_path.is_file():
        raise VoiceNotUsable(f"no voice profile at {profile_path}")

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    clips = []
    for relative in profile.get("reference_wavs") or []:
        path = Path(relative)
        path = path if path.is_absolute() else root / relative
        if path.is_file():
            clips.append(path)
    if not clips:
        raise VoiceNotUsable(
            f"'{voice}' has no reference audio on disk; re-run `just label {voice}`")

    return sorted(clips, key=lambda p: (-p.stat().st_size, str(p)))[0]


def conditioning_key(root: Path, voice: str) -> str:
    """Identity of what this engine conditions on, for the derived cache.

    Per engine, because the same voice conditions differently here than under
    XTTS: one reference clip rather than a pooled latent. Two backends caching
    under one key is how a voice ends up sounding like the other engine's idea
    of it.
    """
    reference = reference_for(root, voice)
    digest = hashlib.sha256()
    digest.update(ENGINE.encode("utf-8"))
    digest.update(reference.name.encode("utf-8"))
    digest.update(reference.read_bytes())
    return digest.hexdigest()[:32]


def accepted(settings: dict) -> dict:
    """Only the controls `generate` takes, with the rest left for the caller."""
    return {k: v for k, v in (settings or {}).items() if k in CONTROLS}


def ignored(settings: dict) -> list[str]:
    return sorted(k for k in (settings or {}) if k not in CONTROLS)


class ChatterboxBackend:
    """Stage 4's view of Chatterbox Multilingual."""

    engine = ENGINE

    def __init__(self, root: Path, device: str = "auto") -> None:
        self.root = root
        self.device = device
        self._model = None
        self._reported: set[str] = set()

    def _load(self):
        if self._model is None:
            import torch
            from chatterbox import ChatterboxMultilingualTTS

            chosen = self.device
            if chosen in ("", "auto"):
                chosen = ("cuda" if torch.cuda.is_available()
                          else "mps" if torch.backends.mps.is_available() else "cpu")
            self._model = ChatterboxMultilingualTTS.from_pretrained(
                device=torch.device(chosen))
        return self._model

    def supports(self, language: str) -> bool:
        from chatterbox import SUPPORTED_LANGUAGES

        return language in SUPPORTED_LANGUAGES

    def sample_rate(self, voice: str) -> int:
        """Read from the model, which is the only thing that actually knows."""
        return int(getattr(self._load(), "sr", NATIVE_SAMPLE_RATE))

    def speak(self, text: str, language: str, voice: str, settings: dict) -> "ndarray":
        if not self.supports(language):
            from chatterbox import SUPPORTED_LANGUAGES

            raise VoiceNotUsable(
                f"Chatterbox does not read '{language}'. It reads "
                f"{', '.join(sorted(SUPPORTED_LANGUAGES))}")

        model = self._load()
        reference = reference_for(self.root, voice)

        left_out = ignored(settings)
        if left_out and voice not in self._reported:
            import sys

            self._reported.add(voice)
            print(f"note: chatterbox ignores {', '.join(left_out)}", file=sys.stderr)

        wav = model.generate(text, language_id=language,
                             audio_prompt_path=str(reference), **accepted(settings))

        # A torch tensor with a channel dimension, which the wav writer does
        # not want. Converted unconditionally rather than behind a hasattr
        # check: this engine always returns a tensor, and a branch that might
        # hand back either type is one stage 4 would have to know about.
        import numpy as np

        return np.asarray(wav.detach().cpu().numpy(), dtype="float32").squeeze()
