"""Say whether there is room, before eight hours of work finds out there is not.

A twenty-hour book is roughly three and a half gigabytes of fragment audio at
24 kHz mono 16-bit, and a batch of twenty is not a rounding error on a laptop.
Running out mid-render is the worst moment to learn it: the fragments already
written are fine, the one being written is truncated, and the manifest can be
left with half a line at the end of it.

The estimate is deliberately rough and deliberately pessimistic. Nothing here
tries to be accurate to the megabyte, because the question is only whether the
whole job fits with something to spare. A reserve that leaves the machine
usable is worth more than precision: a disk with four bytes free is a disk
nobody can log in to.

Only fragments that are not yet rendered are counted, so a resumed render asks
for what it still has to write rather than for the whole book again.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

# 16-bit mono PCM, which is what the pipeline uses end to end.
BYTES_PER_SAMPLE = 2
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_BITRATE = "64k"

# Fragment durations are estimates from character counts, and a model that
# reads slowly writes a bigger file than the estimate says. This is the margin
# on top, not a guess at accuracy.
HEADROOM = 1.25

# Never plan to use the last of the disk. A render that fits exactly leaves a
# machine with nowhere to write a log, and the next stage has nowhere to work.
RESERVE_BYTES = 1 << 29        # 512 MB


class NotEnoughSpace(RuntimeError):
    """A job that would very likely run the disk out, refused before it starts."""


@dataclass(frozen=True)
class Estimate:
    """What a stage needs against what there is, and where."""

    what: str
    needed: int
    free: int
    where: Path
    reserve: int = RESERVE_BYTES

    @property
    def enough(self) -> bool:
        return self.free >= self.needed + self.reserve

    @property
    def message(self) -> str:
        if self.enough:
            return (f"{self.what} needs about {human(self.needed)}; "
                    f"{human(self.free)} free")
        return (f"{self.what} needs about {human(self.needed)} and only "
                f"{human(self.free)} is free on the disk holding {self.where}, "
                f"counting a {human(self.reserve)} reserve. Free some space, or "
                f"move the data directory to a larger disk")


def human(size: float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def free_bytes(path: Path) -> int:
    """Space on the filesystem that will hold `path`.

    Walks up to something that exists: the audio directory for a book that has
    never been rendered does not exist yet, and asking about it would raise
    rather than answer.
    """
    for candidate in [path, *path.parents]:
        if candidate.exists():
            return shutil.disk_usage(candidate).free
    return shutil.disk_usage(Path(path.anchor or ".")).free


def parse_bitrate(value: str) -> int:
    """`64k` as bits per second. Anything unreadable falls back to the default."""
    text = (value or "").strip().lower()
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 64_000


def chunk_seconds(root: Path, slug: str) -> tuple[float, float]:
    """Total and still-to-render seconds for a book.

    Read as raw JSON rather than through the manifest models: this runs before
    every synthesis and every assembly, on books of several thousand fragments,
    and it needs two fields out of each.
    """
    path = root / "data" / "book" / slug / "chunks.jsonl"
    audio_dir = root / "data" / "audio" / slug
    total = remaining = 0.0
    if not path.is_file():
        return 0.0, 0.0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            seconds = float(chunk.get("est_seconds") or 0.0)
            total += seconds
            if not (audio_dir / f"{chunk.get('id')}.wav").exists():
                remaining += seconds
    return total, remaining


def _config(root: Path, section: str) -> dict:
    from bookbinder.assemble import load_config

    return load_config(root, section)


def synthesis(root: Path, slug: str) -> Estimate:
    """Room for the fragments a render still has to write."""
    _total, remaining = chunk_seconds(root, slug)
    rate = int(_config(root, "sample").get("sample_rate") or DEFAULT_SAMPLE_RATE)
    needed = int(remaining * rate * BYTES_PER_SAMPLE * HEADROOM)
    where = root / "data" / "audio"
    return Estimate(what=f"narrating '{slug}'", needed=needed,
                    free=free_bytes(where), where=where)


def assembly(root: Path, slug: str, fmt: str = "") -> Estimate:
    """Room for the finished file.

    Assembly streams through ffmpeg rather than building one big wav first, so
    what it needs is the size of the output and nothing more.
    """
    total, _remaining = chunk_seconds(root, slug)
    cfg = _config(root, "assemble")
    chosen = fmt or str(cfg.get("format") or "m4b")
    if chosen == "wav":
        rate = int(cfg.get("sample_rate") or DEFAULT_SAMPLE_RATE)
        per_second = rate * BYTES_PER_SAMPLE
    else:
        per_second = parse_bitrate(str(cfg.get("bitrate") or DEFAULT_BITRATE)) / 8
    needed = int(total * per_second * HEADROOM)
    where = root / "data" / "out"
    return Estimate(what=f"assembling '{slug}' as {chosen}", needed=needed,
                    free=free_bytes(where), where=where)


def require(estimate: Estimate) -> Estimate:
    """Raise unless there is room. The message is written to be shown as-is."""
    if not estimate.enough:
        raise NotEnoughSpace(estimate.message)
    return estimate


def main() -> None:
    """`just preflight <slug> [synth|assemble] [format]`, and part of both recipes."""
    import sys

    from bookbinder.paths import project_root

    args = [a for a in sys.argv[1:] if a]
    if not args:
        print("usage: preflight <slug> [synth|assemble] [format]", file=sys.stderr)
        raise SystemExit(2)

    slug = args[0]
    stage = args[1] if len(args) > 1 else "synth"
    root = project_root()
    estimate = (assembly(root, slug, args[2] if len(args) > 2 else "")
                if stage == "assemble" else synthesis(root, slug))
    print(estimate.message)
    # Non-zero stops the recipe before the stage it guards.
    raise SystemExit(0 if estimate.enough else 1)


if __name__ == "__main__":
    main()
