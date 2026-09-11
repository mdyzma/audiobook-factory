"""A cast that changes volume mid-sentence is the defect you cannot ignore.

Nothing here is about taste. It is about two voices in one book landing on one
level, and about never reaching that level by clipping, which trades a quiet
voice for a distorted one.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from bookbinder.loudness import (
    MAX_GAIN_DB,
    PEAK_CEILING_DBTP,
    TARGET_LUFS,
    Loudness,
    Unmeasurable,
    describe,
    gain_for,
    level_voice,
    measure,
)


def tone(path, seconds=4.0, volume=0.5, rate=24000):
    """A real wav, because the measurement is ffmpeg's and not ours to fake."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=220:sample_rate={rate}:duration={seconds}",
         "-af", f"volume={volume}", "-c:a", "pcm_s16le", str(path)],
        check=True)
    return path


def silence(path, seconds=4.0, rate=24000):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"anullsrc=r={rate}:cl=mono", "-t", str(seconds),
         "-c:a", "pcm_s16le", str(path)],
        check=True)
    return path


class TestMeasuring:
    def test_a_real_file_gives_real_numbers(self, tmp_path):
        level = measure(tone(tmp_path / "a.wav"))
        assert -70 < level.integrated_lufs < 0
        assert level.true_peak_dbtp < 6
        assert level.seconds == pytest.approx(4.0, abs=0.3)

    def test_a_quieter_file_measures_quieter(self, tmp_path):
        loud = measure(tone(tmp_path / "loud.wav", volume=0.5))
        quiet = measure(tone(tmp_path / "quiet.wav", volume=0.05))
        assert quiet.integrated_lufs < loud.integrated_lufs - 10

    def test_silence_is_recognised_as_silence(self, tmp_path):
        assert measure(silence(tmp_path / "s.wav")).silent

    def test_a_missing_file_says_so(self, tmp_path):
        with pytest.raises(Unmeasurable, match="no audio"):
            measure(tmp_path / "nothing.wav")

    def test_something_that_is_not_audio_says_so(self, tmp_path):
        path = tmp_path / "notaudio.wav"
        path.write_text("this is not a wav", encoding="utf-8")
        with pytest.raises(Unmeasurable):
            measure(path)


class TestChoosingTheGain:
    def _level(self, lufs, peak=-10.0):
        return Loudness(integrated_lufs=lufs, true_peak_dbtp=peak, seconds=10.0)

    def test_a_quiet_voice_is_brought_up(self, tmp_path):
        assert gain_for(self._level(-30.0)) == pytest.approx(7.0, abs=0.11)

    def test_a_loud_voice_is_brought_down(self):
        assert gain_for(self._level(-14.0)) == pytest.approx(-6.0, abs=0.11)

    def test_a_voice_already_on_target_is_left_alone(self):
        assert gain_for(self._level(TARGET_LUFS)) == pytest.approx(0.0, abs=0.11)

    def test_two_voices_end_up_at_the_same_level(self):
        """The whole point, stated directly."""
        narrator, dialogue = self._level(-16.0), self._level(-24.0)
        assert (narrator.integrated_lufs + gain_for(narrator)) == pytest.approx(
            dialogue.integrated_lufs + gain_for(dialogue), abs=0.11)

    def test_the_true_peak_ceiling_wins_over_the_target(self):
        # Reaching a loudness target by clipping has not reached it: the peaks
        # are what a listener hears as distortion. This voice wants 10 dB up
        # and gets the 2 dB its headroom allows.
        dynamic = self._level(-30.0, peak=-5.0)
        assert gain_for(dynamic) == pytest.approx(2.0, abs=0.11)
        assert dynamic.true_peak_dbtp + gain_for(dynamic) <= PEAK_CEILING_DBTP

    def test_a_peak_already_over_the_ceiling_is_brought_down(self):
        # Quiet on average and peaking above the ceiling: the ceiling is a
        # constraint, not a preference, so it comes down even though that
        # takes it further from the loudness target.
        over = self._level(-30.0, peak=-1.0)
        assert gain_for(over) == pytest.approx(-2.0, abs=0.11)
        assert over.true_peak_dbtp + gain_for(over) <= PEAK_CEILING_DBTP

    def test_turning_down_is_never_blocked_by_the_ceiling(self):
        # Lowering a level cannot raise a peak, so headroom is irrelevant.
        assert gain_for(self._level(-10.0, peak=-0.5)) == pytest.approx(-10.0, abs=0.11)

    def test_a_hopeless_voice_is_not_amplified_without_limit(self):
        # Beyond this a voice is wrong rather than quiet, and lifting it would
        # amplify the room it was recorded in along with the speech. Headroom
        # is deliberately generous here so the cap is what bites.
        assert gain_for(self._level(-60.0, peak=-40.0)) == MAX_GAIN_DB

    def test_silence_is_refused_rather_than_amplified(self):
        with pytest.raises(Unmeasurable, match="no speech"):
            gain_for(Loudness(integrated_lufs=-91.0, true_peak_dbtp=-91.0, seconds=5.0))


