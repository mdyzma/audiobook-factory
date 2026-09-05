"""Stage 4 - render every chunk in the manifest to a wav file.

Resumable by design: a book is tens of thousands of chunks and a crash at
hour six should not cost the first six hours. Existing wavs are skipped, so
re-running the command continues where it stopped.

Reads data/book/<slug>/chunks.jsonl, writes data/audio/<slug>/<chunk id>.wav
and rendered.jsonl carrying the real per-chunk durations that stage 5 needs
for chapter marks.
"""

from __future__ import annotations

import json
import sys
import time
import tomllib
from pathlib import Path

import torch
import typer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from narrator.engine import VoiceProfile, compute_latents, load_model, pick_device  # noqa: E402

app = typer.Typer(add_completion=False)


def load_synth_config(root: Path) -> dict:
    path = root / "config" / "pipeline.toml"
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get("synth", {})


@app.command()
def main(
    slug: str = typer.Argument(..., help="Book slug under data/book/"),
    voice: str = typer.Option(..., help="Voice name"),
    device: str = typer.Option("auto", help="auto | cuda | mps | cpu"),
    limit: int = typer.Option(0, help="Render only the first N chunks (smoke test)"),
    force: bool = typer.Option(False, help="Re-render chunks that already have audio"),
) -> None:
    import soundfile as sf
    from tqdm import tqdm

    root = Path(__file__).resolve().parents[3]
    chunks_path = root / "data" / "book" / slug / "chunks.jsonl"
    if not chunks_path.exists():
        raise typer.BadParameter(f"missing {chunks_path}; run `just chunk {slug}` first")

    out_dir = root / "data" / "audio" / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit:
        chunks = chunks[:limit]

    profile = VoiceProfile.load(root, voice)
    dev = pick_device(device)
    cfg = load_synth_config(root)
    typer.echo(f"synthesising {len(chunks)} chunks as '{voice}' on {dev}")

    model = load_model(profile, dev)
    tts_model, gpt_cond_latent, speaker_embedding = compute_latents(model, profile)

    # Reuse cached latents when clone.py already produced them.
    latents_path = root / "data" / "voices" / voice / "latents.pt"
    if latents_path.exists():
        cached = torch.load(latents_path, map_location=dev)
        gpt_cond_latent = cached["gpt_cond_latent"].to(dev)
        speaker_embedding = cached["speaker_embedding"].to(dev)

    rendered: list[dict] = []
    started = time.time()
    failures: list[str] = []

    for chunk in tqdm(chunks, desc="synth"):
        wav_path = out_dir / f"{chunk['id']}.wav"
        if wav_path.exists() and not force:
            info = sf.info(wav_path)
            chunk["audio_path"] = str(wav_path.relative_to(root))
            chunk["duration_sec"] = round(info.duration, 3)
            rendered.append(chunk)
            continue

        try:
            out = tts_model.inference(
                chunk["text"],
                chunk.get("language", profile.language),
                gpt_cond_latent,
                speaker_embedding,
                temperature=cfg.get("temperature", 0.70),
                length_penalty=cfg.get("length_penalty", 1.0),
                repetition_penalty=cfg.get("repetition_penalty", 2.0),
                top_k=cfg.get("top_k", 50),
                top_p=cfg.get("top_p", 0.85),
                speed=cfg.get("speed", 1.0),
            )
        except Exception as exc:  # a single bad chunk must not kill the run
            failures.append(f"{chunk['id']}: {exc}")
            continue

        sf.write(wav_path, out["wav"], profile.sample_rate)
        chunk["audio_path"] = str(wav_path.relative_to(root))
        chunk["duration_sec"] = round(len(out["wav"]) / profile.sample_rate, 3)
        rendered.append(chunk)

    manifest_path = out_dir / "rendered.jsonl"
    with manifest_path.open("w", encoding="utf-8") as fh:
        for chunk in rendered:
            fh.write(json.dumps(chunk, ensure_ascii=False) + "\n")

    audio_seconds = sum(c.get("duration_sec") or 0 for c in rendered)
    elapsed = time.time() - started
    typer.echo(
        f"rendered {len(rendered)}/{len(chunks)} chunks, "
        f"{audio_seconds / 3600:.2f} h of audio in {elapsed / 60:.1f} min "
        f"({audio_seconds / elapsed:.1f}x realtime)\n-> {manifest_path}"
    )
    if failures:
        (out_dir / "failures.txt").write_text("\n".join(failures), encoding="utf-8")
        typer.echo(f"{len(failures)} chunks failed; see {out_dir / 'failures.txt'}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
