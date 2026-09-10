"""Running out of disk is not a hard failure to survive, it is a slow one.

The fragments already written are fine. The one being written is truncated, the
manifest can end mid-line, and the eight hours it took to get there are spent.
The point of this stage is to spend a tenth of a second instead.
"""

from __future__ import annotations

import json

import pytest

from bookbinder.preflight import (
    HEADROOM,
    RESERVE_BYTES,
    Estimate,
    NotEnoughSpace,
    assembly,
    chunk_seconds,
    free_bytes,
    human,
    parse_bitrate,
    require,
    synthesis,
)


def book(root, slug="solaris", seconds=(60.0, 60.0, 60.0)):
    book_dir = root / "data" / "book" / slug
    book_dir.mkdir(parents=True, exist_ok=True)
    (root / "data" / "audio" / slug).mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"id": f"ch001_{i:04d}", "est_seconds": s})
             for i, s in enumerate(seconds)]
    (book_dir / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return book_dir


class TestWhatIsStillToWrite:
    def test_a_fresh_book_counts_every_fragment(self, tmp_path):
        book(tmp_path)
        assert chunk_seconds(tmp_path, "solaris") == (180.0, 180.0)

    def test_a_resumed_render_asks_only_for_the_rest(self, tmp_path):
        # The whole reason resume exists: asking for the full book again would
        # refuse a job that fits comfortably.
        book(tmp_path)
        (tmp_path / "data" / "audio" / "solaris" / "ch001_0000.wav").write_bytes(b"RIFF")
        total, remaining = chunk_seconds(tmp_path, "solaris")
        assert (total, remaining) == (180.0, 120.0)

    def test_a_book_with_no_chapter_split_yet_asks_for_nothing(self, tmp_path):
        assert chunk_seconds(tmp_path, "never-chunked") == (0.0, 0.0)

    def test_a_damaged_line_does_not_stop_the_estimate(self, tmp_path):
        # A preflight that raises on a stray line is worse than one that is a
        # fragment short.
        book_dir = book(tmp_path)
        with (book_dir / "chunks.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        assert chunk_seconds(tmp_path, "solaris")[0] == 180.0


class TestSizing:
    def test_an_hour_of_speech_is_sized_from_the_sample_rate(self, tmp_path):
        book(tmp_path, seconds=(3600.0,))
        estimate = synthesis(tmp_path, "solaris")
        assert estimate.needed == int(3600 * 24000 * 2 * HEADROOM)

    def test_the_estimate_carries_headroom(self, tmp_path):
        book(tmp_path, seconds=(3600.0,))
        assert synthesis(tmp_path, "solaris").needed > 3600 * 24000 * 2

    def test_assembly_is_sized_from_the_bitrate_not_the_fragments(self, tmp_path):
        # The output is encoded, so it is a fraction of the fragment audio.
        book(tmp_path, seconds=(3600.0,))
        assert assembly(tmp_path, "solaris", "m4b").needed < \
            synthesis(tmp_path, "solaris").needed

    def test_a_wav_export_is_sized_like_the_fragments(self, tmp_path):
        book(tmp_path, seconds=(3600.0,))
        assert assembly(tmp_path, "solaris", "wav").needed == \
            int(3600 * 24000 * 2 * HEADROOM)

    def test_assembly_counts_the_whole_book_not_the_remainder(self, tmp_path):
        # Half-rendered is still assembled whole, so resume does not apply here.
        book(tmp_path)
        (tmp_path / "data" / "audio" / "solaris" / "ch001_0000.wav").write_bytes(b"RIFF")
        assert assembly(tmp_path, "solaris", "wav").needed == \
            int(180 * 24000 * 2 * HEADROOM)

    @pytest.mark.parametrize("text,expected", [
        ("64k", 64_000), ("128K", 128_000), ("1m", 1_000_000),
        ("192000", 192_000), ("", 64_000), ("nonsense", 64_000),
    ])
    def test_bitrates_are_read_the_way_ffmpeg_writes_them(self, text, expected):
        assert parse_bitrate(text) == expected


class TestTheAnswer:
    def _estimate(self, needed, free, tmp_path):
        return Estimate(what="narrating 'solaris'", needed=needed, free=free,
                        where=tmp_path)

    def test_room_to_spare_is_enough(self, tmp_path):
        assert self._estimate(1000, RESERVE_BYTES + 5000, tmp_path).enough

    def test_a_fit_that_leaves_nothing_is_not_enough(self, tmp_path):
        # Exactly enough is not enough: a machine with no free disk cannot
        # write the log that would say what went wrong.
        assert not self._estimate(1000, 1000, tmp_path).enough

    def test_the_reserve_is_what_makes_the_difference(self, tmp_path):
        assert not self._estimate(1000, RESERVE_BYTES + 999, tmp_path).enough
        assert self._estimate(1000, RESERVE_BYTES + 1000, tmp_path).enough

    def test_refusing_says_what_to_do_about_it(self, tmp_path):
        estimate = self._estimate(10 * 1024 ** 3, 1024, tmp_path)
        with pytest.raises(NotEnoughSpace, match="Free some space"):
            require(estimate)

    def test_refusing_names_both_numbers(self, tmp_path):
        message = self._estimate(10 * 1024 ** 3, 1024, tmp_path).message
        assert "10.0 GB" in message and "1.0 KB" in message

    def test_enough_room_passes_the_estimate_through(self, tmp_path):
        estimate = self._estimate(1, RESERVE_BYTES * 2, tmp_path)
        assert require(estimate) is estimate


class TestAskingTheFilesystem:
    def test_it_answers_for_a_directory_that_does_not_exist_yet(self, tmp_path):
        # A book that has never been rendered has no audio directory, and
        # asking about it directly raises rather than answers.
        assert free_bytes(tmp_path / "data" / "audio" / "never-made") > 0

    def test_a_real_book_gets_a_real_answer(self, tmp_path):
        book(tmp_path)
        assert synthesis(tmp_path, "solaris").free > 0


class TestReadableSizes:
    @pytest.mark.parametrize("size,expected", [
        (512, "512 B"), (2048, "2.0 KB"), (5 * 1024 ** 2, "5.0 MB"),
        (3 * 1024 ** 3, "3.0 GB"), (2048 * 1024 ** 3, "2048.0 GB"),
    ])
    def test_sizes_read_the_way_a_person_would_say_them(self, size, expected):
        assert human(size) == expected
