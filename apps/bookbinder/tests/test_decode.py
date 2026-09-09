"""Decoding is the one stage whose mistakes cannot be undone later.

A letter replaced with U+FFFD at import is gone: no synthesis model, no
correction pass and no amount of ftfy brings it back. So the properties pinned
here are that the same Polish text in four encodings comes out identical, and
that anything genuinely ambiguous stops rather than guesses.
"""

from __future__ import annotations

import pytest

from bookbinder.decode import (
    Decoded,
    UndecodableSource,
    decode_bytes,
    decode_file,
    detect_bom,
    text_quality,
)

# Enough Polish to exercise every diacritic, and long enough that a wrong
# single-byte table is statistically obvious rather than a coin toss.
POLISH = (
    "Zażółć gęślą jaźń. Ocean falował pod stacją, a wiatr wiał nieprzerwanie "
    "od trzech dni. Zszedłem po drabince do kabiny i zamknąłem właz za sobą. "
    "Śnieg padał na łąki pod Łodzią, a mgła osiadła na rzece o świcie."
)

ENGLISH = (
    "The ocean heaved beneath the station and the wind had not stopped for "
    "three days. I climbed down the ladder and closed the hatch behind me."
)


class TestTheSameTextInEveryEncoding:
    """The headline acceptance criterion for this stage."""

    @pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "cp1250", "iso-8859-2"])
    def test_polish_survives_the_round_trip(self, encoding):
        out = decode_bytes(POLISH.encode(encoding))
        assert out.text == POLISH

    def test_utf_8_with_a_bom_matches_utf_8_without(self):
        assert decode_bytes(POLISH.encode("utf-8-sig")).text == \
            decode_bytes(POLISH.encode("utf-8")).text

    @pytest.mark.parametrize("encoding", ["utf-8", "cp1250", "iso-8859-2", "cp1252"])
    def test_english_survives_the_round_trip(self, encoding):
        assert decode_bytes(ENGLISH.encode(encoding)).text == ENGLISH

    def test_every_polish_diacritic_is_preserved(self):
        for letter in "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ":
            raw = f"Litera {letter} w zdaniu.".encode("cp1250")
            assert letter in decode_bytes(raw).text


class TestNothingIsEverReplaced:
    def test_no_replacement_character_is_introduced(self):
        # errors="replace" was the old behaviour and is what this rules out.
        raw = POLISH.encode("cp1250")
        assert "�" not in decode_bytes(raw).text

    def test_an_undecodable_file_raises_rather_than_losing_bytes(self):
        # A lone continuation byte is not valid UTF-8, and the surrounding
        # bytes are chosen so no single-byte table produces plausible prose.
        raw = bytes([0x81, 0x8D, 0x8F, 0x90, 0x9D]) * 40
        try:
            out = decode_bytes(raw)
        except UndecodableSource:
            return
        # If some table did accept it, it must at least be flagged.
        assert out.needs_review or out.score < 0


class TestEncodingChoice:
    def test_windows_1250_is_not_read_as_iso_8859_2(self):
        # The two differ for Polish, and charset-normalizer alone gets this
        # wrong, which is why quality scoring decides instead.
        out = decode_bytes(POLISH.encode("cp1250"))
        assert out.encoding == "cp1250"
        assert out.text == POLISH

    def test_iso_8859_2_is_not_read_as_windows_1250(self):
        out = decode_bytes(POLISH.encode("iso-8859-2"))
        assert out.encoding == "iso-8859-2"
        assert out.text == POLISH

    @pytest.mark.parametrize("true_encoding", ["cp1250", "iso-8859-2"])
    def test_the_two_polish_tables_are_told_apart_on_ordinary_prose(self, true_encoding):
        # Both decode any byte without error, so only the resulting text
        # distinguishes them. This failed on a real book while the unit case
        # above passed, because the mis-decode produced punctuation rather
        # than letters.
        prose = (
            "Ocean falował pod stacją, a wiatr wiał nieprzerwanie od trzech dni. "
            "Śnieg padał na łąki pod Łodzią, a mgła osiadła na rzece o świcie. "
            "Nikt nie odpowiadał na wezwania, więc usiadłem przy pulpicie."
        )
        out = decode_bytes(prose.encode(true_encoding))
        assert out.encoding == true_encoding
        assert out.text == prose
        assert not out.needs_review

    def test_utf_8_wins_without_consulting_a_detector(self):
        out = decode_bytes(POLISH.encode("utf-8"))
        assert (out.encoding, out.method) == ("utf-8", "utf-8")

    def test_a_bom_is_honoured(self):
        out = decode_bytes(POLISH.encode("utf-16"))
        assert out.method == "bom"

    def test_ascii_reports_its_equivalent_readings(self):
        # Nothing distinguishes these tables on ASCII, so claiming to have
        # identified one would be inventing certainty.
        out = decode_bytes(ENGLISH.encode("ascii"))
        assert out.method == "ascii"
        assert "cp1250" in out.equivalent and "iso-8859-2" in out.equivalent
        assert not out.needs_review


