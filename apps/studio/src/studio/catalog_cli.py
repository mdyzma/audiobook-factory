"""Catalog and run commands shared by terminal workflows and Studio."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import typer

from studio.catalog import Catalog, encode, inside
from studio.database import Database, StorageError, migrate_queue
from studio.paths import project_root
from studio.runs import execute_stage, prepare_run

app = typer.Typer(add_completion=False)


@app.command("migrate")
def migrate() -> None:
    migrate_queue(project_root())
    typer.echo(encode(Catalog(project_root()).reconcile()))


@app.command("reconcile")
def reconcile() -> None:
    typer.echo(encode(Catalog(project_root()).reconcile()))


@app.command("forget")
def forget(run: str, force: bool = typer.Option(False, help="Drop it even if the queue still names it")) -> None:
    """Remove a run, its directory, and anything only it was keeping alive."""
    from studio.reclaim import forget_run
    try:
        typer.echo(encode(forget_run(project_root(), run, force=force)))
    except StorageError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


@app.command("forget-book")
def forget_book_cmd(slug: str, force: bool = typer.Option(False, help="Drop it even if the queue still names a run")) -> None:
    """Remove a book, every run of it, and the stored bytes only it held."""
    from studio.reclaim import forget_book
    try:
        typer.echo(encode(forget_book(project_root(), slug, force=force)))
    except StorageError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


@app.command("collapse")
def collapse() -> None:
    """Fold run files into the assets they duplicate, for runs made before linking."""
    from studio.reclaim import collapse_runs
    typer.echo(encode(collapse_runs(project_root())))


@app.command("sweep")
def sweep() -> None:
    """Delete stored bytes nothing points at any more."""
    from studio.reclaim import sweep_assets
    typer.echo(encode(sweep_assets(project_root())))


@app.command("check")
def check() -> None:
    result = Database(project_root()).check()
    typer.echo(encode(result))
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("books")
def books() -> None:
    typer.echo(encode(Catalog(project_root()).books()))


@app.command("runs")
def runs(slug: str = "") -> None:
    typer.echo(encode(Catalog(project_root()).runs(slug)))


@app.command("import-file")
def import_file(source: Path, slug: str = "", language: str = "", title: str = "",
                author: str = "", encoding: str = "") -> None:
    from studio.imports import import_sources
    result = import_sources(project_root(), [source.expanduser().resolve()], slug=slug,
                            language=language, title=title, author=author, encoding=encoding)
    typer.echo(result["text"])
    if result["paused"]:
        raise typer.Exit(1)


@app.command("import-folder")
def import_folder(folder: Path, language: str = "", encoding: str = "", recursive: bool = False) -> None:
    from studio.imports import import_folder as ingest
    result = ingest(project_root(), folder.expanduser().resolve(), recursive=recursive,
                    language=language, encoding=encoding)
    typer.echo(result["text"])


@app.command("prepare")
def prepare(slug: str, voice: str = "", model: str = "", request_key: str = "") -> None:
    run = prepare_run(project_root(), slug, voice=voice, model=model, request_key=request_key)
    typer.echo(run["id"])


@app.command("stage")
def stage(run_id: str, action: str, fmt: str = "", device: str = "auto",
          only: str = "", sample: str = "0", limit: int = 0, strict: bool = False) -> None:
    raise typer.Exit(execute_stage(project_root(), run_id, action, fmt=fmt, device=device, only=only, sample=sample, limit=limit, strict=strict))


@app.command("process")
def process(slug: str, action: str, voice: str = "", model: str = "", fmt: str = "",
            device: str = "auto", only: str = "", sample: str = "0", limit: int = 0,
            strict: bool = False, single_voice: bool = False) -> None:
    root = project_root()
    catalog = Catalog(root)
    book = catalog.register_book(slug)
    candidates = catalog.runs(slug)
    run = None
    if action != "chunk":
        for candidate in candidates:
            if candidate["status"] == "legacy":
                continue
            snapshot = json.loads(candidate["snapshot_json"])
            if candidate["text_version_id"] == book["text_version_id"] and (
                not voice or snapshot["voice"] == voice
            ) and (not model or snapshot["model"]["id"] == model):
                run = candidate
                break
    if run is None:
        if action == "chunk" and not model:
            from bookbinder.models import load_registry
            language = catalog.one("SELECT language FROM text_versions WHERE id=?", (book["text_version_id"],))["language"]
            model = load_registry(root).resolve(language).id
        run = prepare_run(root, slug, voice=voice, model=model, reuse=True, single_voice=single_voice)
    typer.echo(f"audiobook run {run['id']}")
    code = execute_stage(root, run["id"], action, fmt=fmt, device=device, only=only, sample=sample, limit=limit, strict=strict)
    if code == 0 and action == "chunk":
        # Current authoring files remain convenient exports of the selected plan.
        source = inside(root, run["root_key"]) / "data/book" / slug
        destination = root / "data/book" / slug
        for name in ("book.json", "chunks.jsonl"):
            shutil.copy2(source / name, destination / name)
        (destination / ".needs-chunking").unlink(missing_ok=True)
        catalog.register_book(slug)
    raise typer.Exit(code)


@app.command("backup")
def backup(destination: Path) -> None:
    from studio.storage_backup import backup as make_backup
    typer.echo(encode(make_backup(project_root(), destination.expanduser().resolve())))


@app.command("voice")
def voice_record(name: str) -> None:
    typer.echo(Catalog(project_root()).register_voice(name))


@app.command("output")
def output(slug: str, fmt: str = "m4b") -> None:
    from studio.data import artifact_root
    if fmt not in ("m4b", "mp3", "wav"):
        raise StorageError("choose m4b, mp3 or wav")
    from studio.data import check_name
    check_name(slug)
    path = artifact_root(project_root(), slug) / "data/out" / f"{slug}.{fmt}"
    if not path.is_file():
        raise StorageError("this audiobook has no finished output in that format")
    typer.echo(str(path))


@app.command("progress")
def progress(slug: str, report: bool = False, watch: bool = False, interval: float = 5) -> None:
    import time
    from studio.data import artifact_root
    from studio.catalog import read_json
    while True:
        base = artifact_root(project_root(), slug)
        payload = read_json(base / "data/audio" / slug / ("report.json" if report else "progress.json"), {})
        typer.echo(encode(payload))
        if not watch or not payload.get("running"):
            return
        time.sleep(max(0.1, interval))


@app.command("restore")
def restore(source: Path, destination: Path) -> None:
    from studio.storage_backup import restore as restore_backup
    typer.echo(encode(restore_backup(source.expanduser().resolve(), destination.expanduser().resolve())))


def main() -> None:
    try:
        app()
    except StorageError as exc:
        typer.echo(str(exc), err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
