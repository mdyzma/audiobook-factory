"""The resolved backend, as read from `book.json`.

narrator cannot import bookbinder: they are separate environments with
incompatible numpy, and the only thing crossing that boundary is files. So the
shape of `BookMeta.model` is mirrored here by hand, the way the render report
already is. `docs/schemas/book_meta_v4.json` is the contract, and
`test_schemas.py` on the bookbinder side pins the two together.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelChoice:
    """Which backend narrates this book, decided when it was chunked.

    Read rather than recomputed, so that changing a default in
    `config/models.toml` cannot change a book that is already part-rendered,
    and so the fragments are spoken by the model their sizes were packed for.
    """

    id: str = ""
    engine: str = ""
    environment: str = ""
    checkpoint: str = ""
    revision: str = ""
    native_sample_rate: int = 24000
    char_limit: int = 0
    settings: dict[str, float] = field(default_factory=dict)
    unsupported: list[str] = field(default_factory=list)
    source: str = ""

    @classmethod
    def from_book(cls, book: dict) -> "ModelChoice":
        """Read the model block, tolerating books chunked before it existed."""
        raw = book.get("model") or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    @property
    def identity(self) -> str:
        """What has to match for rendered audio to be reusable."""
        return f"{self.id}@{self.revision}" if self.revision else self.id
