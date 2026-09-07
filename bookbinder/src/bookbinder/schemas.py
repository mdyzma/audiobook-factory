"""Export the manifest models to docs/schemas/ as JSON Schema.

The exported files are the reviewable form of the contract between the three
environments. narrator and transcriber cannot import these models, so they
write matching JSON by hand; the schemas are what makes a divergence visible.

CI runs `--check`, so a model change that nobody meant to make fails there
rather than at the next render.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from bookbinder.paths import project_root
from bookbinder.manifest import json_schemas

app = typer.Typer(add_completion=False)


def schema_dir(root: Path) -> Path:
    return root / "docs" / "schemas"


def render(schema: dict) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


@app.command()
def main(
    check: bool = typer.Option(
        False, help="Fail if the files on disk differ, instead of rewriting them"
    ),
) -> None:
    root = project_root()
    out_dir = schema_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    stale: list[str] = []
    for name, schema in json_schemas().items():
        path = out_dir / f"{name}.json"
        rendered = render(schema)
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                stale.append(name)
        else:
            path.write_text(rendered, encoding="utf-8")

    if check:
        if stale:
            typer.echo(
                "schemas are out of date: " + ", ".join(stale) + "\nrun: just schemas",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(f"{len(json_schemas())} schemas up to date")
    else:
        typer.echo(f"wrote {len(json_schemas())} schemas -> {out_dir}")


if __name__ == "__main__":
    app()