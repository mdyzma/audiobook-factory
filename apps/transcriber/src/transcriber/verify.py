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

SCHEMA_VERSION = 6


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


def publish_text(path: Path, content: str) -> Path:
    """Write beside the target and rename into place.

    Mirrors bookbinder.manifest.publish_text; this environment cannot import
    it. A rename within a directory is atomic, so a run killed part-way leaves
    either the previous file or the complete new one, never half of one that
    parses as a smaller book.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.part")
    try:
        staged.write_text(content, encoding="utf-8")
        staged.replace(path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return path


def load_qa_config(root: Path) -> dict:
    path = root / "config" / "pipeline.toml"
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get("qa", {})


def group_by_language(chunks: list[dict], default: str = "pl") -> dict[str, list[dict]]:
    """Fragments grouped by the language they were narrated in.

    A mixed-language book needs a transcriber per language. Taking the first
    fragment's language for the whole run, as this used to, means every
    fragment in the other language is heard by the wrong model and reported as
    a synthesis failure.
    """
    groups: dict[str, list[dict]] = {}
    for chunk in chunks:
        groups.setdefault(chunk.get("language") or default, []).append(chunk)
    return groups


def thresholds_for(cfg: dict, override: float = 0.0) -> tuple[float, dict[str, float]]:
    """The word error rate above which a fragment is worth looking at.

    Per language, because the transcriber is not equally good at all of them
    and a rate that means trouble in English is ordinary in Polish. An explicit
    `--max-wer` applies to everything and is how a one-off check is tightened.
    """
    base = float(cfg.get("max_wer", 0.15))
    per_language = {str(k): float(v)
                    for k, v in (cfg.get("max_wer_by_language") or {}).items()}
    if override:
        return override, {}
    return base, per_language


def audio_fingerprint(chunks: list[dict]) -> str:
    """Digest of the fragments this report describes.

    A quality report outlives the audio it was made from. Recording which
    fragments were checked is what lets a reader tell a current report from one
    describing a render that has since been redone.
    """
    import hashlib

    pairs = sorted((c["id"], c.get("fingerprint") or "") for c in chunks)
    digest = hashlib.sha256(json.dumps(pairs).encode("utf-8"))
    return digest.hexdigest()[:32]


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
    threshold, per_language = thresholds_for(cfg, max_wer)

    everything = [json.loads(l) for l in
                  rendered_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    available = [c for c in everything if c.get("audio_path")]

    chunks = available[::sample] if sample > 1 else list(available)
    if limit:
        chunks = chunks[:limit]
    if not chunks:
        raise typer.BadParameter("no rendered audio to check")

    # One transcriber per language, loaded in turn. An explicit --language
    # forces a single model, which is how a book whose manifest is wrong gets
    # checked without re-chunking it first.
    groups = ({language: chunks} if language
              else group_by_language(chunks, default="pl"))

    dev, compute_type = pick_device(device)
    typer.echo(
        f"checking {len(chunks)} of {len(available)} rendered chunks with "
        f"whisperx {model} on {dev}\n"
        f"languages: {', '.join(f'{k} ({len(v)})' for k, v in sorted(groups.items()))}"
    )

    findings = []
    total_wer = 0.0
    for lang, group in sorted(groups.items()):
        limit_here = per_language.get(lang, threshold)
        asr = whisperx.load_model(model, dev, compute_type=compute_type, language=lang)

        for chunk in tqdm(group, desc=f"verify {lang}"):
            wav = root / chunk["audio_path"]
            if not wav.exists():
                findings.append({"chunk_id": chunk["id"], "expected": chunk["text"],
                                 "heard": "", "wer": 1.0, "language": lang,
                                 "source_text": chunk.get("source_text", "")})
                total_wer += 1.0
                continue
            audio = whisperx.load_audio(str(wav))
            result = asr.transcribe(audio, batch_size=8)
            heard = " ".join(seg["text"] for seg in result["segments"]).strip()

            # Compared against the spoken text, which is what the model was
            # given. The printed spelling rides along so a finding can be found
            # in the book: "doktor" was heard, but the page says "dr.".
            wer = word_error_rate(chunk["text"], heard)
            total_wer += wer
            if wer > limit_here:
                findings.append({"chunk_id": chunk["id"], "expected": chunk["text"],
                                 "heard": heard, "wer": wer, "language": lang,
                                 "source_text": chunk.get("source_text", "")})

        del asr

    book_path = root / "data" / "book" / slug / "book.json"
    synthesis_model = ""
    if book_path.exists():
        synthesis_model = (json.loads(book_path.read_text(encoding="utf-8"))
                           .get("model") or {}).get("id", "")

    report = {
        "schema_version": SCHEMA_VERSION,
        "slug": slug,
        "model": model,
        "max_wer": threshold,
        "thresholds": per_language,
        "chunks_checked": len(chunks),
        "mean_wer": round(total_wer / len(chunks), 4),
        "coverage": {
            "checked": len(chunks),
            "available": len(available),
            "sample": sample if sample > 1 else 0,
            "languages": sorted(groups),
        },
        "audio_fingerprint": audio_fingerprint(chunks),
        "synthesis_model": synthesis_model,
        "findings": findings,
    }
    report_path = publish_text(
        root / "data" / "audio" / slug / "qa_report.json",
        json.dumps(report, ensure_ascii=False, indent=2))

    # Said plainly, because a sampled pass and a full one are different claims
    # and one read as the other is how a book ships on every twentieth fragment.
    scope = ("every rendered fragment" if len(chunks) == len(available)
             else f"{len(chunks)} of {len(available)} rendered fragments"
                  + (f", every {sample}th" if sample > 1 else ""))
    typer.echo(
        f"mean WER {report['mean_wer']:.3f} across {scope}; "
        f"{len(findings)} above threshold\n-> {report_path}"
    )
    for finding in findings[:10]:
        typer.echo(f"  {finding['chunk_id']} [{finding['language']}] "
                   f"wer={finding['wer']:.2f}")
        typer.echo(f"    said:  {finding['expected'][:80]}")
        typer.echo(f"    heard: {finding['heard'][:80]}")
        if finding["source_text"]:
            typer.echo(f"    page:  {finding['source_text'][:80]}")
    if findings:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()