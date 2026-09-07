"""Stage 1b - turn a cleaned voice recording into a labelled cloning dataset.

Runs in the `transcriber` environment (WhisperX, numpy 2.x). Cutting on
WhisperX alignment boundaries rather than fixed 10-second intervals keeps
word endings intact; clipped endings are what make amateur clones sound
metallic.

Output: data/datasets/<voice>/wavs/*.wav plus metadata.csv in Coqui's
`filename|transcription` format, plus voice.json describing the reference
clips the narrator should use for instant cloning.
"""

from __future__ import annotations

from transcriber.paths import project_root

import json
import os
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)


def pick_device(requested: str) -> tuple[str, str]:
    """Return (device, compute_type). float16 needs CUDA; CPU/MPS fall back to int8."""
    import torch

    if requested != "auto":
        device = requested
    elif torch.cuda.is_available():
        device = "cuda"
    else:
        # WhisperX runs through faster-whisper/CTranslate2, which has no MPS
        # backend. On the M1 this is CPU, and that is expected.
        device = "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


@app.command()
def main(
    voice: str = typer.Argument(..., help="Voice name, e.g. michal"),
    audio: Path = typer.Option(None, help="Defaults to data/processed/<voice>/cleaned_full.wav"),
    model: str = typer.Option("large-v3", help="Whisper model size"),
    language: str = typer.Option("pl", help="Spoken language of the sample"),
    device: str = typer.Option("auto", help="auto | cuda | cpu"),
    batch_size: int = typer.Option(16),
    min_sec: float = typer.Option(1.5),
    max_sec: float = typer.Option(15.0),
    reference_count: int = typer.Option(12, help="Longest N segments kept as cloning references"),
) -> None:
    import whisperx
    from pydub import AudioSegment
    from tqdm import tqdm

    root = project_root()
    audio_path = audio or root / "data" / "processed" / voice / "cleaned_full.wav"
    if not audio_path.exists():
        raise typer.BadParameter(f"missing {audio_path}; run `just clean` first")

    out_dir = root / "data" / "datasets" / voice
    wavs_dir = out_dir / "wavs"
    wavs_dir.mkdir(parents=True, exist_ok=True)

    dev, compute_type = pick_device(device)
    typer.echo(f"whisperx {model} on {dev} ({compute_type})")

    asr = whisperx.load_model(model, dev, compute_type=compute_type, language=language)
    waveform = whisperx.load_audio(str(audio_path))
    result = asr.transcribe(waveform, batch_size=batch_size)

    # Alignment is the whole point: it gives word-accurate boundaries to cut on.
    align_model, meta = whisperx.load_align_model(language_code=result["language"], device=dev)
    result = whisperx.align(
        result["segments"], align_model, meta, waveform, dev, return_char_alignments=False
    )

    full = AudioSegment.from_wav(audio_path)
    rows: list[tuple[str, str, float]] = []

    for i, seg in enumerate(tqdm(result["segments"], desc="cutting")):
        start_ms, end_ms = int(seg["start"] * 1000), int(seg["end"] * 1000)
        text = seg["text"].strip()
        duration = (end_ms - start_ms) / 1000
        if not (min_sec < duration < max_sec) or len(text) < 6:
            continue
        filename = f"seg_{i:04d}.wav"
        full[start_ms:end_ms].export(wavs_dir / filename, format="wav")
        rows.append((filename, text, duration))

    if not rows:
        raise typer.Exit(code=1)

    with (out_dir / "metadata.csv").open("w", encoding="utf-8") as fh:
        for filename, text, _ in rows:
            fh.write(f"{filename}|{text}\n")

    # Instant cloning wants a handful of clean, reasonably long clips rather
    # than the whole dataset. Longest segments carry the most prosody.
    references = sorted(rows, key=lambda r: r[2], reverse=True)[:reference_count]
    voice_profile = {
        "name": voice,
        "engine": "xtts_v2",
        "language": language,
        "sample_rate": 24000,
        "mode": "instant",
        "model_dir": None,
        "reference_wavs": [str(Path("data/datasets") / voice / "wavs" / r[0]) for r in references],
        "dataset_dir": str(Path("data/datasets") / voice),
        "segment_count": len(rows),
        "total_minutes": round(sum(r[2] for r in rows) / 60, 2),
    }
    voices_dir = root / "data" / "voices"
    voices_dir.mkdir(parents=True, exist_ok=True)
    (voices_dir / f"{voice}.json").write_text(
        json.dumps(voice_profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    typer.echo(
        f"{len(rows)} segments ({voice_profile['total_minutes']} min) -> {out_dir}\n"
        f"voice profile -> data/voices/{voice}.json"
    )


if __name__ == "__main__":
    app()