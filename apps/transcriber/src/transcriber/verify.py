"""Stage 6 (optional) - check that the audio says what the text said.

XTTS fails quietly. It can truncate a long chunk, skip a clause, or repeat a
phrase, and none of that raises anything: the wav is written, the duration
looks plausible, and the defect only surfaces when someone listens. On a
twenty-hour book nobody listens to all of it before publishing.

So this reads the rendered audio back with WhisperX and compares the
transcript to the source text. It runs in the transcriber environment, where
WhisperX already lives, and writes a report the same shape bookbinder
validates.

Word error rate is the measure: substitutions plus deletions plus insertions,
over the number of source words. Small values are normal, because the
transcriber mishears too. What matters is the outliers.
"""

from __future__ import annotations

from transcriber.paths import project_root

import json
import re
import tomllib
import unicodedata
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)

# Mirrors bookbinder.manifest.DRY_RUN_MARKER; this environment cannot import it.
DRY_RUN_MARKER = ".dry-run.json"

SCHEMA_VERSION = 5


def normalise_for_compare(text: str) -> list[str]:
    """Strip everything that a transcriber cannot be expected to reproduce.

    Punctuation, case and diacritic form are not defects in synthesis, so they
    are removed before comparing. What remains is the sequence of words.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return text.split()


def word_error_rate(expected: str, heard: str) -> float:
    """Levenshtein distance over words, divided by the expected word count."""
    ref, hyp = normalise_for_compare(expected), normalise_for_compare(heard)
    if not ref:
        return 0.0 if not hyp else 1.0

    # Two rows are enough; a chunk is at most a few dozen words.
    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp, start=1):
            current.append(min(
                previous[j] + 1,                                    # deletion
                current[j - 1] + 1,                                 # insertion
                previous[j - 1] + (ref_word != hyp_word),           # substitution
            ))
        previous = current
    return round(previous[-1] / len(ref), 4)


def load_qa_config(root: Path) -> dict:
    path = root / "config" / "pipeline.toml"
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get("qa", {})


@app.command()
def main(
    slug: str = typer.Argument(..., help="Book slug; reads data/audio/<slug>/rendered.jsonl"),
    model: str = typer.Option("large-v3", help="Whisper model size"),
    language: str = typer.Option("", help="Defaults to the language in the manifest"),
    device: str = typer.Option("auto", help="auto | cuda | cpu"),
    max_wer: float = typer.Option(0.0, help="0 = use the value in config/pipeline.toml"),
    limit: int = typer.Option(0, help="Check only the first N chunks"),
    sample: int = typer.Option(
        0, help="Check every Nth chunk instead of all of them; much faster on a long book"
    ),
) -> None:
    # Both checks run before whisperx is imported: loading it costs seconds and
    # a model download, and neither failure needs it.
    root = project_root()
    audio_dir = root / "data" / "audio" / slug
    rendered_path = audio_dir / "rendered.jsonl"
    if not rendered_path.exists():
        raise typer.BadParameter(f"missing {rendered_path}; run `just synth {slug}` first")

    # Transcribing silence takes as long as transcribing narration and every
    # fragment fails, which reads as a catastrophic voice problem rather than
    # the missing render it is. Mirrors bookbinder.manifest.DRY_RUN_MARKER.
    if (audio_dir / DRY_RUN_MARKER).exists():
        raise typer.BadParameter(
            f"data/audio/{slug}/ is dry-run silence, not narration. "
            f"Run `just synth {slug}` first."
        )

    import whisperx
    from tqdm import tqdm

    from transcriber.auto_label import pick_device

    cfg = load_qa_config(root)
    threshold = max_wer or cfg.get("max_wer", 0.15)

    chunks = [json.loads(l) for l in rendered_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if sample > 1:
        chunks = chunks[::sample]
    if limit:
        chunks = chunks[:limit]
    chunks = [c for c in chunks if c.get("audio_path")]
    if not chunks:
        raise typer.BadParameter("no rendered audio to check")

    lang = language or chunks[0].get("language", "pl")
    dev, compute_type = pick_device(device)
    typer.echo(f"checking {len(chunks)} chunks with whisperx {model} on {dev}")

    asr = whisperx.load_model(model, dev, compute_type=compute_type, language=lang)

    findings = []
    total_wer = 0.0
    for chunk in tqdm(chunks, desc="verify"):
        wav = root / chunk["audio_path"]
        if not wav.exists():
            findings.append({"chunk_id": chunk["id"], "expected": chunk["text"],
                             "heard": "", "wer": 1.0})
            continue
        audio = whisperx.load_audio(str(wav))
        result = asr.transcribe(audio, batch_size=8)
        heard = " ".join(seg["text"] for seg in result["segments"]).strip()

        wer = word_error_rate(chunk["text"], heard)
        total_wer += wer
        if wer > threshold:
            findings.append({"chunk_id": chunk["id"], "expected": chunk["text"],
                             "heard": heard, "wer": wer})

    report = {
        "schema_version": SCHEMA_VERSION,
        "slug": slug,
        "model": model,
        "max_wer": threshold,
        "chunks_checked": len(chunks),
        "mean_wer": round(total_wer / len(chunks), 4),
        "findings": findings,
    }
    report_path = root / "data" / "audio" / slug / "qa_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    typer.echo(
        f"mean WER {report['mean_wer']:.3f} over {len(chunks)} chunks; "
        f"{len(findings)} above {threshold}\n-> {report_path}"
    )
    for finding in findings[:10]:
        typer.echo(f"  {finding['chunk_id']} wer={finding['wer']:.2f}")
        typer.echo(f"    said:  {finding['expected'][:80]}")
        typer.echo(f"    heard: {finding['heard'][:80]}")
    if findings:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()