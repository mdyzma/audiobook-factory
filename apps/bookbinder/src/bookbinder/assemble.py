"""Stage 5 - concatenate rendered fragments into a finished audiobook.

Inserts each chunk's pause_after_ms as real silence, derives chapter marks
from the accumulated durations, and muxes an m4b with embedded chapters and
tags. Uses an ffmpeg concat list rather than loading audio into memory, so a
twenty-hour book costs nothing in RAM.

Reads data/audio/<slug>/rendered.jsonl, writes data/out/<slug>.<format>.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import tomllib
from collections import Counter
from pathlib import Path

import typer

from bookbinder.paths import project_root
from bookbinder.fingerprint import fragment_fingerprint, voice_revision
from bookbinder.manifest import BookMeta, is_dry_run_audio, read_book

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


# Below this, a fragment holds no speech. Real narration peaks near -1 dB;
# ffmpeg reports -91 dB for digital silence.
SILENCE_DBFS = -60.0

# Enough fragments to be certain, few enough to stay instant on a long book.
SILENCE_SAMPLE = 12


def peak_dbfs(path: Path) -> float | None:
    """Loudest sample in a wav, in dBFS. None if ffmpeg cannot read it."""
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in probe.stderr.splitlines():
        if "max_volume:" in line:
            try:
                return float(line.split("max_volume:")[1].strip().split()[0])
            except (IndexError, ValueError):
                return None
    return None


def all_silent(paths: list[Path]) -> bool:
    """True when every fragment sampled is silence.

    The marker file catches the usual cause, a dry run left in place. This
    catches the rest: audio from an older version with no marker, a voice that
    rendered to nothing, a truncated write. It samples rather than reads
    everything, because being sure costs a full pass over hours of audio and
    twelve fragments spread across a book already settles the question.
    """
    if not paths:
        return False
    step = max(1, len(paths) // SILENCE_SAMPLE)
    peaks = [peak_dbfs(p) for p in paths[::step][:SILENCE_SAMPLE] if p.exists()]
    heard = [p for p in peaks if p is not None]
    return bool(heard) and all(p <= SILENCE_DBFS for p in heard)


# Enough ids to recognise the gap, few enough that a broken book does not
# print ten thousand lines.
LISTED_IDS = 5


def _listed(ids: list[str]) -> str:
    shown = ", ".join(ids[:LISTED_IDS])
    return f"{shown}, ..." if len(ids) > LISTED_IDS else shown


def planned_chunks(book_dir: Path) -> list[dict]:
    """The fragments the chunker planned, in order. Empty when unknown."""
    path = book_dir / "chunks.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stale_fragments(
    root: Path, rendered: list[dict], planned: dict[str, dict], book: BookMeta
) -> list[str]:
    """Fragments whose audio was not made from what the book now says.

    The fingerprint recorded when a fragment was written is compared against
    one recomputed from the current plan. That is what catches a book
    re-chunked after a text correction, or half-rendered under a different
    model: both leave a full set of readable files with exactly the right
    names, which is all the completeness check above can see.

    Each fragment is checked against the voice it was actually rendered with,
    so an intentional `--voice` override is not mistaken for drift. Whether
    that voice still matches the cast is a separate question, and the recorded
    voice is what makes it answerable.

    Fragments written before fingerprinting existed carry none and are let
    through, rather than forcing a re-render of every book already on disk.
    """
    revisions: dict[str, str] = {}
    stale: list[str] = []
    for chunk in rendered:
        recorded = chunk.get("fingerprint") or ""
        current = planned.get(chunk["id"])
        if not recorded or current is None:
            continue

        voice = chunk.get("voice") or ""
        if voice not in revisions:
            revisions[voice] = voice_revision(root, voice)

        expected = fragment_fingerprint(
            text=current["text"],
            language=current.get("language", ""),
            model=book.model.identity,
            voice=voice,
            voice_revision=revisions[voice],
            settings=dict(book.model.settings),
        )
        if recorded != expected:
            stale.append(chunk["id"])
    return stale


def completeness_problems(root: Path, chunks: list[dict], planned: list[str]) -> list[str]:
    """Every reason this book is not ready to assemble.

    A render that fails partway writes only the fragments it managed, and a
    deleted wav leaves the manifest pointing at nothing. Both used to assemble
    into a shorter book whose chapter marks are quietly wrong, with the loss
    reported only as a line on stderr. Checked before any ffmpeg work runs.
    """
    seen = [c["id"] for c in chunks]
    counts = Counter(seen)
    known = set(seen)
    problems: list[str] = []

    duplicates = sorted(i for i, n in counts.items() if n > 1)
    if duplicates:
        problems.append(
            f"{len(duplicates)} fragment(s) appear more than once: {_listed(duplicates)}")

    if planned:
        absent = [i for i in planned if i not in known]
        if absent:
            problems.append(
                f"{len(absent)} of {len(planned)} planned fragment(s) were never "
                f"rendered: {_listed(absent)}")
        expected = set(planned)
        unknown = [i for i in dict.fromkeys(seen) if i not in expected]
        if unknown:
            problems.append(
                f"{len(unknown)} rendered fragment(s) are not in the chunk plan, so "
                f"the text has changed since the render: {_listed(unknown)}")

    unreadable = [c["id"] for c in chunks
                  if not c.get("audio_path") or not (root / c["audio_path"]).exists()]
    if unreadable:
        problems.append(
            f"{len(unreadable)} fragment(s) have no audio file on disk: {_listed(unreadable)}")

    return problems


def concat_line(path: Path) -> str:
    """One entry for an ffmpeg concat list.

    The demuxer parses `'` as a quote, so a path containing an apostrophe ends
    the filename early and the render fails on a file that does not exist. The
    POSIX trick applies: close the quote, emit an escaped apostrophe, reopen.
    """
    return "file '{}'".format(path.as_posix().replace("'", r"'\''"))


# ffmetadata syntax. A newline ends an entry, so a title carrying one can add
# tags of its own; `=`, `;` and `#` are separators and `\` is the escape.
FFMETADATA_SPECIAL = re.compile(r"([=;#\\])")


def metadata_value(value: str) -> str:
    """Escape a tag value for an ffmetadata file.

    Titles and chapter names come from the ebook, which is not ours. Without
    this, an EPUB whose title contains a newline writes whatever follows it
    into the finished audiobook as further tags.
    """
    escaped = FFMETADATA_SPECIAL.sub(r"\\\1", value.replace("\r\n", "\n").replace("\r", "\n"))
    return escaped.replace("\n", "\\\n")


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
    root = project_root()
    audio_dir = root / "data" / "audio" / slug
    rendered_path = audio_dir / "rendered.jsonl"
    if not rendered_path.exists():
        raise typer.BadParameter(f"missing {rendered_path}; run `just synth` first")

    # `just book-dry` assembles silence on purpose, so this is a warning and not
    # an error. It exists because the resulting file is a normal-looking
    # audiobook of the right length, and the only other way to find out is to
    # press play.
    dry = is_dry_run_audio(audio_dir)
    if dry:
        typer.echo(
            f"warning: data/audio/{slug}/ is dry-run silence, so this file will "
            f"be silent. Run `just synth {slug}` for real audio.",
            err=True,
        )

    cfg = load_config(root, "assemble")
    fmt = fmt or cfg.get("format", "m4b")
    bitrate = bitrate or cfg.get("bitrate", "64k")
    sample_rate = cfg.get("sample_rate", 24000)
    channels = cfg.get("channels", 1)

    book_dir = root / "data" / "book" / slug
    book = read_book(book_dir / "book.json")
    chunks = [json.loads(l) for l in rendered_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    chunks.sort(key=lambda c: c["order"])

    plan = planned_chunks(book_dir)
    problems = completeness_problems(root, chunks, [c["id"] for c in plan])
    stale = stale_fragments(root, chunks, {c["id"]: c for c in plan}, book)
    if stale:
        problems.append(
            f"{len(stale)} fragment(s) were rendered from different text, a "
            f"different model or different settings than the book now uses: "
            f"{_listed(stale)}")
    if problems:
        raise typer.BadParameter(
            f"'{slug}' is not ready to assemble:\n"
            + "".join(f"  - {p}\n" for p in problems)
            + f"Finish the render with `just synth {slug}`, or repair named "
            f"fragments with `just resynth {slug} <ids>`."
        )

    # Second line of defence, and the one that does not depend on knowing why.
    # Skipped when the marker already said so, to avoid warning twice.
    if not dry and all_silent([root / c["audio_path"] for c in chunks if c.get("audio_path")]):
        dry = True
        typer.echo(
            f"warning: every fragment sampled from data/audio/{slug}/ is silent, "
            f"so this file will be too. Re-run `just synth {slug}`.",
            err=True,
        )

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

            # Guaranteed present: completeness_problems refused the run otherwise.
            wav = root / chunk["audio_path"]
            concat_lines.append(concat_line(wav))
            clock += chunk.get("duration_sec") or 0.0

            pause = int(chunk.get("pause_after_ms") or 0)
            if pause > 0:
                if pause not in silence_cache:
                    silence_path = tmp_dir / f"sil_{pause}.wav"
                    make_silence(silence_path, pause, sample_rate, channels)
                    silence_cache[pause] = silence_path
                concat_lines.append(concat_line(silence_cache[pause]))
                clock += pause / 1000

        list_path = tmp_dir / "concat.txt"
        list_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

        # ffmetadata carries chapter marks and tags into the container.
        meta_lines = [
            ";FFMETADATA1",
            f"title={metadata_value(book.title)}",
            f"artist={metadata_value(book.author)}",
            f"album={metadata_value(book.title)}",
            f"genre=Audiobook",
            f"language={metadata_value(book.language)}",
        ]
        for i, (_, title, start) in enumerate(chapter_marks):
            end = chapter_marks[i + 1][2] if i + 1 < len(chapter_marks) else clock
            meta_lines += [
                "[CHAPTER]", "TIMEBASE=1/1000",
                f"START={int(start * 1000)}", f"END={int(end * 1000)}",
                f"title={metadata_value(title)}",
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
        f"-> {out_path}" + ("  (silence: dry run)" if dry else "")
    )


if __name__ == "__main__":
    app()