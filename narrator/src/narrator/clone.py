"""Stage 1c - build and audition a cloned voice.

Instant cloning needs no training: XTTS-v2 derives a speaker embedding from
the reference clips produced by the auto-labeller. This command caches those
latents and renders a short audition so the clone can be judged before
committing hours of synthesis to it.
"""

from __future__ import annotations

import json
from pathlib import Path

import torch
import typer

from narrator.paths import project_root
from narrator.engine import VoiceProfile, compute_latents, load_model, pick_device

app = typer.Typer(add_completion=False)

AUDITION_TEXT = {
    "pl": "To jest próbka sklonowanego głosu. Sprawdź intonację, oddechy i tempo.",
    "en": "This is a sample of the cloned voice. Check intonation, breathing and pace.",
}


@app.command()
def main(
    voice: str = typer.Argument(..., help="Voice name, matching data/voices/<voice>.json"),
    device: str = typer.Option("auto", help="auto | cuda | mps | cpu"),
    text: str = typer.Option("", help="Audition text; defaults to a built-in phrase"),
) -> None:
    root = project_root()
    profile = VoiceProfile.load(root, voice)
    dev = pick_device(device)
    typer.echo(f"cloning '{voice}' from {len(profile.reference_wavs)} references on {dev}")

    tts_model = load_model(profile, dev)
    gpt_cond_latent, speaker_embedding = compute_latents(tts_model, profile)

    cache_dir = root / "data" / "voices" / voice
    cache_dir.mkdir(parents=True, exist_ok=True)
    torch.save(
        {"gpt_cond_latent": gpt_cond_latent.cpu(), "speaker_embedding": speaker_embedding.cpu()},
        cache_dir / "latents.pt",
    )

    import soundfile as sf

    audition = text or AUDITION_TEXT.get(profile.language, AUDITION_TEXT["en"])
    out = tts_model.inference(
        audition, profile.language, gpt_cond_latent, speaker_embedding, temperature=0.7
    )
    audition_path = cache_dir / "audition.wav"
    sf.write(audition_path, out["wav"], profile.sample_rate)

    (cache_dir / "clone.json").write_text(
        json.dumps({"voice": voice, "device": dev, "mode": profile.mode,
                    "references": len(profile.reference_wavs)}, indent=2),
        encoding="utf-8",
    )
    typer.echo(f"latents cached -> {cache_dir / 'latents.pt'}\naudition -> {audition_path}")


if __name__ == "__main__":
    app()