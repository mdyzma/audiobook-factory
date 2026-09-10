"""Which synthesis backend narrates a book, and what it can do.

Everything about a model that the rest of the pipeline needs to know lives in
`config/models.toml` rather than in code. Chunk sizes, the languages a voice
reference may be in, whether cloning needs a transcript, and which environment
can even load the thing are all properties of the model, and hard-coding
XTTS's answers to them is what made a second backend a rewrite rather than a
configuration entry.

Resolution is: an explicit choice, or the tested default for the book's
language. There is no third case. A model that cannot narrate the language it
was asked for is an error naming the alternatives, never a quiet substitution
part-way through a book.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Kept in step with the backends that actually exist. A registry entry naming
# anything else is a typo, and saying so at load time beats failing hours into
# a render.
KNOWN_ENGINES = frozenset({"xtts", "chatterbox", "qwen3-tts"})

CLONING_MODES = frozenset({"instant", "finetune", "none"})

# How much is known about an entry, weakest first.
VALIDATION_LEVELS = ("unvalidated", "baseline", "validated")


class RegistryError(ValueError):
    """A registry that will not be used, with a reason worth showing."""


class ModelUnavailable(RegistryError):
    """The requested model cannot narrate what it was asked to."""


@dataclass(frozen=True)
class ModelSpec:
    """One synthesis backend, as the pipeline sees it."""

    id: str
    engine: str
    environment: str
    checkpoint: str
    narration_languages: tuple[str, ...]
    reference_languages: tuple[str, ...]
    cloning: str = "instant"
    needs_reference_transcript: bool = False
    native_sample_rate: int = 24000
    controls: tuple[str, ...] = ()
    validation: str = "unvalidated"
    package: str = ""
    revision: str = ""
    default_char_limit: int = 250
    char_limits: dict[str, int] = field(default_factory=dict)

    def narrates(self, language: str) -> bool:
        return language in self.narration_languages

    def accepts_reference_in(self, language: str) -> bool:
        """Whether a voice recorded in `language` may be used as a reference.

        Cross-language cloning is a separate question from narration: an engine
        may read Polish well from an English reference, or badly, and only
        listening settles it. The registry records what the model documents.
        """
        return language in self.reference_languages

    def char_limit(self, language: str) -> int:
        """The most text this model will read in one fragment, in characters."""
        return self.char_limits.get(language, self.default_char_limit)

    @property
    def identity(self) -> str:
        """What has to match for rendered audio to be reusable.

        Two runs of the same model at different revisions are not the same
        model, and audio from one must not be resumed into the other.
        """
        return f"{self.id}@{self.revision}" if self.revision else self.id

    def supported_controls(self, settings: dict) -> dict:
        """The subset of `settings` this backend actually implements."""
        return {k: v for k, v in settings.items() if k in self.controls}

    def unsupported_controls(self, settings: dict) -> list[str]:
        """Settings this backend will ignore, so a caller can say so out loud."""
        return sorted(k for k in settings if k not in self.controls)


@dataclass(frozen=True)
class Registry:
    models: dict[str, ModelSpec]
    defaults: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "Registry":
        if not path.exists():
            raise RegistryError(f"no model registry at {path}")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw, source=str(path))

    @classmethod
    def from_dict(cls, raw: dict, source: str = "registry") -> "Registry":
        models: dict[str, ModelSpec] = {}
        for model_id, entry in (raw.get("models") or {}).items():
            models[model_id] = _spec(model_id, entry, source)

        defaults = {str(k): str(v) for k, v in (raw.get("defaults") or {}).items()}
        for language, model_id in defaults.items():
            if model_id not in models:
                raise RegistryError(
                    f"{source}: the default for '{language}' is '{model_id}', "
                    f"which is not defined. Known: {', '.join(sorted(models)) or 'none'}"
                )
            if not models[model_id].narrates(language):
                raise RegistryError(
                    f"{source}: '{model_id}' is the default for '{language}' but "
                    f"does not list it as a narration language"
                )
        return cls(models=models, defaults=defaults)

    def get(self, model_id: str) -> ModelSpec:
        try:
            return self.models[model_id]
        except KeyError:
            raise ModelUnavailable(
                f"no model '{model_id}'. Known: {', '.join(sorted(self.models))}"
            ) from None

    def for_language(self, language: str) -> list[ModelSpec]:
        """Every model that can narrate this language, best-validated first."""
        return sorted(
            (m for m in self.models.values() if m.narrates(language)),
            key=lambda m: (-VALIDATION_LEVELS.index(m.validation), m.id),
        )

    def resolve(self, language: str, override: str = "") -> ModelSpec:
        """The model that will narrate this book.

        An override is checked rather than trusted, because the failure it
        prevents is a book rendered in a language the engine cannot read.
        """
        if override:
            spec = self.get(override)
            if not spec.narrates(language):
                alternatives = [m.id for m in self.for_language(language)]
                raise ModelUnavailable(
                    f"'{override}' does not narrate '{language}'. "
                    + (f"These do: {', '.join(alternatives)}" if alternatives
                       else f"Nothing in the registry narrates '{language}'")
                )
            return spec

        model_id = self.defaults.get(language)
        if not model_id:
            alternatives = [m.id for m in self.for_language(language)]
            raise ModelUnavailable(
                f"no default model for '{language}'. "
                + (f"Set one in config/models.toml, or choose from: "
                   f"{', '.join(alternatives)}" if alternatives
                   else f"Nothing in the registry narrates '{language}' either")
            )
        return self.get(model_id)


def _spec(model_id: str, entry: dict, source: str) -> ModelSpec:
    def required(key: str) -> str:
        value = entry.get(key)
        if not value:
            raise RegistryError(f"{source}: model '{model_id}' has no {key}")
        return str(value)

    engine = required("engine")
    if engine not in KNOWN_ENGINES:
        raise RegistryError(
            f"{source}: model '{model_id}' names engine '{engine}', which no "
            f"backend implements. Known: {', '.join(sorted(KNOWN_ENGINES))}"
        )

    cloning = str(entry.get("cloning", "instant"))
    if cloning not in CLONING_MODES:
        raise RegistryError(
            f"{source}: model '{model_id}' has cloning '{cloning}'; "
            f"expected one of {', '.join(sorted(CLONING_MODES))}"
        )

    validation = str(entry.get("validation", "unvalidated"))
    if validation not in VALIDATION_LEVELS:
        raise RegistryError(
            f"{source}: model '{model_id}' has validation '{validation}'; "
            f"expected one of {', '.join(VALIDATION_LEVELS)}"
        )

    narration = tuple(str(x) for x in entry.get("narration_languages") or ())
    if not narration:
        raise RegistryError(
            f"{source}: model '{model_id}' lists no narration languages")

    return ModelSpec(
        id=model_id,
        engine=engine,
        environment=required("environment"),
        checkpoint=required("checkpoint"),
        narration_languages=narration,
        reference_languages=tuple(
            str(x) for x in entry.get("reference_languages") or narration),
        cloning=cloning,
        needs_reference_transcript=bool(entry.get("needs_reference_transcript", False)),
        native_sample_rate=int(entry.get("native_sample_rate", 24000)),
        controls=tuple(str(x) for x in entry.get("controls") or ()),
        validation=validation,
        package=str(entry.get("package", "")),
        revision=str(entry.get("revision", "")),
        default_char_limit=int(entry.get("default_char_limit", 250)),
        char_limits={str(k): int(v) for k, v in (entry.get("char_limits") or {}).items()},
    )


def load_registry(root: Path) -> Registry:
    """The project's registry, from `config/models.toml`."""
    return Registry.load(root / "config" / "models.toml")


