"""What stage 4 needs from a synthesis backend, and nothing more.

The pipeline used to be XTTS all the way down: its model name compiled into
the engine, its 24 kHz assumed at every boundary, and its per-language
character limits treated as a property of the language. Adding a second engine
meant a rewrite rather than a configuration entry.

This is the seam. A backend answers two questions, which text to speak and at
what rate the result comes back, and everything else about it lives in
`config/models.toml` on the bookbinder side. Adding an engine whose
dependencies conflict with XTTS means a new environment under `apps/` with its
own module here; nothing in this package may assume it is the only one.

The `ModelChoice` block in `book.json` is the request. narrator cannot import
bookbinder, so the shape is mirrored in `choice.py`, and
`docs/schemas/book_meta_v4.json` is the contract between them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from numpy import ndarray

    from narrator.choice import ModelChoice


class Backend(Protocol):
    """One synthesis engine, as stage 4 sees it."""

    engine: str

    def sample_rate(self, voice: str) -> int:
        """The rate this backend's audio comes back at, for this voice.

        Asked per voice because a fine-tuned checkpoint may differ from the
        stock one. Assembly resamples explicitly at its own boundary; nothing
        here silently converts, because a rate mistake changes pitch and every
        chapter timestamp with it.
        """
        ...

    def speak(self, text: str, language: str, voice: str, settings: dict) -> "ndarray":
        """Render one fragment. Raises on failure; stage 4 records and moves on."""
        ...


class UnsupportedEngine(RuntimeError):
    """This environment cannot load the backend the book asks for."""


def backend_for(choice: "ModelChoice", root: Path, device: str) -> Backend:
    """The backend that will narrate this book, or a refusal saying why.

    A book bound to an engine this environment does not implement is an error
    rather than a fallback. Silently narrating it with whatever is installed is
    how a book ends up in the wrong voice with a report that says it went fine.
    """
    if choice.engine in ("", "xtts"):
        from narrator.backends.xtts import XttsBackend

        return XttsBackend(root, device)

    raise UnsupportedEngine(
        f"'{choice.id or choice.engine}' runs on engine '{choice.engine}', which "
        f"the narrator environment does not implement. It is configured to run "
        f"in '{choice.environment or 'an unspecified environment'}'."
    )
