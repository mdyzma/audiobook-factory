"""What the narrator says, versus what the page says.

Every change here is audible, and every one has to be traceable: quality
checking transcribes the audio and compares it to the spoken text, so a
substitution the manifest does not record shows up as a fault in the render.
"""

from __future__ import annotations

import pytest

from bookbinder.speech import (
    ABBREVIATIONS,
    Prepared,
    load_dictionary,
    prepare,
)


class TestPolishAbbreviations:
    @pytest.mark.parametrize("written,spoken", [
        ("dr.", "doktor"),
        ("prof.", "profesor"),
        ("np.", "na przykład"),
        ("itd.", "i tak dalej"),
        ("itp.", "i tym podobne"),
        ("tzw.", "tak zwany"),
        ("ok.", "około"),
        ("ul.", "ulica"),
    ])
    def test_common_forms_are_expanded(self, written, spoken):
        assert prepare(f"Spotkałem {written} tam", "pl").text == f"Spotkałem {spoken} tam"

    def test_the_longest_match_wins(self):
        # `m.in.` must not be read as an entry for `in`.
        assert prepare("Byli tam m.in. sąsiedzi", "pl").text == \
            "Byli tam między innymi sąsiedzi"

    def test_capitalisation_is_carried_over(self):
        assert prepare("Dr. Kelvin przyszedł", "pl").text == "Doktor Kelvin przyszedł"

    def test_a_word_ending_in_an_abbreviation_is_left_alone(self):
        # "widok." ends with "ok." but is not the abbreviation.
        assert prepare("Piękny widok.", "pl").text == "Piękny widok."

    def test_an_abbreviation_without_its_dot_is_left_alone(self):
        # "ok" without a dot is a different word, and "dr" may be a name.
        assert prepare("To jest ok", "pl").text == "To jest ok"


class TestEnglishAbbreviations:
    @pytest.mark.parametrize("written,spoken", [
        ("Dr.", "Doctor"), ("Mr.", "Mister"), ("Mrs.", "Missus"),
        ("St.", "Saint"), ("etc.", "et cetera"), ("vs.", "versus"),
    ])
    def test_common_forms_are_expanded(self, written, spoken):
        assert prepare(f"Met {written} there", "en").text == f"Met {spoken} there"

    def test_multi_dot_forms_are_expanded(self):
        assert prepare("Fruit, e.g. apples", "en").text == "Fruit, for example apples"
        assert prepare("The rest, i.e. everything", "en").text == \
            "The rest, that is everything"

    def test_possessives_and_contractions_are_untouched(self):
        text = "It's the station's hatch and they don't know."
        assert prepare(text, "en").text == text


class TestLanguageSeparation:
    def test_polish_rules_do_not_run_on_english(self):
        # "ok." is Polish for "about"; in English it is not expanded.
        assert prepare("It cost ok. five", "en").text == "It cost ok. five"

    def test_english_rules_do_not_run_on_polish(self):
        assert "et cetera" not in prepare("Koty, psy etc.", "pl").text

    def test_an_unknown_language_changes_nothing(self):
        text = "Der Ozean wogte unter der Station."
        assert prepare(text, "de").text == text


class TestSymbols:
    def test_percent_is_spoken(self):
        assert prepare("Wzrost o 15%", "pl").text == "Wzrost o 15 procent"
        assert prepare("Up by 15%", "en").text == "Up by 15 percent"

    def test_a_symbol_flush_against_a_number_gains_a_space(self):
        # Without one the expansion becomes a single unsayable word.
        assert "15procent" not in prepare("Wzrost o 15%", "pl").text

    def test_a_symbol_already_spaced_does_not_gain_another(self):
        assert prepare("Wzrost o 15 %", "pl").text == "Wzrost o 15 procent"

    def test_ampersand_is_spoken(self):
        assert prepare("Marks & Spencer", "en").text == "Marks and Spencer"

    def test_degrees_are_spoken(self):
        assert prepare("Było 21°C w kabinie", "pl").text == \
            "Było 21 stopni Celsjusza w kabinie"


