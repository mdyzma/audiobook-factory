"""Chunking is where a book can be silently corrupted.

XTTS truncates past its per-language character limit without warning, so an
oversize chunk loses text with no error anywhere. These tests pin the two
properties that matter: nothing exceeds the limit, and no character is lost.
Words stay whole except where a single word is longer than the limit and so
has no break point at all.
"""

from __future__ import annotations

import pytest

from bookbinder.chunk import hard_split, pack, split_sentences
from bookbinder.manifest import char_limit

PL = char_limit("pl")  # 224

# A project without a model registry is not configured, so chunking refuses
# rather than guessing. Test projects therefore carry a minimal one.
REGISTRY = """
[defaults]
pl = "xtts-v2"
en = "xtts-v2"

[models.xtts-v2]
engine = "xtts"
environment = "narrator"
checkpoint = "tts_models/multilingual/multi-dataset/xtts_v2"
narration_languages = ["pl", "en"]
native_sample_rate = 24000
controls = ["temperature", "speed"]
validation = "baseline"

[models.xtts-v2.char_limits]
pl = 224
en = 250
"""


def words(chunks: list[str]) -> list[str]:
    return " ".join(chunks).split()


class TestPack:
    def test_short_paragraph_stays_one_chunk(self):
        assert pack(["Ocean falował pod stacją."], PL, 40) == ["Ocean falował pod stacją."]

    def test_never_exceeds_limit(self):
        long = ["Bardzo długie zdanie o oceanie i stacji badawczej. " * 20]
        for chunk in pack(long, PL, 40):
            assert len(chunk) <= PL

    def test_preserves_every_word_in_order(self):
        sentences = [
            "Zszedłem po drabince do kabiny.",
            "Ocean falował pod stacją, a wiatr wiał nieprzerwanie od trzech dni.",
            "Krótko.",
        ]
        assert words(pack(sentences, PL, 40)) == words(sentences)

    def test_trailing_runt_merges_backwards(self):
        out = pack(["Zdanie wystarczająco długie by przekroczyć próg scalania.", "Tak."], PL, 40)
        assert len(out) == 1

    def test_leading_runt_merges_forwards(self):
        # A short opener has no predecessor to fold into; it must fold forward.
        out = pack(["Tak.", "Zdanie wystarczająco długie by przekroczyć próg scalania."], PL, 40)
        assert len(out) == 1
        assert out[0].startswith("Tak.")

    def test_paragraph_of_only_runts_survives(self):
        # Merging must not drop text just because nothing reaches min_chars.
        sentences = ["Tak.", "Nie.", "Może."]
        assert words(pack(sentences, PL, 40)) == words(sentences)

    def test_single_short_sentence_is_kept(self):
        assert pack(["Tak."], PL, 40) == ["Tak."]

    def test_empty_input(self):
        assert pack([], PL, 40) == []

    def test_overlong_token_survives_packing(self):
        # What the pipeline actually calls. A sentence carrying one unbreakable
        # token must keep every character and still respect the limit.
        sentences = ["Przed nim krótkie zdanie.", "b" * 600, "Po nim kolejne zdanie."]
        out = pack(sentences, PL, 40)
        joined = " ".join(out).replace(" ", "")
        assert joined == "".join(sentences).replace(" ", "")
        for chunk in out:
            assert len(chunk) <= PL

    @pytest.mark.parametrize("language", ["pl", "en", "ja", "zh-cn", "ru"])
    def test_respects_each_language_limit(self, language):
        limit = char_limit(language)
        for chunk in pack(["Sentence about the ocean and the station. " * 30], limit, 40):
            assert len(chunk) <= limit


