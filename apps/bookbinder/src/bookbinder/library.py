"""Find the books in a folder, and work out which are already here.

One file is one book. Scanning is explicit rather than continuous: a folder is
looked at when asked, and what it finds is reviewed before anything is queued.

The hard part is not finding files, it is knowing what each one *is*. The same
book can arrive twice under two names, an edited copy can arrive under the
name already used, and two unrelated books can both be called "Sołaris".
Getting any of those wrong overwrites work that took hours to produce, so the
answer comes from the bytes rather than from the filename.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# What counts as a book here. PDF is out: the assessment scoped folder work to
# TXT and EPUB, and `just ingest` still takes a PDF one file at a time.
BOOK_SUFFIXES = (".txt", ".epub")

# Enough of a hash to disambiguate two books with the same title without making
# the slug unreadable. Collisions at this length need billions of books.
SLUG_DISCRIMINATOR = 6

# Read in blocks: a large EPUB should not be held in memory to be hashed.
HASH_BLOCK = 1 << 20


@dataclass
class Candidate:
    """One file the scan found, and what is already known about it."""

    path: Path
    sha256: str = ""
    bytes: int = 0
    slug: str = ""
    status: str = "new"          # new | unchanged | revised | duplicate | unsupported
    duplicate_of: str = ""
    note: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def ready(self) -> bool:
        """Whether importing this would add or advance a book."""
        return self.status in ("new", "revised")


@dataclass
class Scan:
    """Everything one look at a folder turned up."""

    folder: Path
    books: list[Candidate] = field(default_factory=list)
    unsupported: list[Candidate] = field(default_factory=list)

    @property
    def ready(self) -> list[Candidate]:
        return [c for c in self.books if c.ready]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(HASH_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class Imported:
    """A book already here, as the scan needs to see it."""

    slug: str
    sha256: str = ""
    source: str = ""            # the path it was imported from


def imported_books(root: Path) -> dict[str, Imported]:
    """Every book already here, keyed by slug.

    Read from the manifests rather than from a separate index, so it cannot
    drift from what is actually on disk.
    """
    book_root = root / "data" / "book"
    if not book_root.is_dir():
        return {}

    known: dict[str, Imported] = {}
    for meta_path in sorted(book_root.glob("*/chapters.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")).get("meta") or {}
        except (OSError, ValueError):
            continue
        slug = meta_path.parent.name
        known[slug] = Imported(
            slug=slug,
            sha256=str(meta.get("source_sha256") or ""),
            source=str(meta.get("original_source") or ""),
        )
    return known


def unique_slug(base: str, taken: set[str]) -> str:
    """A name that will not overwrite a different book already using it.

    Two unrelated books both called "Sołaris" would otherwise land on one slug,
    and the second import would replace the first along with however many hours
    of audio had been rendered for it.
    """
    if base not in taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in taken:
        suffix += 1
    return f"{base}-{suffix}"


def classify(
    path: Path,
    sha: str,
    base_slug: str,
    known: dict[str, Imported],
    seen: dict[str, str],
    taken: set[str],
) -> Candidate:
    """What this file is, relative to what is here and what this scan has seen.

    The order matters. Identical bytes settle it outright, whatever the file is
    called. Failing that, the path it was imported from is what distinguishes a
    corrected copy of a book already here from a different book that happens to
    share a filename: the first should replace, the second must not.
    """
    result = Candidate(path=path, sha256=sha, slug=base_slug)

    # Checked before the within-scan duplicate below: "this book is already
    # here" is both stronger and more useful than "this is a copy of the file
    # two lines up", and every copy of it deserves the same answer regardless
    # of which one the scan happened to reach first.
    same_bytes = next((b for b in known.values() if b.sha256 and b.sha256 == sha), None)
    if same_bytes is not None:
        result.status = "unchanged"
        result.slug = same_bytes.slug
        result.note = f"already imported as '{same_bytes.slug}'"
        return result

    if sha in seen:
        result.status = "duplicate"
        result.duplicate_of = seen[sha]
        result.note = f"byte-for-byte the same file as {seen[sha]}"
        return result

    from_here = next((b for b in known.values()
                      if b.source and Path(b.source) == path), None)
    if from_here is not None:
        result.status = "revised"
        result.slug = from_here.slug
        result.note = (f"'{from_here.slug}' was imported from this same file and the "
                       f"bytes have changed; importing replaces its text and "
                       f"invalidates its audio")
        return result

    result.status = "new"
    result.slug = unique_slug(base_slug, taken)
    if result.slug != base_slug:
        result.note = f"'{base_slug}' is a different book already here"
    return result


def scan(
    root: Path,
    folder: Path,
    recursive: bool = False,
    slug_for: "Callable[[Path], str] | None" = None,
) -> Scan:
    """Look at `folder` and say what is in it.

    Non-recursive by default. A books folder with an `originals/` subdirectory
    beside it should not silently import both copies of everything, so going
    deeper is asked for rather than assumed.

    `slug_for` derives a proposed name from a path; the default uses the
    filename, because reading each file for its title would mean decoding every
    one before the user has chosen anything.
    """
    from bookbinder.ingest import slugify

    if not folder.is_dir():
        raise NotADirectoryError(f"no folder at {folder}")

    namer = slug_for or (lambda p: slugify(p.stem))
    found = Scan(folder=folder)
    known = imported_books(root)
    taken = set(known)
    seen: dict[str, str] = {}

    paths = sorted(folder.rglob("*") if recursive else folder.glob("*"))
    for path in paths:
        if not path.is_file() or path.name.startswith("."):
            continue

        suffix = path.suffix.lower()
        if suffix not in BOOK_SUFFIXES:
            # Named rather than skipped: a folder of MOBI files that produced
            # no output and no message would look like a broken scan.
            found.unsupported.append(Candidate(
                path=path, status="unsupported",
                note=f"{suffix or 'no extension'} is not read here; "
                     f"expected {' or '.join(BOOK_SUFFIXES)}"))
            continue

        sha = file_sha256(path)
        candidate = classify(path, sha, namer(path), known, seen, taken)
        candidate.bytes = path.stat().st_size
        found.books.append(candidate)

        seen.setdefault(sha, path.name)
        # Reserve the name for the rest of this scan, so two new books sharing
        # a title in one folder do not both claim it.
        taken.add(candidate.slug)

    return found


def report(found: Scan) -> str:
    """The scan as a person needs to read it before importing anything."""
    lines = [f"{found.folder}", ""]
    if not found.books and not found.unsupported:
        return "\n".join(lines + ["  nothing here to read"])

    width = max((len(c.name) for c in found.books), default=0)
    for candidate in found.books:
        size = (f"{candidate.bytes / 1024 / 1024:.1f} MB"
                if candidate.bytes >= 1024 * 1024
                else f"{candidate.bytes / 1024:.1f} KB")
        lines.append(f"  {candidate.status:10} {candidate.name:{width}}  {size:>9}"
                     f"  -> {candidate.slug}")
        if candidate.note:
            lines.append(f"  {'':10} {candidate.note}")

    for candidate in found.unsupported:
        lines.append(f"  {'skipped':10} {candidate.name}")
        lines.append(f"  {'':10} {candidate.note}")

    ready = found.ready
    lines += ["", f"  {len(ready)} to import, {len(found.books) - len(ready)} already "
                  f"here or duplicated, {len(found.unsupported)} skipped"]
    return "\n".join(lines)


def main() -> None:
    """`just scan <folder>`."""
    import sys

    from bookbinder.paths import project_root

    args = [a for a in sys.argv[1:] if a]
    recursive = "--recursive" in args
    folders = [a for a in args if not a.startswith("-")]
    if not folders:
        print("usage: scan <folder> [--recursive]", file=sys.stderr)
        raise SystemExit(2)

    print(report(scan(project_root(), Path(folders[0]), recursive=recursive)))


if __name__ == "__main__":
    main()
