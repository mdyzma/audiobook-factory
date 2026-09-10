"""Stage 4 - render every chunk in the manifest to a wav file.

Resumable by design: a book is tens of thousands of chunks and a crash at
hour six should not cost the first six hours. Existing wavs are skipped, so
re-running the command continues where it stopped.

Multi-voice aware. Each chunk carries a `role`, and `book.json` carries the
role-to-voice mapping the chunker resolved. Speaker latents are loaded once
per voice, not once per chunk, which is the difference between a book that
renders overnight and one that does not.

Reads data/book/<slug>/chunks.jsonl, writes data/audio/<slug>/<chunk id>.wav,
rendered.jsonl with real durations, and report.json describing the run.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import torch
import typer

from narrator.paths import project_root
from narrator.backends import UnsupportedEngine, backend_for
from narrator.choice import ModelChoice
from narrator.engine import pick_device
from narrator.fingerprint import fragment_fingerprint, voice_revision

app = typer.Typer(add_completion=False)

# narrator cannot import bookbinder (different environments, incompatible
# numpy), so the report shape is mirrored here. It is validated on the
# bookbinder side; docs/schemas/render_report_v6.json is the contract.
SCHEMA_VERSION = 6

# Mirrors bookbinder.manifest.DRY_RUN_MARKER. A dry run leaves silence at
# exactly the paths a real render writes, and resume skips any fragment that
# already has a wav, so without this the two combine into a book-length file of
# nothing. Keep the name in step with bookbinder.
DRY_RUN_MARKER = ".dry-run.json"


# How often progress.json is rewritten. Synthesising a fragment takes seconds,
# so per-fragment writes are free; skipped fragments are near-instant on a
# resumed run, hence the throttle.
PROGRESS_INTERVAL_SEC = 0.5


# What each wav in this directory was rendered from. One JSON line per
# fragment, appended as it lands rather than written at the end, so a render
# killed at hour six leaves the first six hours reusable.
FINGERPRINTS = "fingerprints.jsonl"


def discard_dry_run(out_dir: Path) -> int:
    """Remove dry-run silence so resume cannot mistake it for narration.

    A dry run writes a wav per fragment at the estimated duration, which is
    what makes it useful for checking structure and what makes it dangerous
    here: the resume rule below is `skip anything that already has a wav`, so
    silence left in place would be adopted wholesale and the book would come
    out empty. Returns the number of files removed.
    """
    marker = out_dir / DRY_RUN_MARKER
    if not marker.exists():
        return 0
    removed = 0
    for wav in out_dir.glob("*.wav"):
        wav.unlink()
        removed += 1
    (out_dir / "rendered.jsonl").unlink(missing_ok=True)
    (out_dir / FINGERPRINTS).unlink(missing_ok=True)
    marker.unlink()
    return removed


def read_fingerprints(out_dir: Path) -> dict[str, str]:
    """Fingerprints for the audio already in this directory.

    A later line wins: re-rendering one fragment appends rather than rewriting,
    which is what keeps the file append-only and crash-safe.
    """
    path = out_dir / FINGERPRINTS
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            found[entry["id"]] = entry["fingerprint"]
        except (ValueError, KeyError):
            # A line torn in half by a kill. Everything before it still counts.
            continue
    return found


def append_fingerprint(out_dir: Path, chunk_id: str, fingerprint: str) -> None:
    with (out_dir / FINGERPRINTS).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": chunk_id, "fingerprint": fingerprint}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# A fault in the voice or the model repeats for every fragment, so failing the
# whole book one fragment at a time wastes hours to reach a conclusion
# available after the first few.
SYSTEMIC_FAILURES = 5


def classify_failure(exc: BaseException) -> str:
    """Whose fault this is: the fragment, the voice, or the model.

    A fragment fault is one piece of text the engine could not read, and the
    rest of the book is unaffected. A voice or model fault is the run's, and
    retrying ten thousand fragments against it produces ten thousand identical
    errors and no audio.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if isinstance(exc, FileNotFoundError) or "voice profile" in text or "latent" in text:
        return "voice"
    if any(word in text for word in ("checkpoint", "cuda", "out of memory",
                                     "load_model", "no such file or directory")):
        return "model"
    return "fragment"


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


