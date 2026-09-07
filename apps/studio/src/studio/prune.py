"""Remove finished job records and their logs.

Nothing expires on its own. A render's log grows with every progress line, so
on a long book `data/.studio/jobs/` is worth clearing occasionally.
"""

from __future__ import annotations

import typer

from studio.jobs import JobRunner
from studio.paths import project_root

app = typer.Typer(add_completion=False)


def human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"


@app.command()
def main(
    keep: int = typer.Option(20, help="Finished jobs to keep, newest first"),
    all: bool = typer.Option(False, "--all", help="Remove every finished job"),
) -> None:
    result = JobRunner(project_root()).prune(keep=keep, remove_all=all)

    typer.echo(
        f"removed {result['removed']} finished job(s), "
        f"{human(result['bytes_freed'])} freed"
    )
    if result["kept"]:
        typer.echo(f"kept {result['kept']} most recent")
    if result["running"]:
        typer.echo(f"left {result['running']} still running alone")
    if result["stale_locks_cleared"]:
        typer.echo(f"cleared {result['stale_locks_cleared']} stale lock(s)")


if __name__ == "__main__":
    app()