def _report(registry: Registry) -> str:
    """The registry as a person needs to read it before choosing."""
    lines: list[str] = []
    if registry.defaults:
        lines.append("defaults")
        for language, model_id in sorted(registry.defaults.items()):
            lines.append(f"  {language}  {model_id}")
        lines.append("")

    lines.append("models")
    for spec in sorted(registry.models.values(), key=lambda m: m.id):
        used_for = sorted(l for l, m in registry.defaults.items() if m == spec.id)
        lines.append(
            f"  {spec.id}  ({spec.validation})"
            + (f"  default for {', '.join(used_for)}" if used_for else "")
        )
        lines.append(f"      engine {spec.engine} in {spec.environment}")
        lines.append(f"      narrates {', '.join(spec.narration_languages)}")
        if spec.reference_languages != spec.narration_languages:
            lines.append(f"      references {', '.join(spec.reference_languages)}")
        lines.append(
            f"      {spec.native_sample_rate} Hz, cloning {spec.cloning}"
            + (", reference transcript required" if spec.needs_reference_transcript else "")
        )
        if not spec.revision:
            lines.append("      revision not pinned yet")
    return "\n".join(lines)


def main() -> None:
    """`just models`."""
    from bookbinder.paths import project_root

    print(_report(load_registry(project_root())))


if __name__ == "__main__":
    main()
