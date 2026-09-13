"""A miniature project tree, so tests never touch the real data directory."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

# ERROR_PRIVILEGE_NOT_HELD: Windows creates symlinks only with Developer Mode
# or elevation.
_NO_SYMLINK_PRIVILEGE = 1314


@pytest.fixture
def symlink():
    """Make `link` point at `target`, as far as this machine allows.

    A directory falls back to a junction, which needs no privilege and which
    path resolution follows the same way. A file has no such stand-in, so the
    test is skipped rather than reported as a containment failure it is not.
    """
    def make(link: Path, target: Path, *, directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            if sys.platform != "win32" or getattr(exc, "winerror", None) != _NO_SYMLINK_PRIVILEGE:
                raise
            if not directory:
                pytest.skip("file symlinks on Windows need Developer Mode or elevation")
            import _winapi
            _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]
    return make


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    shutil.copy2(Path(__file__).resolve().parents[3] / "config/models.toml", tmp_path / "config/models.toml")
    (tmp_path / "data" / "voices" / "michal").mkdir(parents=True)
    (tmp_path / "data" / "datasets" / "michal").mkdir(parents=True)
    (tmp_path / "data" / "book" / "solaris").mkdir(parents=True)
    (tmp_path / "data" / "audio" / "solaris").mkdir(parents=True)
    (tmp_path / "data" / "out").mkdir(parents=True)

    (tmp_path / "data" / "voices" / "michal.json").write_text(json.dumps({
        "name": "michal", "engine": "xtts_v2", "language": "pl", "mode": "instant",
        "sample_rate": 24000, "model_dir": None, "segment_count": 18,
        "total_minutes": 1.4,
        "reference_wavs": ["data/datasets/michal/wavs/seg_0000.wav"],
    }), encoding="utf-8")
    (tmp_path / "data" / "voices" / "michal" / "audition.wav").write_bytes(b"RIFFfake")

    (tmp_path / "data" / "book" / "solaris" / "book.json").write_text(json.dumps({
        "schema_version": 1, "slug": "solaris", "title": "Sołaris", "author": "Lem",
        "language": "pl", "source_file": "", "source_sha256": "", "voice": "",
        "cast": {"narrator": "michal", "dialogue": "michal"},
        "chapters": [{"index": 1, "title": "Przybysz", "first_chunk": 0, "chunk_count": 2}],
        "chunk_count": 2, "est_hours": 0.01,
    }), encoding="utf-8")

    # No schema_version here: Chunk forbids unknown fields, and only book.json
    # carries one.
    chunk = {
        "id": "ch001_0000", "chapter_index": 1,
        "chapter_title": "Przybysz", "order": 0, "text": "Ocean falował.",
        "kind": "paragraph", "language": "pl", "role": "narrator",
        "is_dialogue": False, "pause_after_ms": 350, "chars": 14, "est_seconds": 0.9,
        "source_ref": "", "audio_path": None, "duration_sec": None,
    }
    (tmp_path / "data" / "book" / "solaris" / "chunks.jsonl").write_text(
        json.dumps(chunk) + "\n", encoding="utf-8")

    # The same environment variable the containers use, so the tests exercise
    # real root discovery rather than a stand-in for it.
    monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(tmp_path))
    # No test wants the dashboard quietly starting renders behind it.
    monkeypatch.setenv("AF_NO_DRAIN", "1")
    return tmp_path
