"""A local dashboard for audiobook-factory.

It reads what the pipeline writes, and it can start and stop pipeline stages.

Starting things is why it binds to localhost and why `jobs.py` accepts an action
name from a fixed table rather than a command. There is no authentication here,
so anything that can reach this server can run a render.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import json
from dataclasses import asdict

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from studio import data
from studio.data import UnsafeName
from studio import authoring
from studio.authoring import AuthoringError
from studio.jobs import ACTIONS, FORMATS, LANGUAGES, JobError, JobRunner
from studio import batch as batching
from studio.queue import Queue, QueueError

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

NO_DRAIN = "AF_NO_DRAIN"


def draining() -> bool:
    """Whether this server should run the queue as well as show it.

    On by default: a queue nothing drains is a list. Set `AF_NO_DRAIN=1` to run
    the dashboard as a viewer beside a `just drain` somewhere else, or wherever
    a background loop starting real jobs would be a surprise.

    Read when the server starts rather than when this module is imported, so a
    caller that sets it can still be heard.
    """
    return os.environ.get(NO_DRAIN, "").strip().lower() not in ("1", "true", "yes")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the queue while the dashboard is open.

    A queue nothing drains is a list, so this is what makes queueing a batch
    mean anything without a second terminal. It is one worker taking one step
    per tick, in a thread, because every tick touches the disk and the point is
    not to hold the event loop while it does.
    """
    task = asyncio.create_task(_drain()) if draining() else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _drain() -> None:
    from studio.worker import IDLE_SECONDS, Worker

    worker = Worker(root(), name="studio-dashboard")
    while True:
        try:
            await asyncio.to_thread(worker.tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A tick that raises must not end the loop: the queue would then
            # stop silently and a batch would sit there looking merely slow.
            pass
        await asyncio.sleep(IDLE_SECONDS)


app = FastAPI(title="audiobook-factory studio", docs_url="/api/docs",
              lifespan=lifespan)


def root() -> Path:
    return data.project_root()


def runner() -> JobRunner:
    # Built per request: everything it needs is on disk, so there is no state
    # to keep and nothing to go stale if the server restarts.
    return JobRunner(root())


def safe(name: str) -> str:
    try:
        return data.check_name(name)
    except UnsafeName:
        raise HTTPException(status_code=400, detail="invalid name")


def _duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s" if m else f"{s}s"


TEMPLATES.env.filters["duration"] = _duration


# --- pages -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return TEMPLATES.TemplateResponse(request, "dashboard.html", {
        "books": data.list_books(root()),
        "voices": data.list_voices(root()),
        "jobs": runner().jobs()[:10],
    })


@app.get("/book/{slug}", response_class=HTMLResponse)
def book_page(request: Request, slug: str):
    book = data.get_book(root(), safe(slug))
    if book is None:
        raise HTTPException(status_code=404, detail=f"no book '{slug}'")
    jobs = [j for j in runner().jobs() if j.slug == slug]
    from studio.catalog import Catalog
    from studio.database import database_path
    history = []
    if database_path(root()).exists():
        catalog = Catalog(root())
        for run in catalog.runs(slug):
            settings = json.loads(run["snapshot_json"])
            history.append({"id": run["id"], "status": run["status"], "created_at": run["created_at"],
                            "model": settings["model"].get("id") or "Unknown model",
                            "voices": ", ".join(sorted(set(settings["cast"].values()))),
                            "exports": catalog.rows("SELECT id,format FROM exports WHERE run_id=? ORDER BY created_at", (run["id"],))})
    return TEMPLATES.TemplateResponse(request, "book.html", {
        "book": book,
        "history": history,
        "overrides": authoring.get_roles(root(), slug),
        "roles": sorted({*book.cast, *authoring.read_cast(root())}),
        "jobs": jobs[:10],
        "active": next((j for j in jobs if j.running), None),
        "formats": FORMATS,
        # A full book is tens of thousands of fragments; the page shows a
        # window, and the API serves the rest.
        "chunks": data.load_chunks(root(), slug, limit=300),
    })


