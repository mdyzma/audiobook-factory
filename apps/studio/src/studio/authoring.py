"""Writes: role corrections, the cast, and incoming files.

Everything here changes the project on disk, so each function validates before
it writes and none of them accept a path from the caller.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from bookbinder import overrides as role_overrides
from bookbinder.cast import Cast
from bookbinder.manifest import NARRATOR_ROLE

from studio.data import UnsafeName, check_name

# Extensions accepted for upload. Anything else is refused rather than stored
# and left for a later stage to choke on.
VOICE_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".webm"}
BOOK_SUFFIXES = {".epub", ".pdf", ".txt", ".md"}

# Generous, but not unbounded: a long recording is tens of megabytes and a big
# ebook is a few. This is a local tool, so the limit exists to catch mistakes.
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

# A role name has to survive being a yaml key and a filename component.
ROLE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AuthoringError(ValueError):
    """Something the caller asked for that will not be done, with a reason."""


@dataclass
class Upload:
    path: Path
    bytes_written: int


def _slugify_filename(name: str) -> str:
    """Derive a safe stem from an uploaded filename.

    Uses bookbinder's slugify, which transliterates the letters NFKD cannot
    decompose. Rolling a separate one here reintroduced exactly the bug that
    turned "Sołaris" into "soaris" and rejected it outright.
    """
    from bookbinder.ingest import slugify

    stem = slugify(Path(name).stem)
    return stem.strip("-.") or "upload"


def store_upload(root: Path, kind: str, filename: str, stream) -> Upload:
    """Save an uploaded voice sample or ebook under data/raw/.

    The caller's filename decides only the *stem*, and even that is slugified.
    The directory and extension are chosen here.
    """
    if kind == "voice":
        target_dir, allowed = root / "data" / "raw" / "voices", VOICE_SUFFIXES
    elif kind == "book":
        target_dir, allowed = root / "data" / "raw" / "books", BOOK_SUFFIXES
    else:
        raise AuthoringError(f"unknown upload kind '{kind}'")

    suffix = Path(filename or "").suffix.lower()
    if suffix not in allowed:
        raise AuthoringError(
            f"{kind} files must be one of {', '.join(sorted(allowed))}, got '{suffix or 'none'}'"
        )

    stem = _slugify_filename(filename)
    try:
        check_name(stem)
    except UnsafeName:
        raise AuthoringError(f"cannot derive a safe name from '{filename}'")

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{stem}{suffix}"

    written = 0
    with target.open("wb") as out:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            written += len(block)
            if written > MAX_UPLOAD_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                raise AuthoringError(
                    f"upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
                )
            out.write(block)

    if written == 0:
        target.unlink(missing_ok=True)
        raise AuthoringError("upload was empty")
    return Upload(path=target, bytes_written=written)


def list_raw(root: Path, kind: str) -> list[dict]:
    directory = root / "data" / "raw" / ("voices" if kind == "voice" else "books")
    allowed = VOICE_SUFFIXES if kind == "voice" else BOOK_SUFFIXES
    if not directory.is_dir():
        return []
    return sorted(
        ({"name": p.name, "bytes": p.stat().st_size,
          "path": str(p.relative_to(root))}
         for p in directory.iterdir()
         if p.is_file() and p.suffix.lower() in allowed),
        key=lambda d: d["name"],
    )


# --- role corrections --------------------------------------------------------

def set_role(root: Path, slug: str, source_ref: str, role: str) -> dict[str, str]:
    """Correct, or clear, the role for one paragraph of a book."""
    check_name(slug)
    book_dir = root / "data" / "book" / slug
    if not book_dir.is_dir():
        raise AuthoringError(f"no book '{slug}'")
    if not source_ref or len(source_ref) > 300:
        raise AuthoringError("missing or absurd source_ref")

    role = (role or "").strip().lower()
    if role and not ROLE_NAME.match(role):
        raise AuthoringError(
            "a role is lowercase letters, digits, hyphens or underscores"
        )
    return role_overrides.set_role(book_dir, source_ref, role)


def get_roles(root: Path, slug: str) -> dict[str, str]:
    check_name(slug)
    return role_overrides.load(root / "data" / "book" / slug)


# --- cast --------------------------------------------------------------------

def cast_path(root: Path) -> Path:
    return root / "config" / "cast.yml"


def read_cast(root: Path) -> dict[str, dict]:
    path = cast_path(root)
    if not path.exists():
        return {}
    cast = Cast.load(path)
    return {name: {"voice": cfg.voice, "speed": cfg.speed}
            for name, cfg in cast.roles.items()}


def write_cast(root: Path, roles: dict[str, dict]) -> Path:
    """Rewrite config/cast.yml from a mapping of role to settings.

    Written by hand rather than with a yaml dumper so the explanatory comments
    survive: this file is edited by people at least as often as by this code.
    """
    import yaml  # noqa: F401  (proves the dependency exists before we claim yaml)

    if NARRATOR_ROLE not in roles:
        raise AuthoringError("the cast must define a 'narrator' role")

    known_voices = {p.stem for p in (root / "data" / "voices").glob("*.json")}

    lines = [
        "# Which voice reads which role.",
        "#",
        "# Roles with no entry fall back to narrator. Every voice named here",
        "# needs data/voices/<voice>.json, which `just voice` creates.",
        "#",
        "# Edited through the dashboard; hand edits are preserved on the next",
        "# read, but rewriting from the dashboard replaces this file.",
        "",
        "roles:",
    ]
    for name in sorted(roles):
        if not ROLE_NAME.match(name):
            raise AuthoringError(f"invalid role name '{name}'")
        cfg = roles[name] or {}
        voice = str(cfg.get("voice") or "").strip()
        if not voice:
            raise AuthoringError(f"role '{name}' needs a voice")
        try:
            check_name(voice)
        except UnsafeName:
            raise AuthoringError(f"invalid voice '{voice}'")
        try:
            speed = float(cfg.get("speed", 1.0))
        except (TypeError, ValueError):
            raise AuthoringError(f"role '{name}' has a non-numeric speed")
        if not 0.5 <= speed <= 2.0:
            raise AuthoringError(f"speed for '{name}' must be between 0.5 and 2.0")

        lines.append(f"  {name}:")
        lines.append(f"    voice: {voice}")
        lines.append(f"    speed: {speed}")
        if voice not in known_voices:
            lines.append(f"    # note: no data/voices/{voice}.json yet")
    lines.append("")

    path = cast_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yml.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)
    return path


def backup_cast(root: Path) -> Path | None:
    """Keep one previous version, so a bad edit is recoverable."""
    path = cast_path(root)
    if not path.exists():
        return None
    backup = path.with_suffix(".yml.bak")
    shutil.copy2(path, backup)
    return backup
