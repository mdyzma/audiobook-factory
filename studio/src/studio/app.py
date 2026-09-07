"""A local, read-only dashboard for audiobook-factory.

Read-only on purpose: this phase proves the file-reading layer before any
process supervision exists. Nothing here starts, stops or edits anything.

It binds to localhost. That is not a default to drift away from later without
thought: the machine running this has the pipeline on it, and a later phase will
add the ability to start jobs.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from studio import data
from studio.data import UnsafeName

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="audiobook-factory studio", docs_url="/api/docs")


def root() -> Path:
    return data.project_root()


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
    })


@app.get("/book/{slug}", response_class=HTMLResponse)
def book_page(request: Request, slug: str):
    book = data.get_book(root(), safe(slug))
    if book is None:
        raise HTTPException(status_code=404, detail=f"no book '{slug}'")
    return TEMPLATES.TemplateResponse(request, "book.html", {
        "book": book,
        # A full book is tens of thousands of fragments; the page shows a
        # window, and the API serves the rest.
        "chunks": data.load_chunks(root(), slug, limit=300),
    })


@app.get("/voice/{name}", response_class=HTMLResponse)
def voice_page(request: Request, name: str):
    voice = data.get_voice(root(), safe(name))
    if voice is None:
        raise HTTPException(status_code=404, detail=f"no voice '{name}'")
    return TEMPLATES.TemplateResponse(request, "voice.html", {"voice": voice})


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