class TestPronunciationDictionary:
    def test_a_book_entry_is_applied(self):
        out = prepare("Kelvin wrócił", "pl", {"Kelvin": "Kelwin"})
        assert out.text == "Kelwin wrócił"

    def test_the_longest_entry_wins(self):
        out = prepare("Kris Kelvin wrócił", "pl",
                      {"Kelvin": "Kelwin", "Kris Kelvin": "Krys Kelwin"})
        assert out.text == "Krys Kelwin wrócił"

    def test_an_entry_does_not_fire_inside_a_word(self):
        assert prepare("Kelvinometr", "pl", {"Kelvin": "Kelwin"}).text == "Kelvinometr"

    def test_a_dictionary_entry_beats_an_abbreviation(self):
        # Someone whose name really is written "dr." keeps it.
        out = prepare("Firma dr. Oetker", "pl", {"dr. Oetker": "doktor Etker"})
        assert out.text == "Firma doktor Etker"

    def test_loading_a_missing_file_is_not_an_error(self, tmp_path):
        assert load_dictionary(tmp_path / "absent.yml") == {}

    def test_loading_reads_written_to_spoken_pairs(self, tmp_path):
        path = tmp_path / "pronunciation.yml"
        path.write_text("Kelvin: Kelwin\nSnaut: Snałt\n", encoding="utf-8")
        assert load_dictionary(path) == {"Kelvin": "Kelwin", "Snaut": "Snałt"}

    def test_a_malformed_dictionary_says_so(self, tmp_path):
        path = tmp_path / "pronunciation.yml"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_dictionary(path)


class TestProvenance:
    def test_the_original_spelling_is_kept(self):
        out = prepare("Spotkałem dr. Kelvina", "pl")
        assert out.source_text == "Spotkałem dr. Kelvina"
        assert out.text == "Spotkałem doktor Kelvina"
        assert out.changed

    def test_unchanged_text_reports_no_substitutions(self):
        out = prepare("Ocean falował pod stacją.", "pl")
        assert out.substitutions == []
        assert not out.changed

    def test_each_substitution_records_its_span_in_the_source(self):
        text = "Spotkałem dr. Kelvina"
        out = prepare(text, "pl")
        sub = out.substitutions[0]
        assert text[sub.start:sub.end] == "dr."
        assert (sub.source, sub.spoken, sub.kind) == ("dr.", "doktor", "abbreviation")

    def test_spans_stay_valid_when_the_text_grows(self):
        # Positions refer to the source, not the rewritten string, so several
        # expansions in one paragraph do not drift.
        text = "np. dr. Kelvin i itd."
        out = prepare(text, "pl")
        for sub in out.substitutions:
            assert text[sub.start:sub.end] == sub.source

    def test_substitutions_are_in_reading_order(self):
        out = prepare("np. potem dr. potem itd.", "pl")
        starts = [s.start for s in out.substitutions]
        assert starts == sorted(starts)


class TestConservatism:
    """What is deliberately left alone, and why.

    Polish inflects numerals for case and gender, so a rule that turns "3" into
    one fixed word is wrong more often than the digits are. Numbers stay as
    written until a real model has been listened to; see slice B.
    """

    @pytest.mark.parametrize("text", [
        "Miał 3 koty i 15 książek.",
        "Kosztowało 1 234,56 zł w 1961 roku.",
        "Wróciłem 5 maja o 17:30.",
    ])
    def test_numbers_are_left_to_the_model(self, text):
        assert prepare(text, "pl").text == text

    def test_a_dictionary_can_still_settle_a_number(self, text="W 1961 r. wrócił"):
        out = prepare(text, "pl", {"1961": "tysiąc dziewięćset sześćdziesiątym pierwszym"})
        assert "tysiąc dziewięćset" in out.text


