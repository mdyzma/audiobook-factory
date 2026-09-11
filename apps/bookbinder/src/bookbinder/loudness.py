"""Measure a voice once, so a cast does not change volume mid-sentence.

A book read by a narrator and a dialogue voice that sit four decibels apart is
the most audible defect a multi-voice audiobook has. It is not subtle and it is
not fixable by turning the volume up: the difference moves with the text.

**Corrected at the voice, not at the book.** Levelling between voices has to
happen before the fragments are joined, and doing it at assembly means writing
adjusted copies of the whole book into a temporary directory, several gigabytes
of transient disk every time it is exported. A voice is cloned once, and the
audition rendered at that moment is the model's own output for it, so measuring
the audition gives a gain that costs nothing to apply while rendering.

**Recorded in the profile, which is what makes it honest.** `voice_revision`
already hashes the profile, so writing a gain into it changes the voice's
identity, every fragment made with the old level is correctly seen as stale,
and both the narrator and the assembler derive the same fingerprint from the
same file. Nothing has to agree about loudness separately.

Levelling an existing voice therefore invalidates audio already made with it,
which is right: that audio really was rendered at a different level. It happens
only when somebody asks for it, so books already finished stay finished.

Loudness is EBU R128 integrated, in LUFS, which is what audiobook shops ask
for. True peak is measured alongside it, because a gain that reaches the target
by clipping has not helped.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Audible asks for -18 to -23 LUFS with a true peak no higher than -3 dBTP.
# The middle of that range leaves room either side for a voice that turns out
# quieter or louder than the one it is being matched to.
TARGET_LUFS = -20.0
PEAK_CEILING_DBTP = -3.0

# A voice needing more than this is not quiet, it is wrong: a recording made at
# the wrong gain, or a clone that failed. Lifting it anyway would amplify the
# room it was recorded in along with the speech.
MAX_GAIN_DB = 12.0

# Below this, an integrated measurement is an opinion rather than a number.
# R128 gates on speech activity, and a clip this short may contain very little.
MIN_MEASURABLE_SEC = 2.0

# ffmpeg writes the analysis as the last JSON object on stderr.
JSON_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


class Unmeasurable(RuntimeError):
    """The file could not be measured, with a reason worth showing."""


@dataclass(frozen=True)
class Loudness:
    """What one recording measures, before anything is decided about it."""

    integrated_lufs: float
    true_peak_dbtp: float
    seconds: float
    path: str = ""

    @property
    def silent(self) -> bool:
        # ffmpeg reports -70 or lower for something with no speech in it.
        return self.integrated_lufs <= -70.0


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def measure(path: Path) -> Loudness:
    """Integrated loudness and true peak for one file."""
    if not path.is_file():
        raise Unmeasurable(f"no audio at {path}")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True)
    found = JSON_BLOCK.search(result.stderr or "")
    if not found:
        raise Unmeasurable(f"ffmpeg could not measure {path.name}")
    try:
        data = json.loads(found.group(0))
        return Loudness(integrated_lufs=float(data["input_i"]),
                        true_peak_dbtp=float(data["input_tp"]),
                        seconds=_duration(path), path=path.name)
    except (ValueError, KeyError) as exc:
        raise Unmeasurable(f"could not read the measurement of {path.name}") from exc


def gain_for(level: Loudness, target_lufs: float = TARGET_LUFS,
             peak_ceiling_dbtp: float = PEAK_CEILING_DBTP,
             max_gain_db: float = MAX_GAIN_DB) -> float:
    """The correction in decibels to bring `level` to the target.

    Never enough to push the true peak past the ceiling. Reaching a loudness
    target by clipping is not reaching it: the peaks are what the listener
    hears as distortion, and an audiobook shop measures them too.
    """
    if level.silent:
        raise Unmeasurable(
            f"{level.path or 'the audition'} has no speech in it to measure")
    wanted = target_lufs - level.integrated_lufs
    headroom = peak_ceiling_dbtp - level.true_peak_dbtp
    allowed = min(wanted, headroom) if wanted > 0 else wanted
    return round(max(-max_gain_db, min(max_gain_db, allowed)), 1)


def describe(level: Loudness, gain_db: float,
             target_lufs: float = TARGET_LUFS,
             peak_ceiling_dbtp: float = PEAK_CEILING_DBTP) -> str:
    """The measurement as a person needs to read it.

    Says where the voice actually lands rather than where it was aimed. The two
    differ whenever the true-peak ceiling binds, and an earlier version of this
    reported the target either way, which is the one thing a level report must
    not do.
    """
    reached = level.integrated_lufs + gain_db
    lines = [
        f"  {level.integrated_lufs:.1f} LUFS, true peak {level.true_peak_dbtp:.1f} dBTP"
        f", {level.seconds:.1f} s",
    ]
    if abs(gain_db) < 0.1:
        lines.append(f"  already at the {target_lufs:.0f} LUFS target; no correction")
    else:
        direction = "up" if gain_db > 0 else "down"
        lines.append(f"  {abs(gain_db):.1f} dB {direction}, to {reached:.1f} LUFS")

    short = target_lufs - reached
    if abs(short) > 0.5:
        lines.append(
            f"  held {abs(short):.1f} dB {'below' if short > 0 else 'above'} the "
            f"{target_lufs:.0f} LUFS target to keep the true peak under "
            f"{peak_ceiling_dbtp:.0f} dBTP. This voice is unusually dynamic; its "
            f"quiet passages will sit lower than the rest of the cast")
    if level.seconds and level.seconds < MIN_MEASURABLE_SEC:
        lines.append(f"  only {level.seconds:.1f} s of audio; the measurement is rough")
    return "\n".join(lines)


def settings(root: Path) -> dict[str, float]:
    """Targets from `config/pipeline.toml`, which is where tunables live."""
    from bookbinder.assemble import load_config

    cfg = load_config(root, "loudness")
    return {
        "target_lufs": float(cfg.get("target_lufs", TARGET_LUFS)),
        "peak_ceiling_dbtp": float(cfg.get("peak_ceiling_dbtp", PEAK_CEILING_DBTP)),
        "max_gain_db": float(cfg.get("max_gain_db", MAX_GAIN_DB)),
    }


def audition_path(root: Path, voice: str) -> Path:
    return root / "data" / "voices" / voice / "audition.wav"


def level_voice(root: Path, voice: str, gain_db: float | None = None) -> dict:
    """Measure a voice's audition and record the correction in its profile.

    Writing to the profile is what makes this take effect: `voice_revision`
    hashes that file, so the voice's identity changes, anything rendered at the
    old level is correctly seen as stale, and the narrator and the assembler
    agree about it without being told separately.
    """
    from bookbinder.manifest import publish_text

    profile_path = root / "data" / "voices" / f"{voice}.json"
    if not profile_path.is_file():
        raise Unmeasurable(f"no voice profile at {profile_path}; clone '{voice}' first")

    chosen = settings(root)
    level = measure(audition_path(root, voice))
    correction = gain_for(level, **chosen) if gain_db is None else round(gain_db, 1)

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    previous = float(profile.get("gain_db") or 0.0)
    profile["gain_db"] = correction
    profile["measured"] = {
        "integrated_lufs": round(level.integrated_lufs, 2),
        "true_peak_dbtp": round(level.true_peak_dbtp, 2),
        "seconds": round(level.seconds, 2),
        "target_lufs": chosen["target_lufs"],
    }
    publish_text(profile_path, json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
    return {"voice": voice, "gain_db": correction, "previous_gain_db": previous,
            "level": level, "settings": chosen, "changed": abs(correction - previous) >= 0.1}


def main() -> None:
    """`just level <voice>`: measure the audition and record the correction."""
    import sys

    from bookbinder.paths import project_root

    args = [a for a in sys.argv[1:] if a]
    if not args:
        print("usage: loudness <voice> [--gain=<dB>]", file=sys.stderr)
        raise SystemExit(2)

    override = next((a.split("=", 1)[1] for a in args if a.startswith("--gain=")), "")
    voices = [a for a in args if not a.startswith("-")]
    try:
        result = level_voice(project_root(), voices[0],
                             float(override) if override else None)
    except Unmeasurable as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

    print(f"{result['voice']}")
    print(describe(result["level"], result["gain_db"],
                   result["settings"]["target_lufs"]))
    if result["changed"] and result["previous_gain_db"]:
        print(f"  was {result['previous_gain_db']:+.1f} dB; audio rendered at the "
              f"old level is now out of date")
    elif result["changed"]:
        print("  fragments already rendered for this voice are now out of date")


if __name__ == "__main__":
    main()