def write_progress(path: Path, payload: dict) -> None:
    """Atomic, because a reader may poll this while it is being rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_synth_config(root: Path) -> dict:
    path = root / "config" / "pipeline.toml"
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get("synth", {})


@app.command()
def main(
    slug: str = typer.Argument(..., help="Book slug under data/book/"),
    voice: str = typer.Option("", help="Override the cast and use one voice throughout"),
    device: str = typer.Option("auto", help="auto | cuda | mps | cpu"),
    limit: int = typer.Option(0, help="Render only the first N chunks (smoke test)"),
    force: bool = typer.Option(False, help="Re-render chunks that already have audio"),
    only: str = typer.Option(
        "", help="Comma-separated chunk ids to re-render, e.g. ch002_0041. "
                 "Implies --force for those, and merges into the existing "
                 "rendered.jsonl rather than replacing it."
    ),
) -> None:
    import soundfile as sf
    from tqdm import tqdm

    root = project_root()
    book_dir = root / "data" / "book" / slug
    chunks_path = book_dir / "chunks.jsonl"
    if not chunks_path.exists():
        raise typer.BadParameter(f"missing {chunks_path}; run `just chunk {slug}` first")

    book = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    cast: dict[str, str] = book.get("cast") or {}

    # Which backend this book was chunked for. Read rather than chosen here:
    # the fragment sizes were packed against this model's limit, so rendering
    # them with another one is not the same book.
    choice = ModelChoice.from_book(book)

    all_chunks = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # `--only` re-renders named fragments, which is how a single bad one gets
    # fixed after a quality check without redoing the book.
    wanted = [c.strip() for c in only.split(",") if c.strip()]
    if wanted:
        by_id = {c["id"]: c for c in all_chunks}
        missing = [c for c in wanted if c not in by_id]
        if missing:
            raise typer.BadParameter(f"no such chunk(s): {', '.join(missing)}")
        chunks = [by_id[c] for c in wanted]
        force = True
    else:
        chunks = all_chunks[:limit] if limit else all_chunks

    def voice_for(chunk: dict) -> str:
        if voice:
            return voice
        return cast.get(chunk.get("role", "narrator")) or cast.get("narrator") or ""

    voices = sorted({voice_for(c) for c in chunks})
    if not all(voices):
        raise typer.BadParameter(
            f"no voice for some chunks; `just chunk {slug}` records the cast, "
            f"or pass --voice to override"
        )

    dev = pick_device(device)
    cfg = load_synth_config(root)
    out_dir = root / "data" / "audio" / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    discarded = discard_dry_run(out_dir)
    if discarded:
        typer.echo(f"discarding {discarded} dry-run silence files before rendering")

    try:
        pool = backend_for(choice, root, dev)
    except UnsupportedEngine as exc:
        raise typer.BadParameter(str(exc)) from exc

    # Configured controls this backend does not implement. Saying so is the
    # point: a setting that is quietly ignored looks like one that had no
    # effect, and the difference matters when comparing two engines.
    settings = dict(choice.settings) or {
        k: v for k, v in cfg.items() if isinstance(v, (int, float))}
    if choice.unsupported:
        typer.echo(
            f"note: {choice.id or 'this backend'} ignores "
            f"{', '.join(choice.unsupported)}", err=True)

    # What each existing wav was rendered from. Resume compares against this
    # rather than trusting a filename: the wav for ch001_0004 exists whether it
    # was made by this model from this text or by a different one from text
    # that has since been re-chunked.
    previous = read_fingerprints(out_dir)
    revisions = {v: voice_revision(root, v) for v in voices}

    # Per-role controls sit on top of the book's, so a dialogue voice can read
    # slightly faster than the narration. Merged per fragment rather than per
    # run, and folded into the fingerprint: changing a role's speed has to
    # invalidate that role's audio and nothing else.
    cast_settings: dict[str, dict] = book.get("cast_settings") or {}

    def settings_for(chunk: dict) -> dict:
        role = chunk.get("role") or "narrator"
        return {**settings, **cast_settings.get(role, {})}

    def expected_fingerprint(chunk: dict, chunk_voice: str) -> str:
        return fragment_fingerprint(
            text=chunk["text"],
            language=chunk.get("language", ""),
            model=choice.identity,
            voice=chunk_voice,
            voice_revision=revisions.get(chunk_voice, ""),
            settings=settings_for(chunk),
        )

    typer.echo(
        f"synthesising {len(chunks)} chunks on {dev}"
        + (f" with {choice.identity}" if choice.id else "")
        + f"\nvoices: {', '.join(voices)}"
    )
    started = time.time()
    stale = 0
    retried = 0
    # Config promises this and nothing used to read it, so a fragment the
    # engine fumbled once was recorded as a permanent failure.
    retries = max(0, int(cfg.get("retries", 2)))
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rendered: list[dict] = []
    failures: list[dict] = []
    skipped = 0
    audio_seconds = 0.0
    rendered_rate = choice.native_sample_rate

    # A book is hours of work and report.json only lands at the end, so this is
    # the only view into a running render. Shape mirrors bookbinder's
    # RenderProgress; docs/schemas/render_progress_v6.json is the contract.
    progress_path = out_dir / "progress.json"
    last_progress = 0.0

    def progress(chunk_id: str = "", chunk_voice: str = "", running: bool = True) -> None:
        elapsed = time.time() - started
        done = len(rendered)
        write_progress(progress_path, {
            "schema_version": SCHEMA_VERSION,
            "slug": slug,
            "running": running,
            "pid": os.getpid(),
            "dry_run": False,
            "device": dev,
            "started_at": started_at,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_sec": round(elapsed, 3),
            "chunks_total": len(chunks),
            "chunks_done": done,
            "chunks_rendered": done - skipped,
            "chunks_skipped": skipped,
            "chunks_failed": len(failures),
            "audio_sec": round(audio_seconds, 3),
            "current_chunk_id": chunk_id,
            "current_voice": chunk_voice,
            "last_error": failures[-1]["error"] if failures else "",
            "percent": round(100 * done / len(chunks), 1) if chunks else 0.0,
            "eta_sec": (
                round((len(chunks) - done) * (elapsed / (done - skipped)), 1)
                if done > skipped and len(chunks) > done else 0.0
            ),
        })

    progress()

    for chunk in tqdm(chunks, desc="synth"):
        wav_path = out_dir / f"{chunk['id']}.wav"
        chunk_voice = voice_for(chunk)

        wanted_fingerprint = expected_fingerprint(chunk, chunk_voice)

        if wav_path.exists() and not force:
            reusable = previous.get(chunk["id"]) == wanted_fingerprint
            info = None
            if reusable:
                # A wav that cannot be read back is not audio, whatever its
                # fingerprint says. A run killed mid-write leaves exactly that.
                try:
                    info = sf.info(wav_path)
                except Exception:
                    reusable = False

            if reusable and info is not None:
                chunk["audio_path"] = str(wav_path.relative_to(root))
                chunk["duration_sec"] = round(info.duration, 3)
                chunk["voice"] = chunk_voice
                chunk["fingerprint"] = wanted_fingerprint
                audio_seconds += info.duration
                skipped += 1
                rendered.append(chunk)
                if time.time() - last_progress > PROGRESS_INTERVAL_SEC:
                    progress(chunk["id"], chunk_voice)
                    last_progress = time.time()
                continue
            stale += 1

        # Synthesis is stochastic, so a fragment that failed once may well
        # succeed on the next roll. Bounded, because a fragment that fails
        # every time is not going to start working on the twentieth attempt.
        wav = rate = None
        last_error: BaseException | None = None
        for attempt in range(1, retries + 2):
            try:
                wav = pool.speak(chunk["text"], chunk.get("language", ""),
                                 chunk_voice, settings_for(chunk))
                rate = pool.sample_rate(chunk_voice)
                if attempt > 1:
                    retried += 1
                break
            except Exception as exc:   # a single bad chunk must not kill the run
                last_error = exc
                if classify_failure(exc) != "fragment":
                    break              # retrying a broken voice changes nothing

        if wav is None or rate is None:
            assert last_error is not None
            kind = classify_failure(last_error)
            failures.append({
                "chunk_id": chunk["id"],
                "error": f"{type(last_error).__name__}: {last_error}",
                "attempts": 1 if kind != "fragment" else retries + 1,
                "kind": kind,
            })
            progress(chunk["id"], chunk_voice)
            last_progress = time.time()

            # Every fragment so far has failed the same way. That is the voice
            # or the model, and grinding through the rest of the book to say so
            # helps nobody.
            if (len(failures) >= SYSTEMIC_FAILURES and not rendered
                    and len({f["kind"] for f in failures}) == 1):
                progress(running=False)
                raise typer.BadParameter(
                    f"the first {len(failures)} fragments all failed the same "
                    f"way, so this is the {failures[-1]['kind']} rather than the "
                    f"text: {failures[-1]['error']}"
                )
            continue

        # Written at the engine's own rate. Assembly resamples once, explicitly,
        # at its own boundary; converting here would hide which rate the audio
        # was actually produced at.
        sf.write(wav_path, wav, rate)
        duration = len(wav) / rate
        rendered_rate = rate
        chunk["audio_path"] = str(wav_path.relative_to(root))
        chunk["duration_sec"] = round(duration, 3)
        chunk["voice"] = chunk_voice
        chunk["fingerprint"] = wanted_fingerprint
        # Appended as each fragment lands, so a run killed at hour six leaves
        # everything before it reusable. Written after the wav, so a
        # fingerprint never claims audio that is not on disk.
        append_fingerprint(out_dir, chunk["id"], wanted_fingerprint)
        audio_seconds += duration
        rendered.append(chunk)
        progress(chunk["id"], chunk_voice)
        last_progress = time.time()

    progress(running=False)

    manifest_path = out_dir / "rendered.jsonl"
    if wanted and manifest_path.exists():
        # Only some fragments were touched, so merge rather than replace:
        # writing just these would silently throw away the rest of the book.
        existing: list[dict] = [json.loads(line) for line
                                in manifest_path.read_text(encoding="utf-8").splitlines()
                                if line.strip()]
        replaced = {c["id"]: c for c in rendered}

        merged: list[dict] = []
        for chunk in existing:
            merged.append(replaced.pop(chunk["id"], None) or chunk)
        # Anything re-rendered that was not already in the manifest.
        merged.extend(replaced.values())
        merged.sort(key=lambda c: c["order"])
        rendered_out = merged
    else:
        rendered_out = rendered

    publish_text(manifest_path, "".join(
        json.dumps(chunk, ensure_ascii=False) + "\n" for chunk in rendered_out))

    elapsed = time.time() - started
    report = {
        "schema_version": SCHEMA_VERSION,
        "slug": slug,
        "voice": voice,
        "cast": cast,
        "model": choice.identity,
        "engine": choice.engine,
        "sample_rate": rendered_rate,
        "device": dev,
        "dry_run": False,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_sec": round(elapsed, 3),
        "chunks_total": len(chunks),
        "chunks_rendered": len(rendered) - skipped,
        "chunks_skipped": skipped,
        "audio_sec": round(audio_seconds, 3),
        "chunks_retried": retried,
        "failures": failures,
        # Computed on the bookbinder side too, but written here so the file is
        # readable on its own. test_schemas pins the two shapes together.
        "realtime_factor": round(audio_seconds / elapsed, 2) if elapsed else 0.0,
        "ok": not failures and len(rendered) == len(chunks),
    }
    report_path = publish_text(
        out_dir / "report.json", json.dumps(report, ensure_ascii=False, indent=2))

    typer.echo(
        f"rendered {len(rendered)}/{len(chunks)} chunks "
        f"({skipped} already present"
        + (f", {stale} re-rendered as stale" if stale else "")
        + (f", {retried} succeeded on a retry" if retried else "")
        + f"), {audio_seconds / 3600:.2f} h of audio "
        f"in {elapsed / 60:.1f} min ({audio_seconds / elapsed:.1f}x realtime)\n"
        f"-> {manifest_path}\n-> {report_path}"
    )
    if failures:
        typer.echo(f"{len(failures)} chunks failed; see {report_path}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()