@app.get("/book/{slug}/quality", response_class=HTMLResponse)
def quality_page(request: Request, slug: str):
    """Flagged fragments, side by side with what the transcriber heard.

    This closes the loop on the one failure mode that is otherwise invisible:
    XTTS truncating or repeating without raising anything.
    """
    book = data.get_book(root(), safe(slug))
    if book is None:
        raise HTTPException(status_code=404, detail=f"no book '{slug}'")

    by_id = {c.id: c for c in data.load_chunks(root(), slug)}
    findings = []
    for finding in (book.qa.findings if book.qa else []):
        chunk = by_id.get(finding.chunk_id)
        findings.append({
            "chunk_id": finding.chunk_id,
            "wer": finding.wer,
            "expected": finding.expected,
            "heard": finding.heard,
            "has_audio": bool(chunk and chunk.audio_path),
            "role": chunk.role if chunk else "",
        })
    jobs = [j for j in runner().jobs() if j.slug == slug]
    return TEMPLATES.TemplateResponse(request, "quality.html", {
        "book": book,
        "findings": sorted(findings, key=lambda f: f["wer"], reverse=True),
        "active": next((j for j in jobs if j.running), None),
    })


@app.get("/voice/{name}", response_class=HTMLResponse)
def voice_page(request: Request, name: str):
    voice = data.get_voice(root(), safe(name))
    if voice is None:
        raise HTTPException(status_code=404, detail=f"no voice '{name}'")
    jobs = [j for j in runner().jobs() if j.voice == name]
    return TEMPLATES.TemplateResponse(request, "voice.html", {
        "voice": voice,
        "jobs": jobs[:10],
        "active": next((j for j in jobs if j.running), None),
        # Only books with fragments: a passage to read is the whole point, and
        # offering one that has not been split yet is offering a failure.
        "books": [b.slug for b in data.list_books(root()) if b.chunk_count],
    })


# --- json --------------------------------------------------------------------

@app.get("/api/books")
def api_books():
    return [
        {"slug": b.slug, "title": b.title, "author": b.author, "state": b.state,
         "percent": b.percent, "chapters": b.chapter_count, "chunks": b.chunk_count,
         "est_hours": b.est_hours, "cast": b.cast, "outputs": b.outputs}
        for b in data.list_books(root())
    ]


@app.get("/api/books/{slug}")
def api_book(slug: str):
    book = data.get_book(root(), safe(slug))
    if book is None:
        raise HTTPException(status_code=404, detail=f"no book '{slug}'")
    return {
        "slug": book.slug, "title": book.title, "author": book.author,
        "state": book.state, "percent": book.percent, "cast": book.cast,
        "meta": book.meta.model_dump() if book.meta else None,
        "report": book.report.model_dump() if book.report else None,
        "progress": book.progress.model_dump() if book.progress else None,
        "qa": book.qa.model_dump() if book.qa else None,
        "outputs": book.outputs,
    }


@app.get("/api/books/{slug}/progress")
def api_progress(slug: str):
    """Polled by the dashboard. Cheap, and the only endpoint that changes often."""
    book = data.get_book(root(), safe(slug))
    if book is None:
        raise HTTPException(status_code=404, detail=f"no book '{slug}'")
    if book.progress is None:
        return {"slug": slug, "state": book.state, "percent": book.percent,
                "running": False}
    payload = book.progress.model_dump()
    payload["state"] = book.state
    return payload


@app.get("/api/books/{slug}/chunks")
def api_chunks(slug: str, limit: int = 0, offset: int = 0):
    chunks = data.load_chunks(root(), safe(slug))
    window = chunks[offset: offset + limit] if limit else chunks[offset:]
    return {"total": len(chunks), "offset": offset,
            "chunks": [c.model_dump() for c in window]}


@app.get("/api/voices")
def api_voices():
    return [v.__dict__ for v in data.list_voices(root())]


# --- audio -------------------------------------------------------------------

@app.get("/audio/audition/{name}")
def audition(name: str):
    path = data.voice_dir(root(), safe(name)) / "audition.wav"
    try:
        data.contained(root(), path)
    except UnsafeName:
        raise HTTPException(status_code=404, detail="no audition clip")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no audition clip")
    return FileResponse(path, media_type="audio/wav")


@app.get("/audio/fragment/{slug}/{chunk_id}")
def fragment(slug: str, chunk_id: str):
    path = data.rendered_audio(root(), safe(slug), safe(chunk_id))
    if path is None:
        raise HTTPException(status_code=404, detail="fragment not rendered")
    return FileResponse(path, media_type="audio/wav")


