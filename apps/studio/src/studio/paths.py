"""Find the project root without counting directories.

Counting parents (`Path(__file__).parents[3]`) encodes how deeply this file
happens to sit, so moving a package silently changes what "root" means. The
failure is quiet: paths resolve to somewhere that exists, and the pipeline
writes to the wrong place.

Two ways to answer instead, in order:

1. ``AUDIOBOOK_FACTORY_ROOT``, which containers set explicitly. The image holds
   one environment plus mounted data, so there is nothing to search for.
2. Walking up from this file for a marker that only the root has.

This module is duplicated in each environment on purpose. narrator, transcriber
and bookbinder cannot import one another - that is the whole architecture - so
a shared helper would have to be a fourth package they all depend on, which is
more machinery than ten lines deserve.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "AUDIOBOOK_FACTORY_ROOT"

# Present at the root and nowhere else. `justfile` covers a source checkout;
# `config/pipeline.toml` covers a container, where config is mounted but the
# justfile is not copied.
MARKERS = ("justfile", "config/pipeline.toml")


class ProjectRootNotFound(RuntimeError):
    pass


def _looks_like_root(path: Path) -> bool:
    return any((path / marker).exists() for marker in MARKERS)


def project_root(start: Path | None = None) -> Path:
    override = os.environ.get(ENV_VAR)
    if override:
        root = Path(override).expanduser().resolve()
        if not root.is_dir():
            raise ProjectRootNotFound(f"{ENV_VAR}={override} is not a directory")
        return root

    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        if candidate.is_dir() and _looks_like_root(candidate):
            return candidate

    raise ProjectRootNotFound(
        f"no project root above {here}: looked for {' or '.join(MARKERS)}. "
        f"Set {ENV_VAR} if this is running somewhere unusual."
    )
