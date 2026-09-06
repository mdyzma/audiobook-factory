"""Stage 4 - render every chunk in the manifest to a wav file.

Resumable by design: a book is tens of thousands of chunks and a crash at
hour six should not cost the first six hours. Existing wavs are skipped, so
re-running the command continues where it stopped.

Multi-voice aware. Each chunk carries a `role`, and `book.json` carries the
role-to-voice mapping the chunker resolved. Speaker latents are loaded once
per voice, not once per chunk, which is the difference between a book that
renders overnight and one that does not.

Reads data/book/<slug>/chunks.jsonl, writes data/audio/<slug>/<chunk id>.wav,
rendered.jsonl with real durations, and report.json describing the run.
"""

from __future__ import annotations

import json
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import torch
import typer

from narrator.engine import (
    VoiceProfile,
    compute_latents,
    load_latents,
    load_model,
    pick_device,
)

app = typer.Typer(add_completion=False)

# narrator cannot import bookbinder (different environments, incompatible
# numpy), so the report shape is mirrored here. It is validated on the
# bookbinder side; docs/schemas/render_report_v1.json is the contract.
SCHEMA_VERSION = 1


def load_synth_config(root: Path) -> dict:
    path = root / "config" / "pipeline.toml"
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get("synth", {})


class VoicePool:
    """Loads each voice once and keeps its latents.

    XTTS weights are shared across voices; only the speaker latents differ.
    So one model is loaded, and the pool holds a latent pair per voice.
    """

    def __init__(self, root: Path, device: str) -> None:
        self.root = root
        self.device = device
        self._model = None
        self._latents: dict[str, tuple] = {}
        self._profiles: dict[str, VoiceProfile] = {}

    def profile(self, voice: str) -> VoiceProfile:
        if voice not in self._profiles:
            self._profiles[voice] = VoiceProfile.load(self.root, voice)
        return self._profiles[voice]

    def model(self, voice: str):
        if self._model is None:
            self._model = load_model(self.profile(voice), self.device)
        return self._model

    def latents(self, voice: str):
        if voice not in self._latents:
            profile = self.profile(voice)
            cached = load_latents(self.root, voice, self.device)
            if cached is None:
                cached = compute_latents(self.model(voice), profile)
            self._latents[voice] = cached
        return self._latents[voice]


@app.command()
def main(
    slug: str = typer.Argument(..., help="Book slug under data/book/"),
    voice: str = typer.Option("", help="Override the cast and use one voice throughout"),
    device: str = typer.Option("auto", help="auto | cuda | mps | cpu"),
    limit: int = typer.Option(0, help="Render only the first N chunks (smoke test)"),
    force: bool = typer.Option(False, help="Re-render chunks that already have audio"),
) -> None:
    import soundfile as sf
    from tqdm import tqdm

    root = Path(__file__).resolve().parents[3]
    book_dir = root / "data" / "book" / slug
    chunks_path = book_dir / "chunks.jsonl"
    if not chunks_path.exists():
        raise typer.BadParameter(f"missing {chunks_path}; run `just chunk {slug}` first")

    book = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    cast: dict[str, str] = book.get("cast") or {}

    chunks = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit:
        chunks = chunks[:limit]

    def voice_for(chunk: dict) -> str:
        if voice:
            return voice
        return cast.get(chunk.get("role", "narrator")) or cast.get("narrator") or ""

    voices = sorted({voice_for(c) for c in chunks})
    if not all(voices):
        raise typer.BadParameter(
            f"no voice for some chunks; `just chunk {slug}` records the cast, "
            f"or pass --voice to override"
        )

    dev = pick_device(device)
    cfg = load_synth_config(root)
    out_dir = root / "data" / "audio" / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    typer.echo(
        f"synthesising {len(chunks)} chunks on {dev}\n"
        f"voices: {', '.join(voices)}"
    )

    pool = VoicePool(root, dev)
    started = time.time()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rendered: list[dict] = []
    failures: list[dict] = []
    skipped = 0
    audio_seconds = 0.0

    for chunk in tqdm(chunks, desc="synth"):
        wav_path = out_dir / f"{chunk['id']}.wav"
        chunk_voice = voice_for(chunk)

        if wav_path.exists() and not force:
            info = sf.info(wav_path)
            chunk["audio_path"] = str(wav_path.relative_to(root))
            chunk["duration_sec"] = round(info.duration, 3)
            audio_seconds += info.duration
            skipped += 1
            rendered.append(chunk)
            continue

        try:
            profile = pool.profile(chunk_voice)
            gpt_cond_latent, speaker_embedding = pool.latents(chunk_voice)
            out = pool.model(chunk_voice).inference(
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
            failures.append({"chunk_id": chunk["id"], "error": f"{type(exc).__name__}: {exc}"})
            continue

        sf.write(wav_path, out["wav"], profile.sample_rate)
        duration = len(out["wav"]) / profile.sample_rate
        chunk["audio_path"] = str(wav_path.relative_to(root))
        chunk["duration_sec"] = round(duration, 3)
        audio_seconds += duration
        rendered.append(chunk)

    manifest_path = out_dir / "rendered.jsonl"
    with manifest_path.open("w", encoding="utf-8") as fh:
        for chunk in rendered:
            fh.write(json.dumps(chunk, ensure_ascii=False) + "\n")

    elapsed = time.time() - started
    report = {
        "schema_version": SCHEMA_VERSION,
        "slug": slug,
        "voice": voice,
        "cast": cast,
        "device": dev,
        "dry_run": False,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_sec": round(elapsed, 3),
        "chunks_total": len(chunks),
        "chunks_rendered": len(rendered) - skipped,
        "chunks_skipped": skipped,
        "audio_sec": round(audio_seconds, 3),
        "failures": failures,
        # Computed on the bookbinder side too, but written here so the file is
        # readable on its own. test_schemas pins the two shapes together.
        "realtime_factor": round(audio_seconds / elapsed, 2) if elapsed else 0.0,
        "ok": not failures and len(rendered) == len(chunks),
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    typer.echo(
        f"rendered {len(rendered)}/{len(chunks)} chunks "
        f"({skipped} already present), {audio_seconds / 3600:.2f} h of audio "
        f"in {elapsed / 60:.1f} min ({audio_seconds / elapsed:.1f}x realtime)\n"
        f"-> {manifest_path}\n-> {report_path}"
    )
    if failures:
        typer.echo(f"{len(failures)} chunks failed; see {report_path}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