@app.get("/audio/out/{slug}/{filename}")
def output(slug: str, filename: str):
    try:
        path = data.output_file(root(), safe(slug), filename)
    except UnsafeName:
        raise HTTPException(status_code=400, detail="invalid filename")
    if path is None:
        raise HTTPException(status_code=404, detail="no such output")
    media = {"m4b": "audio/mp4", "mp3": "audio/mpeg", "wav": "audio/wav"}
    return FileResponse(path, media_type=media.get(path.suffix.lstrip("."), "application/octet-stream"))


# --- jobs --------------------------------------------------------------------

def _job_json(job) -> dict:
    return {
        "id": job.id, "action": job.action, "args": job.args, "status": job.status,
        "pid": job.pid, "started_at": job.started_at, "finished_at": job.finished_at,
        "exit_code": job.exit_code, "command": " ".join(job.command),
        "running": job.running,
    }


@app.get("/api/actions")
def api_actions():
    """What this server is willing to run. Nothing else can be started."""
    return {name: spec["args"] for name, spec in ACTIONS.items()}


@app.get("/api/jobs")
def api_jobs(slug: str = "", voice: str = "", limit: int = 50):
    jobs = runner().jobs()
    if slug:
        jobs = [j for j in jobs if j.slug == safe(slug)]
    if voice:
        jobs = [j for j in jobs if j.voice == safe(voice)]
    return [_job_json(j) for j in jobs[:limit]]


