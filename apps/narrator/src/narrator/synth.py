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
import os
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import torch
import typer

if TYPE_CHECKING:  # heavy imports stay out of the runtime path
    from TTS.tts.models.xtts import Xtts

from narrator.paths import project_root
from narrator.engine import (
    VoiceProfile,
    checkpoint_key,
    compute_latents,
    load_latents,
    load_model,
    pick_device,
)

app = typer.Typer(add_completion=False)

# narrator cannot import bookbinder (different environments, incompatible
# numpy), so the report shape is mirrored here. It is validated on the
# bookbinder side; docs/schemas/render_report_v3.json is the contract.
SCHEMA_VERSION = 3

# Mirrors bookbinder.manifest.DRY_RUN_MARKER. A dry run leaves silence at
# exactly the paths a real render writes, and resume skips any fragment that
# already has a wav, so without this the two combine into a book-length file of
# nothing. Keep the name in step with bookbinder.
DRY_RUN_MARKER = ".dry-run.json"


# How often progress.json is rewritten. Synthesising a fragment takes seconds,
# so per-fragment writes are free; skipped fragments are near-instant on a
# resumed run, hence the throttle.
PROGRESS_INTERVAL_SEC = 0.5


def discard_dry_run(out_dir: Path) -> int:
    """Remove dry-run silence so resume cannot mistake it for narration.

    A dry run writes a wav per fragment at the estimated duration, which is
    what makes it useful for checking structure and what makes it dangerous
    here: the resume rule below is `skip anything that already has a wav`, so
    silence left in place would be adopted wholesale and the book would come
    out empty. Returns the number of files removed.
    """
    marker = out_dir / DRY_RUN_MARKER
    if not marker.exists():
        return 0
    removed = 0
    for wav in out_dir.glob("*.wav"):
        wav.unlink()
        removed += 1
    (out_dir / "rendered.jsonl").unlink(missing_ok=True)
    marker.unlink()
    return removed


def write_progress(path: Path, payload: dict) -> None:
    """Atomic, because a reader may poll this while it is being rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_synth_config(root: Path) -> dict:
    path = root / "config" / "pipeline.toml"
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get("synth", {})


class VoicePool:
    """Loads each checkpoint once and keeps a latent pair per voice.

    Instant-cloned voices all share the stock XTTS weights and differ only in
    their speaker latents, so one loaded model serves the whole cast. A
    fine-tuned voice has its own weights, so models are cached by checkpoint
    identity rather than globally: caching a single model meant the first
    voice loaded narrated every other voice in the book.
    """

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


@app.command()
def main(
    slug: str = typer.Argument(..., help="Book slug under data/book/"),
    voice: str = typer.Option("", help="Override the cast and use one voice throughout"),
    device: str = typer.Option("auto", help="auto | cuda | mps | cpu"),
    limit: int = typer.Option(0, help="Render only the first N chunks (smoke test)"),
    force: bool = typer.Option(False, help="Re-render chunks that already have audio"),
    only: str = typer.Option(
        "", help="Comma-separated chunk ids to re-render, e.g. ch002_0041. "
                 "Implies --force for those, and merges into the existing "
                 "rendered.jsonl rather than replacing it."
    ),
) -> None:
    import soundfile as sf
    from tqdm import tqdm

    root = project_root()
    book_dir = root / "data" / "book" / slug
    chunks_path = book_dir / "chunks.jsonl"
    if not chunks_path.exists():
        raise typer.BadParameter(f"missing {chunks_path}; run `just chunk {slug}` first")

    book = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    cast: dict[str, str] = book.get("cast") or {}

    all_chunks = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # `--only` re-renders named fragments, which is how a single bad one gets
    # fixed after a quality check without redoing the book.
    wanted = [c.strip() for c in only.split(",") if c.strip()]
    if wanted:
        by_id = {c["id"]: c for c in all_chunks}
        missing = [c for c in wanted if c not in by_id]
        if missing:
            raise typer.BadParameter(f"no such chunk(s): {', '.join(missing)}")
        chunks = [by_id[c] for c in wanted]
        force = True
    else:
        chunks = all_chunks[:limit] if limit else all_chunks

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

    discarded = discard_dry_run(out_dir)
    if discarded:
        typer.echo(f"discarding {discarded} dry-run silence files before rendering")

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

    # A book is hours of work and report.json only lands at the end, so this is
    # the only view into a running render. Shape mirrors bookbinder's
    # RenderProgress; docs/schemas/render_progress_v3.json is the contract.
    progress_path = out_dir / "progress.json"
    last_progress = 0.0

    def progress(chunk_id: str = "", chunk_voice: str = "", running: bool = True) -> None:
        elapsed = time.time() - started
        done = len(rendered)
        write_progress(progress_path, {
            "schema_version": SCHEMA_VERSION,
            "slug": slug,
            "running": running,
            "pid": os.getpid(),
            "dry_run": False,
            "device": dev,
            "started_at": started_at,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_sec": round(elapsed, 3),
            "chunks_total": len(chunks),
            "chunks_done": done,
            "chunks_rendered": done - skipped,
            "chunks_skipped": skipped,
            "chunks_failed": len(failures),
            "audio_sec": round(audio_seconds, 3),
            "current_chunk_id": chunk_id,
            "current_voice": chunk_voice,
            "last_error": failures[-1]["error"] if failures else "",
            "percent": round(100 * done / len(chunks), 1) if chunks else 0.0,
            "eta_sec": (
                round((len(chunks) - done) * (elapsed / (done - skipped)), 1)
                if done > skipped and len(chunks) > done else 0.0
            ),
        })

    progress()

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
            if time.time() - last_progress > PROGRESS_INTERVAL_SEC:
                progress(chunk["id"], chunk_voice)
                last_progress = time.time()
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
            progress(chunk["id"], chunk_voice)
            last_progress = time.time()
            continue

        sf.write(wav_path, out["wav"], profile.sample_rate)
        duration = len(out["wav"]) / profile.sample_rate
        chunk["audio_path"] = str(wav_path.relative_to(root))
        chunk["duration_sec"] = round(duration, 3)
        audio_seconds += duration
        rendered.append(chunk)
        progress(chunk["id"], chunk_voice)
        last_progress = time.time()

    progress(running=False)

    manifest_path = out_dir / "rendered.jsonl"
    if wanted and manifest_path.exists():
        # Only some fragments were touched, so merge rather than replace:
        # writing just these would silently throw away the rest of the book.
        existing: list[dict] = [json.loads(line) for line
                                in manifest_path.read_text(encoding="utf-8").splitlines()
                                if line.strip()]
        replaced = {c["id"]: c for c in rendered}

        merged: list[dict] = []
        for chunk in existing:
            merged.append(replaced.pop(chunk["id"], None) or chunk)
        # Anything re-rendered that was not already in the manifest.
        merged.extend(replaced.values())
        merged.sort(key=lambda c: c["order"])
        rendered_out = merged
    else:
        rendered_out = rendered

    with manifest_path.open("w", encoding="utf-8") as fh:
        for chunk in rendered_out:
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