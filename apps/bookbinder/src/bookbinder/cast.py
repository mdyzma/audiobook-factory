"""Load config/cast.yml and resolve roles to voices."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bookbinder.manifest import NARRATOR_ROLE


@dataclass(frozen=True)
class RoleConfig:
    voice: str
    speed: float = 1.0


class Cast:
    """role -> voice mapping, with a narrator fallback.

    Falling back rather than failing is deliberate: a book that names a
    character the cast does not cover should still render, in the narrator's
    voice, instead of refusing to start.
    """

    def __init__(self, roles: dict[str, RoleConfig]) -> None:
        if NARRATOR_ROLE not in roles:
            raise ValueError("cast.yml must define a 'narrator' role")
        self.roles = roles

    @classmethod
    def load(cls, path: Path) -> "Cast":
        import yaml

        if not path.exists():
            raise FileNotFoundError(f"no cast at {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        section = raw.get("roles", raw)
        if not isinstance(section, dict):
            raise ValueError(f"{path}: 'roles' must map role names to settings")

        roles: dict[str, RoleConfig] = {}
        for name, cfg in section.items():
            cfg = cfg or {}
            if not isinstance(cfg, dict):
                raise ValueError(f"{path}: role '{name}' must be a mapping")
            voice = cfg.get("voice")
            if not voice:
                raise ValueError(f"{path}: role '{name}' is missing 'voice'")
            roles[str(name)] = RoleConfig(voice=str(voice), speed=float(cfg.get("speed", 1.0)))
        return cls(roles)

    @classmethod
    def single(cls, voice: str) -> "Cast":
        """A one-voice cast, for when no cast.yml is in play."""
        return cls({NARRATOR_ROLE: RoleConfig(voice=voice)})

    @property
    def known_roles(self) -> set[str]:
        return set(self.roles)

    def resolve(self, role: str) -> RoleConfig:
        return self.roles.get(role) or self.roles[NARRATOR_ROLE]

    def voice_for(self, role: str) -> str:
        return self.resolve(role).voice

    def mapping(self, roles: set[str]) -> dict[str, str]:
        """role -> voice for the roles a book actually uses."""
        return {role: self.voice_for(role) for role in sorted(roles)}