@app.post("/api/jobs")
def api_start_job(payload: dict = Body(...)):
    action = str(payload.get("action") or "")
    args = {k: str(v) for k, v in (payload.get("args") or {}).items()}
    try:
        job = runner().start(action, args)
    except JobError as exc:
        # A refusal is the caller's fault, not a server fault: an unknown
        # action, a bad argument, or a book already being rendered.
        raise HTTPException(status_code=409, detail=str(exc))
    return _job_json(job)


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = runner().store.load(safe(job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    return _job_json(job)


@app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
def api_job_log(job_id: str, lines: int = 200):
    return runner().tail(safe(job_id), lines=lines)


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str):
    try:
        return _job_json(runner().cancel(safe(job_id)))
    except JobError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# --- authoring ---------------------------------------------------------------

def _authoring(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except AuthoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UnsafeName as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/library", response_class=HTMLResponse)
def library(request: Request):
    """Uploaded material, and the cast that decides who reads it."""
    jobs = runner().jobs()
    return TEMPLATES.TemplateResponse(request, "library.html", {
        "samples": authoring.list_raw(root(), "voice"),
        "active": next((j for j in jobs if j.running), None),
        "languages": sorted(LANGUAGES),
        "books": authoring.list_raw(root(), "book"),
        "cast": authoring.read_cast(root()),
        "voices": data.list_voices(root()),
        "known_books": {b.slug for b in data.list_books(root())},
    })


# --- batches -----------------------------------------------------------------

# Offered as overrides when the decoder cannot settle it on its own. Kept to
# what the decoder itself will try, so the list cannot promise a reading it
# would then refuse.
ENCODINGS = ("utf-8", "cp1250", "iso-8859-2", "cp1252", "utf-16")

def _queue() -> Queue:
    return Queue(root())


def _queued(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/batch", response_class=HTMLResponse)
def batch_page(request: Request):
    """Review a batch before committing a day of the machine to it."""
    from studio.catalog import Catalog
    recent_imports = Catalog(root()).rows("SELECT source_path,status,details_json FROM import_items ORDER BY rowid DESC LIMIT 50")
    for item in recent_imports:
        item["details"] = json.loads(item["details_json"])
    queue = _queue()
    return TEMPLATES.TemplateResponse(request, "batch.html", {
        "rows": batching.review(root()),
        "recent_imports": recent_imports,
        "items": queue.items(),
        "ready": {item.id for item in queue.ready()},
        "counts": queue.summary(),
        "voices": data.list_voices(root()),
        "formats": FORMATS,
        "languages": sorted(LANGUAGES),
        "encodings": ENCODINGS,
        "steps": batching.STEPS,
        "default_steps": batching.DEFAULT_STEPS,
    })


@app.get("/api/catalog/books")
def api_catalog_books():
    from studio.catalog import Catalog
    return Catalog(root()).books()


@app.get("/api/catalog/imports")
def api_catalog_imports():
    from studio.catalog import Catalog
    return Catalog(root()).rows("SELECT * FROM import_items ORDER BY rowid DESC")


@app.get("/api/catalog/runs")
def api_catalog_runs(slug: str = ""):
    from studio.catalog import Catalog
    return Catalog(root()).runs(slug)


@app.post("/api/catalog/runs")
def api_catalog_prepare(payload: dict = Body(...)):
    from studio.runs import prepare_run
    try:
        return prepare_run(root(), safe(str(payload.get("slug") or "")),
                           voice=str(payload.get("voice") or ""), model=str(payload.get("model") or ""),
                           request_key=str(payload.get("request_key") or ""))
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/catalog/runs/{run_id}/queue")
def api_catalog_queue(run_id: str, payload: dict = Body(default={})):
    from studio.catalog import Catalog
    try:
        run = Catalog(root()).run(run_id)
        steps = batching.chosen_steps(payload.get("steps") or ["synth", "assemble"])
        if not run["plan_id"] and "chunk" not in steps:
            steps.insert(0, "chunk")
        items = Queue(root()).add_plan(run["slug"], [(stage, {"format": str(payload.get("format") or "")}
                                      if stage == "assemble" else {}) for stage in steps], run_id=run_id)
        return {"run_id": run_id, "items": [asdict(item) for item in items]}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/catalog/exports/{export_id}")
def api_catalog_export(export_id: str):
    from studio.catalog import Catalog
    try:
        catalog = Catalog(root())
        export = catalog.one("SELECT * FROM exports WHERE id=?", (export_id,))
        path = catalog.asset_path(export["asset_id"])
        return FileResponse(path, filename=f"audiobook.{export['format']}")
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/batch/books")
def api_batch_books():
    return [vars(row) | {"ready": row.ready, "reader": row.reader}
            for row in batching.review(root())]


@app.post("/api/batch/scan")
def api_batch_scan(payload: dict = Body(...)):
    """Look at a folder without importing anything from it."""
    from bookbinder.library import report, scan

    folder = Path(str(payload.get("folder") or "")).expanduser()
    try:
        found = scan(root(), folder, recursive=bool(payload.get("recursive")))
    except (NotADirectoryError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"text": report(found), "ready": len(found.ready),
            "books": len(found.books), "unsupported": len(found.unsupported)}


@app.post("/api/batch/import")
def api_batch_import(payload: dict = Body(...)):
    """Import a folder, with one language and encoding for the whole pass."""
    from studio.imports import import_folder

    folder = Path(str(payload.get("folder") or "")).expanduser()
    try:
        result = import_folder(
            root(), folder,
            recursive=bool(payload.get("recursive")),
            language=str(payload.get("language") or ""),
            encoding=str(payload.get("encoding") or ""),
        )
    except (NotADirectoryError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@app.post("/api/batch/queue")
def api_batch_queue(payload: dict = Body(...)):
    """Queue the chosen books, and say what was left out and why."""
    slugs = [safe(str(s)) for s in (payload.get("books") or [])]
    result = batching.queue_books(
        root(), slugs,
        steps=[str(s) for s in (payload.get("steps") or [])],
        overrides={str(k): dict(v) for k, v in (payload.get("overrides") or {}).items()},
        voice=str(payload.get("voice") or ""),
        fmt=str(payload.get("format") or ""),
        batch=str(payload.get("batch") or ""),
    )
    return {"queued": result.queued, "skipped": result.skipped,
            "steps": result.total_steps}


# --- the queue ---------------------------------------------------------------

@app.get("/api/queue")
def api_queue():
    queue = _queue()
    ready = {item.id for item in queue.ready()}
    return {"items": [vars(item) | {"ready": item.id in ready}
                      for item in queue.items()],
            "counts": queue.summary()}


@app.post("/api/queue/{item_id}/{what}")
def api_queue_item(item_id: int, what: str):
    """Hold, release, drop or re-offer one step."""
    queue = _queue()
    calls = {"pause": queue.pause, "resume": queue.resume,
             "cancel": queue.cancel, "retry": queue.retry}
    if what not in calls:
        raise HTTPException(status_code=400, detail=f"cannot '{what}' a queue item")
    return vars(_queued(calls[what], item_id))


@app.post("/api/queue/book/{slug}/{what}")
def api_queue_book(slug: str, what: str):
    """The same, for every step of one book: the unit a decision is made about."""
    queue = _queue()
    name = safe(slug)
    calls = {"pause": queue.pause_book, "resume": queue.resume_book,
             "cancel": queue.cancel_book}
    if what not in calls:
        raise HTTPException(status_code=400, detail=f"cannot '{what}' a book")
    return {"slug": name, "changed": _queued(calls[what], name)}


# --- how a book says a word --------------------------------------------------

def _pronounce(fn, *args, **kwargs):
    from bookbinder.pronounce import DictionaryError

    try:
        return fn(*args, **kwargs)
    except DictionaryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _preview_payload(found) -> dict:
    return {
        "entries": found.entries,
        "counts": found.counts,
        "unused": found.unused,
        "total": found.total,
        "occurrences": [vars(o) for o in found.occurrences],
    }


@app.get("/api/books/{slug}/pronunciation")
def api_get_pronunciation(slug: str):
    """The book's dictionary, and what it currently changes."""
    from bookbinder.pronounce import preview

    name = safe(slug)
    return _preview_payload(_pronounce(preview, root(), name))


@app.post("/api/books/{slug}/pronunciation/preview")
def api_preview_pronunciation(slug: str, payload: dict = Body(...)):
    """What a proposed dictionary would do, without saving anything.

    Free, because the substitution is pure text. This is what makes deciding
    whether a fix is worth re-chunking a book cost nothing.
    """
    from bookbinder.pronounce import preview

    entries = payload.get("entries")
    if not isinstance(entries, dict):
        raise HTTPException(status_code=400, detail="expected {'entries': {...}}")
    return _preview_payload(
        _pronounce(preview, root(), safe(slug), {str(k): str(v) for k, v in entries.items()}))


@app.post("/api/books/{slug}/pronunciation")
def api_save_pronunciation(slug: str, payload: dict = Body(...)):
    """Replace the dictionary. Takes effect when the book is split again."""
    from bookbinder.pronounce import preview, save

    entries = payload.get("entries")
    if not isinstance(entries, dict):
        raise HTTPException(status_code=400, detail="expected {'entries': {...}}")
    name = safe(slug)
    clean = {str(k): str(v) for k, v in entries.items()}
    path = _pronounce(save, root(), name, clean)
    return {"path": str(path.relative_to(root())),
            **_preview_payload(_pronounce(preview, root(), name, clean))}


@app.get("/api/voices/{name}/auditions")
def api_auditions(name: str):
    """Every sample rendered for this voice, and what produced each one."""
    voice = safe(name)
    folder = root() / "data" / "voices" / voice / "auditions"
    out = []
    for record in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            out.append({"key": record.stem,
                        **json.loads(record.read_text(encoding="utf-8"))})
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r.get("created_at", ""), reverse=True)


@app.get("/audio/audition/{name}/{key}")
def audition_sample(name: str, key: str):
    """One rendered sample, for the player on the voice page."""
    path = (root() / "data" / "voices" / safe(name) / "auditions" / f"{safe(key)}.wav")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such audition")
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/upload/{kind}")
async def api_upload(kind: str, file: UploadFile = File(...)):
    if kind not in ("voice", "book"):
        raise HTTPException(status_code=400, detail="kind must be voice or book")
    upload = _authoring(authoring.store_upload, root(), kind,
                        file.filename or "", file.file)
    return {"path": str(upload.path.relative_to(root())),
            "name": upload.path.name, "bytes": upload.bytes_written}


@app.get("/api/raw/{kind}")
def api_raw(kind: str):
    if kind not in ("voice", "book"):
        raise HTTPException(status_code=400, detail="kind must be voice or book")
    return authoring.list_raw(root(), kind)


@app.get("/api/books/{slug}/roles")
def api_get_roles(slug: str):
    return _authoring(authoring.get_roles, root(), safe(slug))


@app.post("/api/books/{slug}/roles")
def api_set_role(slug: str, payload: dict = Body(...)):
    """Correct one paragraph's role. An empty role clears the correction.

    Keyed by source_ref rather than chunk id so it survives re-chunking.
    """
    roles = _authoring(
        authoring.set_role, root(), safe(slug),
        str(payload.get("source_ref") or ""), str(payload.get("role") or ""),
    )
    return {"roles": roles, "count": len(roles)}


@app.get("/api/cast")
def api_get_cast():
    return authoring.read_cast(root())


@app.post("/api/cast")
def api_set_cast(payload: dict = Body(...)):
    roles = payload.get("roles")
    if not isinstance(roles, dict):
        raise HTTPException(status_code=400, detail="expected {'roles': {...}}")
    authoring.backup_cast(root())
    path = _authoring(authoring.write_cast, root(), roles)
    return {"path": str(path.relative_to(root())), "roles": authoring.read_cast(root())}
