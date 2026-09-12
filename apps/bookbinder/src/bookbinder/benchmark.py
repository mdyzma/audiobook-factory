"""Run one corpus through every model that claims to read its language.

The comparison this project exists to make is per language and never combined
into a single number. A model that narrates Polish beautifully and cannot say a
number is not a good Polish model, and an average that hides which is which
answers the wrong question.

So this drives, and judges nothing. It resolves each eligible model through the
registry, hands the corpus to the environment that can load it, and collects
what came back. The verdict is a person reading `docs/MODEL-EVAL-<lang>.md`
after listening, which is the only way voice likeness and long-form listening
are ever going to be scored.

Settings are the registry's, not the benchmark's. A comparison run against
settings no book would use measures something nobody will hear.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bookbinder.models import ModelSpec, Registry, load_registry

CORPORA = Path("benchmarks") / "corpora"

# Where a run's audio and numbers land. Under data/, so it is gitignored and
# `just catalog-sweep` never touches it: a benchmark is not a library.
RESULTS = Path("data") / "benchmarks"


class BenchmarkError(RuntimeError):
    """A run that will not start, with a reason worth showing."""


@dataclass
class ModelRun:
    """What one model did with one corpus."""

    model: str
    engine: str
    environment: str
    ok: bool = False
    exit_code: int = 0
    error: str = ""
    result_path: str = ""
    generations: list = field(default_factory=list)

    @property
    def failures(self) -> list:
        return [g for g in self.generations if not g.get("ok")]

    @property
    def flagged(self) -> list:
        return [g for g in self.generations
                if g.get("ok") and (g.get("silent") or g.get("short"))]


def corpus_path(root: Path, language: str) -> Path:
    path = root / CORPORA / f"{language}.toml"
    if not path.is_file():
        raise BenchmarkError(
            f"no corpus for '{language}' at {path.relative_to(root)}. "
            f"Corpora are checked in, so add one rather than generating it")
    return path


def eligible(registry: Registry, language: str) -> list[ModelSpec]:
    """Every model the registry says narrates this language.

    Including the ones nothing has been rendered with: an unvalidated entry is
    exactly what a benchmark exists to move.
    """
    return sorted(registry.for_language(language), key=lambda spec: spec.id)


def choice_payload(spec: ModelSpec, language: str, settings: dict) -> dict:
    """The resolved model, in the shape the narrator side mirrors."""
    return {
        "id": spec.id, "engine": spec.engine, "environment": spec.environment,
        "checkpoint": spec.checkpoint, "revision": spec.revision,
        "native_sample_rate": spec.native_sample_rate,
        "char_limit": spec.char_limit(language),
        # The engine's pin, and only then an explicit override. A benchmark
        # left to `[synth]` would measure two engines at one engine's settings.
        "settings": spec.pinned(settings),
        "unsupported": spec.unsupported_controls(settings),
    }


def interpreter_for(root: Path, spec: ModelSpec) -> Path:
    path = root / "apps" / spec.environment / ".venv" / "bin" / "python"
    if not path.is_file():
        raise BenchmarkError(
            f"'{spec.id}' runs in the '{spec.environment}' environment, which is "
            f"not set up. Run `just setup-{spec.environment}`")
    return path


def run_model(root: Path, spec: ModelSpec, language: str, voice: str,
              out: Path, repeats: int, device: str, settings: dict) -> ModelRun:
    """Hand the corpus to the one environment that can load this model."""
    outcome = ModelRun(model=spec.id, engine=spec.engine, environment=spec.environment)
    out.mkdir(parents=True, exist_ok=True)

    choice = out / "choice.json"
    choice.write_text(json.dumps(choice_payload(spec, language, settings), indent=2),
                      encoding="utf-8")
    try:
        interpreter = interpreter_for(root, spec)
    except BenchmarkError as exc:
        outcome.error = str(exc)
        return outcome

    finished = subprocess.run(
        [str(interpreter), "-m", "narrator.bench", str(corpus_path(root, language)),
         "--choice", str(choice), "--voice", voice, "--out", str(out),
         "--repeats", str(repeats), "--device", device],
        cwd=root)
    outcome.exit_code = finished.returncode

    result = out / "result.json"
    if result.is_file():
        try:
            outcome.generations = json.loads(
                result.read_text(encoding="utf-8")).get("generations", [])
            outcome.result_path = str(result.relative_to(root))
        except ValueError:
            outcome.error = "the result file could not be read"
    elif not outcome.error:
        outcome.error = f"the run exited {finished.returncode} without writing a result"
    outcome.ok = bool(outcome.generations) and not outcome.failures
    return outcome


def report(language: str, runs: list[ModelRun]) -> str:
    """What came back, per model, with nothing averaged across categories."""
    lines = [f"{language}: {len(runs)} model(s)", ""]
    for run in runs:
        total = len(run.generations)
        lines.append(f"  {run.model}  ({run.engine} in {run.environment})")
        if run.error:
            lines.append(f"      {run.error}")
            continue
        lines.append(f"      {total - len(run.failures)}/{total} generated")

        by_category: dict[str, list] = {}
        for made in run.generations:
            by_category.setdefault(made.get("category", "?"), []).append(made)
        for category, made in sorted(by_category.items()):
            bad = [g for g in made if not g.get("ok")]
            odd = [g for g in made if g.get("ok") and (g.get("silent") or g.get("short"))]
            speeds = [g["realtime"] for g in made if g.get("ok") and g.get("realtime")]
            note = f"{len(made) - len(bad)}/{len(made)}"
            if speeds:
                note += f", {sum(speeds) / len(speeds):.2f}x realtime"
            if bad:
                note += f", {len(bad)} failed"
            if odd:
                note += f", {len(odd)} silent or short"
            lines.append(f"        {category:16} {note}")
        for made in run.failures[:3]:
            lines.append(f"        ! {made.get('passage')}: {made.get('error', '')[:80]}")
    lines += ["", "  Nothing here is a verdict. Listening decides voice likeness and",
              "  long-form quality; these are the failures you can measure."]
    return "\n".join(lines)


def run(root: Path, language: str, voice: str, repeats: int = 1,
        device: str = "auto", only: str = "", settings: dict | None = None) -> dict:
    # Checked before anything is resolved or created: a run that cannot happen
    # should not leave a results directory behind to explain later.
    corpus_path(root, language)

    registry = load_registry(root)
    models = eligible(registry, language)
    if only:
        models = [m for m in models if m.id == only]
    if not models:
        raise BenchmarkError(
            f"no model in the registry narrates '{language}'"
            + (f" under the id '{only}'" if only else ""))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = root / RESULTS / f"{stamp}-{language}"
    runs = [run_model(root, spec, language, voice, base / spec.id, repeats, device,
                      settings or {}) for spec in models]

    summary = {"language": language, "voice": voice, "repeats": repeats,
               "device": device, "started": stamp,
               "models": [r.model for r in runs],
               "ok": all(r.ok for r in runs),
               "path": str(base.relative_to(root))}
    (base / "summary.json").write_text(
        json.dumps(summary | {"runs": [vars(r) for r in runs]},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary | {"runs": runs, "report": report(language, runs)}


def main() -> None:
    """`just bench <language> <voice>`."""
    import typer

    from bookbinder.paths import project_root

    app = typer.Typer(add_completion=False)

    @app.command()
    def bench(
        language: str = typer.Argument(..., help="pl or en"),
        voice: str = typer.Argument(..., help="Voice name, as in data/voices/"),
        repeats: int = typer.Option(1, min=1, help="Generations per passage"),
        device: str = typer.Option("auto", help="auto | cuda | mps | cpu"),
        model: str = typer.Option("", help="Only this model, rather than every eligible one"),
        settings: Path = typer.Option(None, help="JSON of pinned controls, applied to "
                                                 "every backend that implements them"),
    ) -> None:
        # Pinned rather than tuned per engine. Each backend takes the subset it
        # implements and names the rest, which is how the same run can be fair
        # to two engines whose controls do not line up.
        pinned = {}
        if settings is not None:
            if not settings.is_file():
                typer.echo(f"no settings file at {settings}", err=True)
                raise typer.Exit(2)
            pinned = json.loads(settings.read_text(encoding="utf-8"))
        try:
            outcome = run(project_root(), language, voice, repeats, device, model,
                          pinned)
        except BenchmarkError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2)
        typer.echo("\n" + outcome["report"])
        typer.echo(f"\n-> {outcome['path']}")
        if not outcome["ok"]:
            raise typer.Exit(1)

    app()


if __name__ == "__main__":
    main()