class TestTables:
    def test_every_language_has_both_tables_or_neither(self):
        from bookbinder.speech import SYMBOLS

        assert set(ABBREVIATIONS) == set(SYMBOLS)

    def test_no_expansion_is_empty(self):
        for table in ABBREVIATIONS.values():
            assert all(v.strip() for v in table.values())


class TestAnchorsBetweenRepresentations:
    """The spoken text has to stay navigable back to the spelling it came from.

    Lengths diverge as soon as anything is expanded, so both spans are kept and
    the mapping is exercised rather than assumed.
    """

    def test_both_spans_are_recorded(self):
        text = "Spotkałem dr. Kelvina"
        out = prepare(text, "pl")
        sub = out.substitutions[0]
        assert text[sub.start:sub.end] == "dr."
        assert out.text[sub.spoken_start:sub.spoken_end] == "doktor"

    def test_spoken_spans_stay_correct_across_several_expansions(self):
        text = "np. potem dr. Kelvin i itd. na końcu"
        out = prepare(text, "pl")
        assert len(out.substitutions) == 3
        for sub in out.substitutions:
            assert text[sub.start:sub.end] == sub.source
            assert out.text[sub.spoken_start:sub.spoken_end] == sub.spoken

    def test_an_offset_before_any_change_maps_to_itself(self):
        out = prepare("Ocean, a potem dr. Kelvin", "pl")
        assert out.source_offset(3) == 3

    def test_an_offset_after_a_change_is_shifted_back(self):
        text = "np. Kelvin"
        out = prepare(text, "pl")          # "na przykład Kelvin"
        spoken_k = out.text.index("Kelvin")
        assert text[out.source_offset(spoken_k):] == "Kelvin"

    def test_an_offset_inside_a_change_maps_to_what_it_replaced(self):
        out = prepare("np. Kelvin", "pl")
        sub = out.substitutions[0]
        assert out.source_offset(sub.spoken_start + 2) == sub.start

    def test_an_offset_past_the_end_is_clamped(self):
        out = prepare("np. Kelvin", "pl")
        assert out.source_offset(len(out.text) + 50) == len(out.source_text)


class TestSplittingKeepsProvenance:
    def test_each_piece_keeps_the_spelling_it_came_from(self):
        from bookbinder.speech import split_provenance

        whole = prepare("Spotkałem dr. Kelvina. Potem wrócił np. wieczorem.", "pl")
        pieces = ["Spotkałem doktor Kelvina.", "Potem wrócił na przykład wieczorem."]
        parts = split_provenance(whole, pieces)

        assert [p.text for p in parts] == pieces
        assert parts[0].source_text.strip() == "Spotkałem dr. Kelvina."
        assert parts[1].source_text.strip() == "Potem wrócił np. wieczorem."

    def test_substitutions_follow_their_piece(self):
        from bookbinder.speech import split_provenance

        whole = prepare("Spotkałem dr. Kelvina. Potem wrócił np. wieczorem.", "pl")
        parts = split_provenance(whole, ["Spotkałem doktor Kelvina.",
                                         "Potem wrócił na przykład wieczorem."])
        assert [s.source for s in parts[0].substitutions] == ["dr."]
        assert [s.source for s in parts[1].substitutions] == ["np."]

    def test_spans_are_rebased_onto_the_piece(self):
        from bookbinder.speech import split_provenance

        whole = prepare("Ocean falował. Potem dr. Kelvin wrócił.", "pl")
        parts = split_provenance(whole, ["Ocean falował.", "Potem doktor Kelvin wrócił."])
        second = parts[1]
        for sub in second.substitutions:
            assert second.source_text[sub.start:sub.end] == sub.source
            assert second.text[sub.spoken_start:sub.spoken_end] == sub.spoken

    def test_a_piece_with_no_changes_carries_none(self):
        from bookbinder.speech import split_provenance

        whole = prepare("Ocean falował. Potem dr. Kelvin wrócił.", "pl")
        parts = split_provenance(whole, ["Ocean falował.", "Potem doktor Kelvin wrócił."])
        assert parts[0].substitutions == []
