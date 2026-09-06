"""Stage 5 - concatenate rendered fragments into a finished audiobook.

Inserts each chunk's pause_after_ms as real silence, derives chapter marks
from the accumulated durations, and muxes an m4b with embedded chapters and
tags. Uses an ffmpeg concat list rather than loading audio into memory, so a
twenty-hour book costs nothing in RAM.

Reads data/audio/<slug>/rendered.jsonl, writes data/out/<slug>.<format>.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
from pathlib import Path

import typer

from bookbinder.manifest import read_book

app = typer.Typer(add_completion=False)


def load_config(root: Path, section: str) -> dict:
    path = root / "config" / "pipeline.toml"
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get(section, {})


def make_silence(path: Path, ms: int, sample_rate: int, channels: int) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={'mono' if channels == 1 else 'stereo'}",
         "-t", f"{ms / 1000:.3f}", "-c:a", "pcm_s16le", str(path)],
        check=True,
    )


def format_timestamp(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


@app.command()
def main(
    slug: str = typer.Argument(..., help="Book slug"),
    fmt: str = typer.Option("", help="m4b | mp3 | wav; defaults to config"),
    bitrate: str = typer.Option("", help="Defaults to config"),
) -> None:
    root = Path(__file__).resolve().parents[3]
    audio_dir = root / "data" / "audio" / slug
    rendered_path = audio_dir / "rendered.jsonl"
    if not rendered_path.exists():
        raise typer.BadParameter(f"missing {rendered_path}; run `just synth` first")

    cfg = load_config(root, "assemble")
    fmt = fmt or cfg.get("format", "m4b")
    bitrate = bitrate or cfg.get("bitrate", "64k")
    sample_rate = cfg.get("sample_rate", 24000)
    channels = cfg.get("channels", 1)

    book = read_book(root / "data" / "book" / slug / "book.json")
    chunks = [json.loads(l) for l in rendered_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    chunks.sort(key=lambda c: c["order"])

    out_dir = root / "data" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.{fmt}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        silence_cache: dict[int, Path] = {}
        concat_lines: list[str] = []
        chapter_marks: list[tuple[int, str, float]] = []
        clock = 0.0
        current_chapter = None

        for chunk in chunks:
            if chunk["chapter_index"] != current_chapter:
                current_chapter = chunk["chapter_index"]
                chapter_marks.append((current_chapter, chunk["chapter_title"], clock))

            wav = root / chunk["audio_path"]
            if not wav.exists():
                typer.echo(f"missing audio for {chunk['id']}, skipping", err=True)
                continue
            concat_lines.append(f"file '{wav.as_posix()}'")
            clock += chunk.get("duration_sec") or 0.0

            pause = int(chunk.get("pause_after_ms") or 0)
            if pause > 0:
                if pause not in silence_cache:
                    silence_path = tmp_dir / f"sil_{pause}.wav"
                    make_silence(silence_path, pause, sample_rate, channels)
                    silence_cache[pause] = silence_path
                concat_lines.append(f"file '{silence_cache[pause].as_posix()}'")
                clock += pause / 1000

        list_path = tmp_dir / "concat.txt"
        list_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

        # ffmetadata carries chapter marks and tags into the container.
        meta_lines = [
            ";FFMETADATA1",
            f"title={book.title}",
            f"artist={book.author}",
            f"album={book.title}",
            f"genre=Audiobook",
            f"language={book.language}",
        ]
        for i, (_, title, start) in enumerate(chapter_marks):
            end = chapter_marks[i + 1][2] if i + 1 < len(chapter_marks) else clock
            meta_lines += [
                "[CHAPTER]", "TIMEBASE=1/1000",
                f"START={int(start * 1000)}", f"END={int(end * 1000)}",
                f"title={title}",
            ]
        meta_path = tmp_dir / "chapters.txt"
        meta_path.write_text("\n".join(meta_lines) + "\n", encoding="utf-8")

        codec = {"m4b": ["-c:a", "aac", "-b:a", bitrate],
                 "mp3": ["-c:a", "libmp3lame", "-b:a", bitrate],
                 "wav": ["-c:a", "pcm_s16le"]}[fmt]

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-i", str(meta_path), "-map_metadata", "1", "-map_chapters", "1",
            "-ar", str(sample_rate), "-ac", str(channels), *codec,
        ]
        if fmt == "m4b":
            cmd += ["-f", "mp4"]
        cmd.append(str(out_path))
        subprocess.run(cmd, check=True)

    (out_dir / f"{slug}.chapters.json").write_text(
        json.dumps([{"index": i, "title": t, "start_sec": round(s, 3)}
                    for i, t, s in chapter_marks], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    size_mb = out_path.stat().st_size / 1024 / 1024
    typer.echo(
        f"{book.title} - {book.author}\n"
        f"{len(chapter_marks)} chapters, {format_timestamp(clock)}, {size_mb:.1f} MB\n"
        f"-> {out_path}"
    )


if __name__ == "__main__":
    app()
