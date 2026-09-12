"""Render a fixed corpus with one backend, and write down what happened.

Comparing two engines means giving them the same text and measuring the same
things. That sounds obvious and is easy to get wrong: chunk the corpus and each
engine reads slightly different fragments; average the results and a model that
narrates beautifully but mangles every number scores well.

So passages go to the backend verbatim, one call each, tagged with the category
they exist to expose, and every generation is recorded separately. Nothing is
averaged here. Averaging is a decision about what matters, and it belongs in
the result sheet where somebody can argue with it.

This runs in whichever environment the model needs, the same way stage 4 does,
because `apps/chatterbox` installs this package for exactly that reason. What
it must not do is decide which model to run: the registry lives in bookbinder
and is passed in as a resolved choice, so a benchmark cannot disagree with what
a book would actually be rendered with.

Repeats are not padding. These engines are stochastic, and a failure that
happens one time in five is the failure that ruins a twenty-hour render.
"""

from __future__ import annotations

import json
import time
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import typer

from narrator.choice import ModelChoice

app = typer.Typer(add_completion=False)

# A generation that produces less audio than this said nothing, whatever the
# engine reported. Usually a refusal that came back as silence.
MIN_AUDIBLE_SECONDS = 0.15

# Speech runs at roughly this many characters a second across both languages,
# so a generation far below it has almost certainly been truncated. Only a
# flag for a person to look at, never a verdict.
CHARS_PER_SECOND = 14.0
TRUNCATION_RATIO = 0.55


@dataclass
class Generation:
    """One call to the backend, and everything measured about it."""

    passage: str
    category: str
    repeat: int
    chars: int
    ok: bool = False
    seconds: float = 0.0
    audio_seconds: float = 0.0
    realtime: float = 0.0
    sample_rate: int = 0
    peak: float = 0.0
    silent: bool = False
    short: bool = False
    error: str = ""
    path: str = ""


@dataclass
class Result:
    """One corpus, one model, one voice."""

    model: str
    engine: str
    language: str
    voice: str
    device: str
    corpus_version: int
    settings: dict = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    generations: list = field(default_factory=list)

    @property
    def failures(self) -> list:
        return [g for g in self.generations if not g.ok]


def read_corpus(path: Path) -> dict:
    if not path.is_file():
        raise typer.BadParameter(f"no corpus at {path}")
    loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    if not loaded.get("passage"):
        raise typer.BadParameter(f"{path} has no passages")
    return loaded


def read_choice(path: Path) -> ModelChoice:
    """The model as bookbinder's registry resolved it.

    Read rather than decided here. narrator cannot import the registry, and a
    benchmark that picked its own settings would be measuring something no book
    would ever be rendered with.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {f for f in ModelChoice.__dataclass_fields__}
    return ModelChoice(**{k: v for k, v in data.items() if k in known})


def measure(wav, rate: int) -> tuple[float, float]:
    import numpy as np

    array = np.asarray(wav, dtype="float32")
    if not array.size or not rate:
        return 0.0, 0.0
    return array.size / rate, float(np.max(np.abs(array)))


def looks_truncated(chars: int, audio_seconds: float) -> bool:
    """Whether far less was said than the text implies.

    A flag, not a verdict. Chatterbox publishes no character limit and caps
    generation by token count, so where it stops is a thing to be measured
    rather than read off a page, and this is what notices.
    """
    if not chars or not audio_seconds:
        return False
    return audio_seconds < (chars / CHARS_PER_SECOND) * TRUNCATION_RATIO


@app.command()
def main(
    corpus: Path = typer.Argument(..., help="benchmarks/corpora/<lang>.toml"),
    choice: Path = typer.Option(..., help="Resolved model, written by the driver"),
    voice: str = typer.Option(..., help="Voice name, as in data/voices/"),
    out: Path = typer.Option(..., help="Where to write the audio and the result"),
    repeats: int = typer.Option(1, min=1, help="Generations per passage"),
    device: str = typer.Option("auto", help="auto | cuda | mps | cpu"),
    keep_audio: bool = typer.Option(True, help="Write the wavs, not only the numbers"),
) -> None:
    import soundfile as sf

    from narrator.backends import backend_for
    from narrator.paths import project_root

    root = project_root()
    loaded = read_corpus(corpus)
    model = read_choice(choice)
    backend = backend_for(model, root, device)

    out.mkdir(parents=True, exist_ok=True)
    result = Result(
        model=model.id or model.engine, engine=model.engine,
        language=str(loaded.get("language") or ""), voice=voice, device=device,
        corpus_version=int(loaded.get("version") or 0),
        settings=dict(model.settings),
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    for passage in loaded["passage"]:
        text = passage.get("text") or ""
        for repeat in range(1, repeats + 1):
            record = Generation(passage=str(passage.get("id") or ""),
                                category=str(passage.get("category") or ""),
                                repeat=repeat, chars=len(text))
            wav = None
            started = time.time()
            try:
                wav = backend.speak(text, result.language, voice, dict(model.settings))
                record.sample_rate = backend.sample_rate(voice)
                record.audio_seconds, record.peak = measure(wav, record.sample_rate)
                record.ok = True
            except Exception as exc:
                record.error = f"{type(exc).__name__}: {exc}"
            record.seconds = round(time.time() - started, 3)

            if record.ok:
                record.realtime = round(record.audio_seconds / record.seconds, 2) \
                    if record.seconds else 0.0
                record.silent = (record.audio_seconds < MIN_AUDIBLE_SECONDS
                                 or record.peak < 1e-4)
                record.short = looks_truncated(record.chars, record.audio_seconds)
                if keep_audio and wav is not None:
                    name = f"{record.passage}.{repeat}.wav"
                    sf.write(out / name, wav, record.sample_rate)
                    record.path = name

            result.generations.append(record)
            typer.echo(
                f"  {record.passage:24} {repeat}/{repeats} "
                + ("ok " if record.ok else "FAILED ")
                + (f"{record.audio_seconds:5.1f}s {record.realtime:5.2f}x"
                   if record.ok else record.error[:70])
                + ("  SILENT" if record.silent else "")
                + ("  SHORT" if record.short else ""))

    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = asdict(result)
    payload["generations"] = [asdict(g) for g in result.generations]
    (out / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    total = len(result.generations)
    typer.echo(f"\n{result.model} on {result.language}: {total - len(result.failures)}"
               f"/{total} generated -> {out / 'result.json'}")
    if result.failures:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