class TestRecordingItOnTheVoice:
    def _voice(self, tmp_path, volume=0.05):
        (tmp_path / "data" / "voices").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "voices" / "michal.json").write_text(json.dumps({
            "name": "michal", "language": "pl", "reference_wavs": [],
        }), encoding="utf-8")
        tone(tmp_path / "data" / "voices" / "michal" / "audition.wav", volume=volume)
        return tmp_path

    def test_the_correction_lands_in_the_profile(self, tmp_path):
        root = self._voice(tmp_path)
        result = level_voice(root, "michal")
        stored = json.loads(
            (root / "data/voices/michal.json").read_text(encoding="utf-8"))
        assert stored["gain_db"] == result["gain_db"] != 0

    def test_what_was_measured_is_kept_beside_it(self, tmp_path):
        # So a person can see why the number is what it is, a year later.
        root = self._voice(tmp_path)
        level_voice(root, "michal")
        stored = json.loads(
            (root / "data/voices/michal.json").read_text(encoding="utf-8"))
        assert set(stored["measured"]) == {
            "integrated_lufs", "true_peak_dbtp", "seconds", "target_lufs"}

    def test_the_rest_of_the_profile_survives(self, tmp_path):
        root = self._voice(tmp_path)
        level_voice(root, "michal")
        stored = json.loads(
            (root / "data/voices/michal.json").read_text(encoding="utf-8"))
        assert stored["name"] == "michal" and stored["language"] == "pl"

    def test_levelling_changes_the_voice_revision(self, tmp_path):
        """This is what keeps the narrator and the assembler in step.

        The profile is hashed into every fragment fingerprint, so recording a
        gain makes audio rendered at the old level correctly stale, and neither
        side has to be told about loudness separately.
        """
        from bookbinder.fingerprint import voice_revision

        root = self._voice(tmp_path)
        before = voice_revision(root, "michal")
        level_voice(root, "michal")
        assert voice_revision(root, "michal") != before

    def test_an_explicit_gain_overrides_the_measurement(self, tmp_path):
        root = self._voice(tmp_path)
        assert level_voice(root, "michal", gain_db=-2.5)["gain_db"] == -2.5

    def test_a_voice_that_was_never_cloned_says_so(self, tmp_path):
        with pytest.raises(Unmeasurable, match="clone"):
            level_voice(tmp_path, "nobody")

    def test_levelling_twice_reports_no_change(self, tmp_path):
        root = self._voice(tmp_path)
        level_voice(root, "michal")
        assert not level_voice(root, "michal")["changed"]


class TestSayingWhatItDid:
    def test_it_names_both_numbers(self):
        level = Loudness(integrated_lufs=-26.0, true_peak_dbtp=-9.0, seconds=6.0)
        text = describe(level, gain_for(level))
        assert "-26.0 LUFS" in text and "-9.0 dBTP" in text

    def test_it_says_when_nothing_needed_doing(self):
        level = Loudness(integrated_lufs=TARGET_LUFS, true_peak_dbtp=-9.0, seconds=6.0)
        assert "no correction" in describe(level, gain_for(level))

    def test_it_says_where_the_voice_actually_lands(self):
        # Not where it was aimed. An earlier version reported the target
        # whether or not the ceiling let it get there.
        level = Loudness(integrated_lufs=-30.0, true_peak_dbtp=-5.0, seconds=6.0)
        text = describe(level, gain_for(level))
        assert "to -28.0 LUFS" in text
        assert "held 8.0 dB below" in text

    def test_it_warns_that_such_a_voice_will_not_match_the_cast(self):
        level = Loudness(integrated_lufs=-30.0, true_peak_dbtp=-5.0, seconds=6.0)
        assert "lower than the rest of the cast" in describe(level, gain_for(level))

    def test_it_says_when_the_clip_was_too_short_to_trust(self):
        level = Loudness(integrated_lufs=-22.0, true_peak_dbtp=-9.0, seconds=0.8)
        assert "rough" in describe(level, gain_for(level))
