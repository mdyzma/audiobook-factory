"""The dataset formatter bridges the transcriber's output into Coqui's loader.

Fine-tuning itself needs CUDA and is not exercised here.
"""

from __future__ import annotations

import pytest

from narrator.train import make_formatter


@pytest.fixture
def dataset(tmp_path):
    wavs = tmp_path / "wavs"
    wavs.mkdir()
    for i in range(3):
        (wavs / f"seg_{i:04d}.wav").write_bytes(b"RIFF")
    (tmp_path / "metadata.csv").write_text(
        "seg_0000.wav|Pierwsze zdanie.\n"
        "seg_0001.wav|Drugie zdanie, dłuższe.\n"
        "seg_0002.wav|Trzecie.\n",
        encoding="utf-8")
    return tmp_path


class TestMakeFormatter:
    def test_parses_every_row(self, dataset):
        assert len(make_formatter("pl")(str(dataset), "metadata.csv")) == 3

    def test_emits_the_keys_the_loader_reads(self, dataset):
        item = make_formatter("pl")(str(dataset), "metadata.csv")[0]
        assert set(item) >= {"text", "audio_file", "language", "speaker_name",
                             "root_path", "audio_unique_name"}

    def test_language_is_propagated(self, dataset):
        assert make_formatter("en")(str(dataset), "metadata.csv")[0]["language"] == "en"

    def test_skips_rows_whose_audio_is_missing(self, dataset):
        (dataset / "metadata.csv").write_text(
            "seg_0000.wav|Istnieje.\nbrak.wav|Nie istnieje.\n", encoding="utf-8")
        items = make_formatter("pl")(str(dataset), "metadata.csv")
        assert [i["text"] for i in items] == ["Istnieje."]

    def test_skips_blank_and_malformed_rows(self, dataset):
        (dataset / "metadata.csv").write_text(
            "seg_0000.wav|Dobre.\n\n   \nbez-separatora\n", encoding="utf-8")
        assert len(make_formatter("pl")(str(dataset), "metadata.csv")) == 1

    def test_text_containing_pipe_is_kept_whole(self, dataset):
        # split on the first separator only; a pipe in the text must survive.
        (dataset / "metadata.csv").write_text(
            "seg_0000.wav|Tekst z | pionową kreską.\n", encoding="utf-8")
        item = make_formatter("pl")(str(dataset), "metadata.csv")[0]
        assert item["text"] == "Tekst z | pionową kreską."

    def test_audio_unique_name_is_unique_per_clip(self, dataset):
        items = make_formatter("pl")(str(dataset), "metadata.csv")
        assert len({i["audio_unique_name"] for i in items}) == len(items)