class TestHardSplit:
    def test_short_sentence_untouched(self):
        assert hard_split("Krótkie zdanie.", PL) == ["Krótkie zdanie."]

    def test_splits_on_clause_punctuation(self):
        sentence = "Pierwsza część zdania, " * 20
        for piece in hard_split(sentence, PL):
            assert len(piece) <= PL

    def test_pathological_single_token(self):
        # One 500-char word cannot be split on punctuation or spaces.
        for piece in hard_split("a" * 500, PL):
            assert len(piece) <= PL

    def test_pathological_single_token_keeps_every_character(self):
        # Regression: the overlong-word branch used to truncate to the limit,
        # dropping 276 of these 500 characters with no error anywhere.
        assert "".join(hard_split("a" * 500, PL)) == "a" * 500

    def test_overlong_token_mid_sentence_keeps_every_character(self):
        # A URL is the realistic case: one token with no internal break point.
        sentence = "Zobacz https://example.com/" + "x" * 300 + " i wróć."
        pieces = hard_split(sentence, PL)
        assert "".join(pieces).replace(" ", "") == sentence.replace(" ", "")
        for piece in pieces:
            assert len(piece) <= PL

    def test_overlong_token_flushes_the_pending_chunk_first(self):
        # The words before the giant token must not be swallowed by it.
        sentence = "Krótkie słowa najpierw " + "y" * 400
        pieces = hard_split(sentence, PL)
        assert pieces[0].startswith("Krótkie słowa najpierw")
        assert "".join(pieces).replace(" ", "") == sentence.replace(" ", "")

    def test_never_splits_mid_word(self):
        sentence = " ".join(["wyrazwielosylabowy"] * 40)
        pieces = hard_split(sentence, PL)
        assert " ".join(pieces).split() == sentence.split()


class TestSplitSentences:
    def test_polish_sentence_boundaries(self):
        out = split_sentences("Pierwsze zdanie. Drugie zdanie! Trzecie?", "pl")
        assert len(out) == 3

    def test_unsupported_language_falls_back_to_english(self):
        # Must not raise; pysbd covers 23 languages and this is not one of them.
        assert split_sentences("First sentence. Second one.", "xx-nonexistent")


