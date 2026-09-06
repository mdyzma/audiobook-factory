"""Render a book to silence, at the right durations.

Synthesis is the slow, heavy part: it needs a 1.6 GB environment, model
weights, and on an M1 about twelve hours for a ten-hour book. Most of what can
go wrong, though, is structural rather than acoustic - a chapter mark in the
wrong place, a role mapped to a voice that does not exist, pauses that do not
add up.

This produces silence of each chunk's estimated duration, writes the same
`rendered.jsonl` the narrator writes, and so lets `just assemble` run against
it. The whole pipeline becomes checkable in seconds with no torch installed,
which is also what lets CI cover it.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import typer

from bookbinder.cast import Cast
from bookbinder.manifest import RenderFailure, RenderReport, read_book, read_chunks

app = typer.Typer(add_completion=False)

MIN_SILENCE_SEC = 0.2


def write_silence(path: Path, seconds: float, sample_rate: int, channels: int) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi",
         "-i", f"anullsrc=r={sample_rate}:cl={'mono' if channels == 1 else 'stereo'}",
         "-t", f"{max(seconds, MIN_SILENCE_SEC):.3f}", "-c:a", "pcm_s16le", str(path)],
        check=True,
    )


@app.command()
def main(
    slug: str = typer.Argument(..., help="Book slug under data/book/"),
    sample_rate: int = typer.Option(24000),
    channels: int = typer.Option(1),
    strict: bool = typer.Option(
        False,
        help="Exit non-zero when a role maps to a voice that has no profile. "
             "Off by default so the structure can be checked before any voice "
             "is cloned.",
    ),
) -> None:
    root = Path(__file__).resolve().parents[3]
    book_dir = root / "data" / "book" / slug
    chunks_path = book_dir / "chunks.jsonl"
    if not chunks_path.exists():
        raise typer.BadParameter(f"missing {chunks_path}; run `just chunk {slug}` first")

    book = read_book(book_dir / "book.json")
    chunks = list(read_chunks(chunks_path))
    out_dir = root / "data" / "audio" / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    report = RenderReport(
        slug=slug, voice=book.voice, cast=book.cast, device="none", dry_run=True,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        chunks_total=len(chunks),
    )

    # A role with no voice profile is a silent mis-cast at render time, so it
    # is worth surfacing here. It is only fatal under --strict, because the
    # usual reason to dry-run is to check structure before cloning any voice.
    cast_path = root / "config" / "cast.yml"
    cast = Cast.load(cast_path) if cast_path.exists() else None
    missing: dict[str, str] = {}
    for role in sorted({c.role for c in chunks}):
        voice = book.cast.get(role) or (cast.voice_for(role) if cast else "")
        if voice and not (root / "data" / "voices" / f"{voice}.json").exists():
            missing[role] = voice
    if strict:
        for role, voice in missing.items():
            report.failures.append(RenderFailure(
                chunk_id=f"role:{role}",
                error=f"role '{role}' maps to voice '{voice}', "
                      f"but data/voices/{voice}.json does not exist",
            ))

    lines = []
    for chunk in chunks:
        wav = out_dir / f"{chunk.id}.wav"
        try:
            write_silence(wav, chunk.est_seconds, sample_rate, channels)
        except subprocess.CalledProcessError as exc:
            report.failures.append(RenderFailure(chunk_id=chunk.id, error=str(exc)))
            continue
        chunk.audio_path = str(wav.relative_to(root))
        chunk.duration_sec = round(max(chunk.est_seconds, MIN_SILENCE_SEC), 3)
        report.chunks_rendered += 1
        report.audio_sec += chunk.duration_sec
        lines.append(chunk.model_dump_json())

    (out_dir / "rendered.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report.elapsed_sec = round(time.time() - started, 3)
    report_path = report.write(out_dir / "report.json")

    typer.echo(
        f"dry run: {report.chunks_rendered}/{report.chunks_total} chunks, "
        f"{report.audio_sec / 60:.1f} min of silence in {report.elapsed_sec:.1f} s\n"
        f"cast: " + (", ".join(f"{r}->{v}" for r, v in book.cast.items()) or "none") + "\n"
        f"-> {report_path}"
    )
    for role, voice in missing.items():
        typer.echo(
            f"  note: role '{role}' -> voice '{voice}', not cloned yet "
            f"(just voice <sample> {voice})",
            err=True,
        )
    if report.failures:
        for failure in report.failures:
            typer.echo(f"  {failure.chunk_id}: {failure.error}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
