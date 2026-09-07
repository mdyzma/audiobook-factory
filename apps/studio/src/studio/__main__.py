"""Launch the dashboard.

Binds to 127.0.0.1 by default and warns loudly if told to do otherwise: this
process reads the whole data directory, and later phases will let it start jobs.
"""

from __future__ import annotations

import typer

app = typer.Typer(add_completion=False)


@app.command()
def main(
    host: str = typer.Option("127.0.0.1", help="Bind address. Leave this alone."),
    port: int = typer.Option(8765),
    reload: bool = typer.Option(False, help="Reload on code changes, for development"),
) -> None:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho(
            f"warning: binding to {host} exposes this dashboard to your network.\n"
            f"         It serves every file under data/ and will run jobs in a\n"
            f"         later version. There is no authentication.",
            fg="yellow", err=True,
        )

    typer.echo(f"studio on http://{host}:{port}")
    uvicorn.run("studio.app:app", host=host, port=port, reload=reload, log_level="warning")


if __name__ == "__main__":
    app()
