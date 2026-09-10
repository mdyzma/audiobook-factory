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

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from studio import data
from studio.data import UnsafeName
from studio import authoring
from studio.authoring import AuthoringError
from studio.jobs import ACTIONS, FORMATS, LANGUAGES, JobError, JobRunner

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
    return TEMPLATES.TemplateResponse(request, "book.html", {
        "book": book,
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