class TestOverrides:
    def test_an_explicit_encoding_is_obeyed(self):
        out = decode_bytes(POLISH.encode("iso-8859-2"), override="iso-8859-2")
        assert (out.encoding, out.method, out.text) == ("iso-8859-2", "override", POLISH)

    def test_an_override_that_cannot_read_the_file_fails_loudly(self):
        with pytest.raises(UndecodableSource, match="not valid"):
            decode_bytes(POLISH.encode("utf-8"), override="ascii")

    def test_an_override_disagreeing_with_a_bom_is_reported(self):
        # A UTF-16 mark and a single-byte override cannot both be right, and
        # cp1250 will happily read those bytes as nonsense without complaining.
        out = decode_bytes(POLISH.encode("utf-16"), override="cp1250")
        assert any("byte-order mark" in w for w in out.warnings)

    def test_a_bom_of_the_same_family_is_not_a_disagreement(self):
        # utf-8-sig read as utf-8 is fine: the mark is stripped either way.
        out = decode_bytes(POLISH.encode("utf-8-sig"), override="utf-8")
        assert out.warnings == []
        assert out.text == POLISH

    def test_an_override_still_yields_composed_unicode(self):
        decomposed = "Zażółć"       # ć as c + combining acute
        out = decode_bytes(decomposed.encode("utf-8"), override="utf-8")
        assert out.text == "Zażółć"


class TestProvenance:
    def test_the_source_hash_is_recorded(self):
        raw = POLISH.encode("utf-8")
        import hashlib
        assert decode_bytes(raw).sha256 == hashlib.sha256(raw).hexdigest()

    def test_the_hash_follows_the_bytes_not_the_text(self):
        # The same text in two encodings is two different sources.
        assert decode_bytes(POLISH.encode("utf-8")).sha256 != \
            decode_bytes(POLISH.encode("cp1250")).sha256

    def test_decode_file_reads_from_disk(self, tmp_path):
        path = tmp_path / "book.txt"
        path.write_bytes(POLISH.encode("cp1250"))
        assert decode_file(path).text == POLISH


class TestTextQuality:
    def test_correct_polish_beats_a_mojibake_reading(self):
        raw = POLISH.encode("cp1250")
        assert text_quality(raw.decode("cp1250")) > text_quality(raw.decode("cp1252"))

    def test_c1_controls_are_decisive(self):
        # Reading cp1250 bytes as iso-8859-2 leaves \x9c and \x9f behind.
        raw = POLISH.encode("cp1250")
        assert text_quality(raw.decode("cp1250")) > text_quality(raw.decode("iso-8859-2"))

    def test_replacement_characters_are_punished(self):
        assert text_quality("Ocean falowa� pod stacj�") < text_quality("Ocean falował")

    def test_symbols_where_letters_belong_are_punished(self):
        # The gap that let a real ISO-8859-2 book be read as Windows-1250:
        # the mis-decode turns `ą` into `±` and `ś` into `¶`, neither of which
        # is alphabetic, so a letters-only test scored both readings perfect.
        assert not ("±".isalpha() or "¶".isalpha())
        assert text_quality("stacją i świt") > text_quality("stacj± i ¶wit")

    def test_empty_text_scores_nothing(self):
        assert text_quality("") == 0.0


class TestBomDetection:
    @pytest.mark.parametrize("encoding,expected", [
        ("utf-8-sig", "utf-8-sig"),
        ("utf-16", "utf-16"),
        ("utf-32", "utf-32"),
    ])
    def test_recognises_each_mark(self, encoding, expected):
        assert detect_bom("x".encode(encoding)) == expected

    def test_plain_utf_8_has_no_mark(self):
        assert detect_bom(POLISH.encode("utf-8")) is None
