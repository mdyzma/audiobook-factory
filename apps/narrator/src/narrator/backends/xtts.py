"""XTTS-v2 behind the backend contract.

All of this used to live inline in stage 4. It is unchanged in behaviour: one
checkpoint loaded per distinct set of weights, one latent pair per voice, and
inference through the model rather than the API wrapper. What is new is that
stage 4 no longer knows any of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from narrator.engine import (
    VoiceProfile,
    checkpoint_key,
    compute_latents,
    load_latents,
    load_model,
)

if TYPE_CHECKING:
    from numpy import ndarray
    from TTS.tts.models.xtts import Xtts


class XttsBackend:
    """Loads each checkpoint once and keeps a latent pair per voice.

    Instant-cloned voices all share the stock weights and differ only in their
    speaker latents, so one loaded model serves the whole cast. A fine-tuned
    voice has its own weights, so models are cached by checkpoint identity
    rather than globally: caching a single model meant the first voice loaded
    narrated every other voice in the book.
    """

    engine = "xtts"

    def __init__(self, root: Path, device: str) -> None:
        self.root = root
        self.device = device
        self._models: "dict[str, Xtts]" = {}
        self._latents: dict[str, tuple] = {}
        self._profiles: dict[str, VoiceProfile] = {}

    def profile(self, voice: str) -> VoiceProfile:
        if voice not in self._profiles:
            self._profiles[voice] = VoiceProfile.load(self.root, voice)
        return self._profiles[voice]

    def model(self, voice: str) -> "Xtts":
        profile = self.profile(voice)
        key = checkpoint_key(profile)
        if key not in self._models:
            self._models[key] = load_model(profile, self.device)
        return self._models[key]

    def latents(self, voice: str):
        if voice not in self._latents:
            profile = self.profile(voice)
            cached = load_latents(self.root, voice, self.device)
            if cached is None:
                cached = compute_latents(self.model(voice), profile)
            self._latents[voice] = cached
        return self._latents[voice]

    def sample_rate(self, voice: str) -> int:
        return self.profile(voice).sample_rate

    def speak(self, text: str, language: str, voice: str, settings: dict) -> "ndarray":
        profile = self.profile(voice)
        gpt_cond_latent, speaker_embedding = self.latents(voice)
        out = self.model(voice).inference(
            text,
            language or profile.language,
            gpt_cond_latent,
            speaker_embedding,
            temperature=settings.get("temperature", 0.70),
            length_penalty=settings.get("length_penalty", 1.0),
            repetition_penalty=settings.get("repetition_penalty", 2.0),
            top_k=int(settings.get("top_k", 50)),
            top_p=settings.get("top_p", 0.85),
            speed=settings.get("speed", 1.0),
        )
        return out["wav"]


# The pool used to be called this, and `just check-narrator` and the tests
# refer to it. Kept so the rename is not a second change to review.
VoicePool = XttsBackend
