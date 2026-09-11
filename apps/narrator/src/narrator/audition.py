"""Hear a voice on this book's own prose, at the settings it will actually use.

The audition rendered while cloning says one fixed sentence at one fixed
temperature, and records nothing about either. It answers "did the clone work",
which is worth knowing once, and nothing about the question anybody actually
has: will this voice read *this book* well enough to spend a night on it.

Three things change that. The passage comes from the book, so the voice is
judged on the prose it will read rather than on a sentence written to flatter
it. The settings come from `book.json`, the same ones synthesis will use, via
the same backend, so what is heard is what will be made. And everything that
produced the sample is written down beside it, because an audition you cannot
attribute is an opinion you cannot act on.

Samples are named by what made them, so asking twice for the same thing costs
nothing and two settings are two files rather than one overwriting the other.
The audition the clone makes is left alone: it is what `just level` measures,
and moving it would change every voice's correction.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import typer

from narrator.choice import ModelChoice
from narrator.fingerprint import voice_revision
from narrator.paths import project_root

app = typer.Typer(add_completion=False)

# Long enough to judge pacing and a sentence boundary, short enough to render
# while somebody waits.
PASSAGE_MIN_CHARS = 120
PASSAGE_MAX_CHARS = 600

# Named by what produced them; this is how long that name is.
KEY_CHARS = 12

WHAT_IT_SAYS = {
    "pl": "To jest próbka sklonowanego głosu. Sprawdź intonację, oddechy i tempo.",
    "en": "This is a sample of the cloned voice. Check intonation, breathing and pace.",
}


def auditions_dir(root: Path, voice: str) -> Path:
    return root / "data" / "voices" / voice / "auditions"


def key_for(text: str, language: str, voice: str, revision: str,
            settings: dict, model: str) -> str:
    """A name that changes when anything that shaped the sample changes."""
    digest = hashlib.sha256()
    for part in (text, language, voice, revision, model,
                 json.dumps(settings, sort_keys=True)):
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:KEY_CHARS]


def read_book(root: Path, slug: str) -> dict:
    path = root / "data" / "book" / slug / "book.json"
    if not path.is_file():
        raise typer.BadParameter(
            f"no book at {path}; `just chunk {slug}` writes it")
    return json.loads(path.read_text(encoding="utf-8"))


def passage(root: Path, slug: str, role: str = "") -> dict:
    """A fragment worth judging a voice on.

    Taken from the middle of the book rather than the start: the opening is
    front matter and a title as often as it is prose, and neither says much
    about how a chapter will sound. Preference goes to a fragment long enough
    to carry a sentence boundary, and to the role being auditioned, so a
    dialogue voice is heard saying dialogue.
    """
    path = root / "data" / "book" / slug / "chunks.jsonl"
    if not path.is_file():
        raise typer.BadParameter(f"no fragments at {path}; `just chunk {slug}` first")

    chunks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                chunks.append(json.loads(line))
            except ValueError:
                continue
    if not chunks:
        raise typer.BadParameter(f"{slug} has no fragments to audition")

    wanted = [c for c in chunks if not role or c.get("role") == role] or chunks
    sized = [c for c in wanted
             if PASSAGE_MIN_CHARS <= len(c.get("text") or "") <= PASSAGE_MAX_CHARS]
    pool = sized or wanted
    return pool[len(pool) // 2]


def render(root: Path, voice: str, text: str, language: str, settings: dict,
           choice: ModelChoice, device: str = "auto", source: dict | None = None
           ) -> tuple[Path, dict, bool]:
    """Say `text` in `voice`, and write down everything that shaped it."""
    from narrator.backends import backend_for
    from narrator.synth import apply_gain, voice_gain

    revision = voice_revision(root, voice)
    key = key_for(text, language, voice, revision, settings, choice.identity)
    out_dir = auditions_dir(root, voice)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path, record_path = out_dir / f"{key}.wav", out_dir / f"{key}.json"

    if wav_path.is_file() and record_path.is_file():
        return wav_path, json.loads(record_path.read_text(encoding="utf-8")), True

    import soundfile as sf

    backend = backend_for(choice, root, device)
    wav = backend.speak(text, language, voice, settings)
    rate = backend.sample_rate(voice)
    gain = voice_gain(root, voice)
    wav, clipped = apply_gain(wav, gain)

    staged = wav_path.with_name(f".{wav_path.name}.part")
    try:
        sf.write(staged, wav, rate)
        staged.replace(wav_path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise

    record = {
        "voice": voice, "voice_revision": revision, "language": language,
        "text": text, "settings": settings, "model": choice.identity,
        "engine": choice.engine, "sample_rate": rate,
        "gain_db": gain, "clipped_samples": clipped, "device": device,
        "seconds": round(len(wav) / rate, 2),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **(source or {}),
    }
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return wav_path, record, False


@app.command()
def main(
    voice: str = typer.Argument(..., help="Voice name, as in data/voices/"),
    book: str = typer.Option("", help="Judge the voice on this book's own prose"),
    role: str = typer.Option("", help="narrator | dialogue, when auditioning a cast"),
    text: str = typer.Option("", help="Say this instead of a passage from the book"),
    language: str = typer.Option("", help="Defaults to the book's, or the voice's"),
    device: str = typer.Option("auto", help="auto | cuda | mps | cpu"),
) -> None:
    root = project_root()
    source: dict = {}
    settings: dict = {}
    choice = ModelChoice()

    if book:
        meta = read_book(root, book)
        choice = ModelChoice.from_book(meta)
        settings = dict(choice.settings)
        settings.update((meta.get("cast_settings") or {}).get(role or "narrator", {}))
        language = language or meta.get("language", "")
        if not text:
            chosen = passage(root, book, role)
            text = chosen.get("text") or ""
            language = language or chosen.get("language", "")
            source = {"book": book, "chunk_id": chosen.get("id", ""),
                      "chapter": chosen.get("chapter_title", ""),
                      "role": chosen.get("role", "")}
    if not language:
        profile = json.loads(
            (root / "data/voices" / f"{voice}.json").read_text(encoding="utf-8"))
        language = profile.get("language", "en")
    if not text:
        text = WHAT_IT_SAYS.get(language, WHAT_IT_SAYS["en"])

    path, record, reused = render(root, voice, text, language, settings, choice,
                                  device, source)

    typer.echo(f"{'already rendered' if reused else 'rendered'} -> {path}")
    typer.echo(f"  {record['seconds']:.1f} s, {record['model'] or 'default backend'}"
               + (f", {record['gain_db']:+.1f} dB" if record["gain_db"] else ""))
    if source:
        typer.echo(f"  {book} · {record.get('chapter') or 'no chapter'}"
                   f" · {record.get('role') or 'narrator'}")
    if settings:
        typer.echo("  " + ", ".join(f"{k}={v}" for k, v in sorted(settings.items())))
    else:
        typer.echo("  the backend's own defaults; give --book to use a book's settings")
    if record["clipped_samples"]:
        typer.echo(f"warning: {record['clipped_samples']} sample(s) clipped; this "
                   f"voice's level correction is too large", err=True)
    typer.echo(f"  {record['text'][:200]}")


if __name__ == "__main__":
    app()
