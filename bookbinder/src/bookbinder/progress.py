"""Show the live state of a render.

`report.json` only exists once a render finishes. This reads `progress.json`,
which is rewritten as the render goes, so a long job can be checked from another
terminal without disturbing it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer

from bookbinder.paths import project_root
from bookbinder.manifest import RenderProgress

app = typer.Typer(add_completion=False)

# A render that has not written anything for this long is presumed dead: the
# file says running=true forever if the process was killed.
STALE_AFTER_SEC = 120


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def age_seconds(timestamp: str) -> float | None:
    try:
        then = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


@app.command()
def main(
    slug: str = typer.Argument(..., help="Book slug under data/audio/"),
    raw: bool = typer.Option(False, help="Print the JSON instead of a summary"),
) -> None:
    root = project_root()
    path = root / "data" / "audio" / slug / "progress.json"
    if not path.exists():
        typer.echo(
            f"no render in progress for '{slug}'\n"
            f"(no {path.relative_to(root)}; the last finished run is in report.json)",
            err=True,
        )
        raise typer.Exit(code=1)

    if raw:
        typer.echo(path.read_text(encoding="utf-8"))
        return

    p = RenderProgress.model_validate_json(path.read_text(encoding="utf-8"))
    age = age_seconds(p.updated_at) if p.updated_at else None
    stale = p.running and age is not None and age > STALE_AFTER_SEC

    if not p.running:
        state = "finished"
    elif stale:
        state = f"STALE (nothing written for {format_duration(age or 0)}; process {p.pid} may be dead)"
    else:
        state = "running"

    bar_width = 30
    filled = int(bar_width * p.percent / 100)
    bar = "#" * filled + "." * (bar_width - filled)

    typer.echo(f"{slug}  [{bar}] {p.percent}%   {state}")
    typer.echo(
        f"  {p.chunks_done}/{p.chunks_total} fragments"
        f"  ({p.chunks_rendered} rendered, {p.chunks_skipped} already present"
        f"{f', {p.chunks_failed} failed' if p.chunks_failed else ''})"
    )
    typer.echo(
        f"  {format_duration(p.elapsed_sec)} elapsed"
        + (f", about {format_duration(p.eta_sec)} left" if p.running and p.eta_sec else "")
        + f"  |  {format_duration(p.audio_sec)} of audio"
        + (f" on {p.device}" if p.device else "")
        + ("  |  dry run" if p.dry_run else "")
    )
    if p.current_chunk_id and p.running:
        typer.echo(f"  at {p.current_chunk_id}" + (f" in '{p.current_voice}'" if p.current_voice else ""))
    if p.last_error:
        typer.echo(f"  last error: {p.last_error}", err=True)


if __name__ == "__main__":
    app()