class TestChunksCarryBothSpellings:
    """Chunking is where the spoken and printed forms are written down together.

    Preparation runs before packing, so the character budget measures what the
    model will read rather than what the page says.
    """

    def _chunk(self, tmp_path, monkeypatch, paragraph, dictionary=None):
        import json
        from typer.testing import CliRunner
        import bookbinder.chunk as chunk_mod

        (tmp_path / "justfile").write_text("", encoding="utf-8")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "pipeline.toml").write_text("", encoding="utf-8")
        (tmp_path / "config" / "models.toml").write_text(REGISTRY, encoding="utf-8")
        book_dir = tmp_path / "data" / "book" / "b"
        book_dir.mkdir(parents=True)
        book_dir.joinpath("chapters.json").write_text(json.dumps({
            "meta": {"title": "Sołaris", "author": "Lem", "language": "pl",
                     "slug": "b", "source_file": ""},
            "chapters": [{"index": 1, "title": "Przybysz", "source_ref": "c1",
                          "paragraphs": [paragraph]}],
        }, ensure_ascii=False), encoding="utf-8")
        if dictionary:
            book_dir.joinpath("pronunciation.yml").write_text(dictionary, encoding="utf-8")
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(tmp_path))

        result = CliRunner().invoke(chunk_mod.app, ["b", "--voice", "v"])
        assert result.exit_code == 0, result.output
        rows = [json.loads(l) for l in
                (book_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        return [r for r in rows if r["kind"] == "paragraph"]

    def test_the_spoken_form_is_what_the_model_gets(self, tmp_path, monkeypatch):
        rows = self._chunk(tmp_path, monkeypatch, "Spotkałem dr. Kelvina na stacji.")
        assert "doktor Kelvina" in rows[0]["text"]
        assert "dr." not in rows[0]["text"]

    def test_the_printed_form_is_kept_beside_it(self, tmp_path, monkeypatch):
        rows = self._chunk(tmp_path, monkeypatch, "Spotkałem dr. Kelvina na stacji.")
        assert "dr. Kelvina" in rows[0]["source_text"]

    def test_spans_are_valid_against_both_forms(self, tmp_path, monkeypatch):
        rows = self._chunk(
            tmp_path, monkeypatch,
            "Spotkałem dr. Kelvina, który badał m.in. tlen, np. jego udział.")
        row = rows[0]
        assert row["substitutions"]
        for sub in row["substitutions"]:
            assert row["source_text"][sub["start"]:sub["end"]] == sub["source"]
            assert row["text"][sub["spoken_start"]:sub["spoken_end"]] == sub["spoken"]

    def test_an_unchanged_paragraph_stores_no_second_copy(self, tmp_path, monkeypatch):
        rows = self._chunk(tmp_path, monkeypatch, "Ocean falował pod stacją badawczą.")
        assert rows[0]["source_text"] == ""
        assert rows[0]["substitutions"] == []

    def test_the_book_dictionary_is_applied(self, tmp_path, monkeypatch):
        rows = self._chunk(tmp_path, monkeypatch, "Kelvin wrócił na pokład.",
                           dictionary="Kelvin: Kelwin\n")
        assert "Kelwin" in rows[0]["text"]
        assert [s["kind"] for s in rows[0]["substitutions"]] == ["dictionary"]

    def test_expansion_cannot_push_a_fragment_over_the_limit(self, tmp_path, monkeypatch):
        # Packing before preparing would leave chunks that fit as written and
        # overflow once said.
        dense = " ".join(["Badał np. tlen i m.in. azot oraz itd. inne gazy."] * 12)
        rows = self._chunk(tmp_path, monkeypatch, dense)
        for row in rows:
            assert len(row["text"]) <= PL


class TestPerRoleControlsReachTheRenderer:
    """cast.yml has carried a speed per role since the beginning and nothing
    ever read it, so a dialogue voice set faster narrated at the same pace as
    everything else."""

    CAST = """roles:
  narrator:
    voice: v
    speed: 1.0
  kelvin:
    voice: v
    speed: 1.15
"""

    def _book(self, tmp_path, monkeypatch):
        import json
        from typer.testing import CliRunner
        import bookbinder.chunk as chunk_mod

        (tmp_path / "justfile").write_text("", encoding="utf-8")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "pipeline.toml").write_text("", encoding="utf-8")
        (tmp_path / "config" / "models.toml").write_text(REGISTRY, encoding="utf-8")
        (tmp_path / "config" / "cast.yml").write_text(self.CAST, encoding="utf-8")
        book_dir = tmp_path / "data" / "book" / "b"
        book_dir.mkdir(parents=True)
        book_dir.joinpath("chapters.json").write_text(json.dumps({
            "meta": {"title": "S", "author": "Lem", "language": "pl",
                     "slug": "b", "source_file": ""},
            "chapters": [{"index": 1, "title": "Jeden", "source_ref": "c1",
                          # Role assignment needs a speaker label; an em-dash
                          # paragraph alone stays with the narrator.
                          "paragraphs": ["Ocean falował pod stacją badawczą.",
                                         "Kelvin: — Wracam na Ziemię."]}],
        }, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setenv("AUDIOBOOK_FACTORY_ROOT", str(tmp_path))

        assert CliRunner().invoke(chunk_mod.app, ["b"]).exit_code == 0
        return json.loads((book_dir / "book.json").read_text(encoding="utf-8"))

    def test_a_role_that_differs_is_recorded(self, tmp_path, monkeypatch):
        book = self._book(tmp_path, monkeypatch)
        assert book["cast_settings"].get("kelvin") == {"speed": 1.15}

    def test_a_role_at_the_default_is_not(self, tmp_path, monkeypatch):
        # Recording every role at 1.0 would be noise in every manifest.
        book = self._book(tmp_path, monkeypatch)
        assert "narrator" not in book["cast_settings"]

    def test_only_controls_the_backend_implements_are_recorded(
            self, tmp_path, monkeypatch):
        book = self._book(tmp_path, monkeypatch)
        for controls in book["cast_settings"].values():
            assert set(controls) <= {"temperature", "speed"}
