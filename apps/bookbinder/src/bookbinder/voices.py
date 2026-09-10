"""Judge a voice recording before anything is cloned from it.

Cloning takes minutes and downloads a speech model on the first run, and a
recording that was never going to work fails at the end of all of that, or
worse produces a voice that sounds wrong for reasons nobody can name. Most of
those reasons are visible in the file itself: it is too short, it is clipped,
it is mostly silence, or it is a stereo interview with two people in it.

This is deliberately in the bookbinder environment. It is ffmpeg and
arithmetic, with no model and no torch, so it can run before the decision to
spend time on a voice has been made.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Below this there is not enough speech to characterise a voice. XTTS wants
# several seconds of clean reference; less than this and the clone is a
# lottery.
MIN_USABLE_SECONDS = 20.0

# Comfortable. More is better up to a point, and past this the extra costs
# labelling time without improving the clone.
GOOD_SECONDS = 120.0

# A peak this close to full scale means samples were almost certainly cut off.
# Clipping survives cloning and is audible in every fragment of the book.
CLIPPING_DBFS = -0.5

# Real speech peaks well above this. Below it the file is effectively silent,
# whatever its length says.
SILENT_DBFS = -50.0

# Most of a recording being silence usually means a long lead-in, a pause the
# speaker never filled, or the wrong file entirely.
MOSTLY_SILENCE = 0.6


@dataclass
class VoiceProbe:
    """What a recording is, and whether it is worth cloning from."""

    path: str = ""
    seconds: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    peak_dbfs: float | None = None
    silence_ratio: float = 0.0
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return not self.problems


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels,duration",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    try:
        return json.loads(out.stdout or "{}")
    except ValueError:
        return {}


def _volume(path: Path) -> tuple[float | None, float]:
    """Peak level in dBFS, and the share of the file that is near-silent."""
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "volumedetect,silencedetect=noise=-40dB:d=0.5",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    peak: float | None = None
    silent = 0.0
    for line in probe.stderr.splitlines():
        if "max_volume:" in line:
            try:
                peak = float(line.split("max_volume:")[1].strip().split()[0])
            except (IndexError, ValueError):
                peak = None
        elif "silence_duration:" in line:
            try:
                silent += float(line.split("silence_duration:")[1].strip().split()[0])
            except (IndexError, ValueError):
                continue
    return peak, silent


def probe(path: Path) -> VoiceProbe:
    """Everything about a recording that can be known without a model."""
    result = VoiceProbe(path=str(path))
    if not path.is_file():
        result.problems.append(f"no file at {path}")
        return result

    meta = _ffprobe(path)
    streams = meta.get("streams") or []
    if not streams:
        result.problems.append("no audio stream; is this really a recording?")
        return result

    stream = streams[0]
    result.sample_rate = int(stream.get("sample_rate") or 0)
    result.channels = int(stream.get("channels") or 0)
    result.seconds = float(stream.get("duration")
                           or (meta.get("format") or {}).get("duration") or 0.0)

    peak, silent_seconds = _volume(path)
    result.peak_dbfs = peak
    if result.seconds > 0:
        result.silence_ratio = round(min(1.0, silent_seconds / result.seconds), 3)

    if result.seconds < MIN_USABLE_SECONDS:
        result.problems.append(
            f"only {result.seconds:.1f}s of audio; a clone needs at least "
            f"{MIN_USABLE_SECONDS:.0f}s of speech to work from")
    elif result.seconds < GOOD_SECONDS:
        result.warnings.append(
            f"{result.seconds:.0f}s is enough to try, but {GOOD_SECONDS:.0f}s or "
            f"more gives the clone something to average over")

    if peak is None:
        result.warnings.append("could not measure the level; ffmpeg read no volume")
    elif peak <= SILENT_DBFS:
        result.problems.append(
            f"peaks at {peak:.1f} dBFS, which is silence. Check the recording "
            f"actually captured anything")
    elif peak >= CLIPPING_DBFS:
        result.warnings.append(
            f"peaks at {peak:.1f} dBFS, so samples are probably clipped. Clipping "
            f"survives cloning and is audible in every fragment")

    if result.silence_ratio >= MOSTLY_SILENCE:
        result.warnings.append(
            f"{result.silence_ratio:.0%} of the file is silence; trim it or expect "
            f"the usable reference to be much shorter than the duration suggests")

    if result.channels > 1:
        result.warnings.append(
            f"{result.channels} channels. Cleaning mixes to mono, which is right "
            f"for one speaker and wrong for an interview with two")

    return result


def report(result: VoiceProbe) -> str:
    """The probe as a person needs to read it before spending time cloning."""
    lines = [Path(result.path).name, ""]
    lines.append(f"  {result.seconds:.1f}s · {result.sample_rate} Hz · "
                 f"{result.channels} channel(s)")
    if result.peak_dbfs is not None:
        lines.append(f"  peaks at {result.peak_dbfs:.1f} dBFS · "
                     f"{result.silence_ratio:.0%} silence")
    for problem in result.problems:
        lines.append(f"  will not work: {problem}")
    for warning in result.warnings:
        lines.append(f"  worth knowing: {warning}")
    if result.usable and not result.warnings:
        lines.append("  nothing to report; this should clone cleanly")
    return "\n".join(lines)


def main() -> None:
    """`just probe <recording>`."""
    import sys

    if len(sys.argv) < 2:
        print("usage: probe <recording>", file=sys.stderr)
        raise SystemExit(2)
    result = probe(Path(sys.argv[1]))
    print(report(result))
    raise SystemExit(0 if result.usable else 1)


if __name__ == "__main__":
    main()
