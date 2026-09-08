"""Stage 4's resume rule, and the one thing that can poison it.

Resume is `skip any fragment that already has a wav`. That rule is what makes
a twenty-hour book survivable, and it is also why a dry run must never be left
in place: dry-run silence occupies exactly those paths, with plausible names
and durations, so a render started on top of one adopts every fragment and
finishes in under a second having produced nothing.

These tests need no model weights.
"""

from __future__ import annotations

import json

from narrator.synth import DRY_RUN_MARKER, discard_dry_run


def make_render(tmp_path, count=3, marked=False):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    for i in range(count):
        (audio_dir / f"ch001_{i:04d}.wav").write_bytes(b"RIFF....WAVE")
    (audio_dir / "rendered.jsonl").write_text("{}\n", encoding="utf-8")
    if marked:
        (audio_dir / DRY_RUN_MARKER).write_text(
            json.dumps({"schema_version": 1, "slug": "book", "chunks": count}),
            encoding="utf-8",
        )
    return audio_dir


class TestDiscardDryRun:
    def test_marked_silence_is_removed_before_a_render(self, tmp_path):
        audio_dir = make_render(tmp_path, count=3, marked=True)
        assert discard_dry_run(audio_dir) == 3
        assert not list(audio_dir.glob("*.wav"))
        assert not (audio_dir / "rendered.jsonl").exists()
        assert not (audio_dir / DRY_RUN_MARKER).exists()

    def test_an_unmarked_render_is_never_touched(self, tmp_path):
        """Hours of finished narration must survive this unconditionally."""
        audio_dir = make_render(tmp_path, count=3, marked=False)
        before = sorted(p.name for p in audio_dir.glob("*.wav"))

        assert discard_dry_run(audio_dir) == 0
        assert sorted(p.name for p in audio_dir.glob("*.wav")) == before
        assert (audio_dir / "rendered.jsonl").exists()

    def test_calling_it_twice_is_harmless(self, tmp_path):
        audio_dir = make_render(tmp_path, count=2, marked=True)
        assert discard_dry_run(audio_dir) == 2
        assert discard_dry_run(audio_dir) == 0

    def test_a_marker_with_no_audio_left_still_clears(self, tmp_path):
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        (audio_dir / DRY_RUN_MARKER).write_text("{}", encoding="utf-8")

        assert discard_dry_run(audio_dir) == 0
        assert not (audio_dir / DRY_RUN_MARKER).exists()
