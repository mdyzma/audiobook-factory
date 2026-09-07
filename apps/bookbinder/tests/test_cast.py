"""Cast resolution maps roles to voices, falling back rather than failing."""

from __future__ import annotations

import pytest

from bookbinder.cast import Cast, RoleConfig


def write_cast(tmp_path, body: str):
    path = tmp_path / "cast.yml"
    path.write_text(body, encoding="utf-8")
    return path


class TestLoad:
    def test_loads_roles_and_voices(self, tmp_path):
        cast = Cast.load(write_cast(tmp_path, """
roles:
  narrator:
    voice: michal
  kelvin:
    voice: kelvin
    speed: 0.98
"""))
        assert cast.voice_for("narrator") == "michal"
        assert cast.resolve("kelvin").speed == 0.98

    def test_accepts_a_bare_mapping_without_the_roles_key(self, tmp_path):
        cast = Cast.load(write_cast(tmp_path, "narrator:\n  voice: michal\n"))
        assert cast.voice_for("narrator") == "michal"

    def test_requires_a_narrator(self, tmp_path):
        with pytest.raises(ValueError, match="narrator"):
            Cast.load(write_cast(tmp_path, "roles:\n  kelvin:\n    voice: k\n"))

    def test_role_without_voice_is_an_error(self, tmp_path):
        with pytest.raises(ValueError, match="missing 'voice'"):
            Cast.load(write_cast(tmp_path, "roles:\n  narrator:\n    speed: 1.0\n"))

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            Cast.load(tmp_path / "absent.yml")


class TestResolve:
    def _cast(self):
        return Cast({"narrator": RoleConfig("michal"), "kelvin": RoleConfig("kelvin")})

    def test_unknown_role_falls_back_to_narrator(self):
        # A book naming a character the cast does not cover should still
        # render, in the narrator's voice, rather than refuse to start.
        assert self._cast().voice_for("snaut") == "michal"

    def test_mapping_covers_every_role_used(self):
        assert self._cast().mapping({"narrator", "kelvin", "snaut"}) == {
            "kelvin": "kelvin", "narrator": "michal", "snaut": "michal",
        }

    def test_single_voice_cast(self):
        cast = Cast.single("michal")
        assert cast.voice_for("narrator") == "michal"
        assert cast.voice_for("kelvin") == "michal"
