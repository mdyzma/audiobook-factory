"""Edit how a book says a word, and hear the difference before committing to it.

The dictionary itself already existed: `speech.load_dictionary` reads
`data/book/<slug>/pronunciation.yml` and chunking applies it. What was missing
is everything around it. You had to know the file was there, edit YAML by hand,
re-chunk the whole book, re-render some of it, and listen, to find out whether
a name now sounds right.

Two things make that cheap. Previewing is free: the substitution is pure text,
so every passage a proposed entry would change can be shown instantly, with no
model and no re-chunking. And a passage picked out that way is exactly what an
audition wants to say, so hearing it is the same mechanism the voice auditions
use rather than a second one.

Entries are written whole and atomically. A dictionary half-saved while a
chunker is reading it would silently narrate part of a book one way and the
rest another.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bookbinder.manifest import publish_text
from bookbinder.speech import load_dictionary, prepare

# Enough of a passage to judge a pronunciation in context without quoting half
# a chapter back at somebody.
CONTEXT_CHARS = 90

# Showing every occurrence of a common word is noise; the first few say
# whether the rule is right, and the count says how much it matters.
SAMPLE_PASSAGES = 5


class DictionaryError(ValueError):
    """A dictionary entry that will not be saved, with a reason to show."""


@dataclass
class Occurrence:
    """One place a dictionary entry changes what is said."""

    chunk_id: str
    chapter: str
    source: str
    spoken: str
    written: str

    @property
    def changed(self) -> bool:
        return self.source != self.spoken


@dataclass
class Preview:
    """What a proposed dictionary would do to a book, before anything is saved."""

    entries: dict[str, str] = field(default_factory=dict)
    occurrences: list[Occurrence] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    unused: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def dictionary_path(root: Path, slug: str) -> Path:
    return root / "data" / "book" / slug / "pronunciation.yml"


def read(root: Path, slug: str) -> dict[str, str]:
    return load_dictionary(dictionary_path(root, slug))


def check(entries: dict) -> dict[str, str]:
    """Reject what would not do what its author expects.

    A blank written form matches everywhere, and a spoken form identical to the
    written one is a rule that does nothing but will be believed to work. Both
    are mistakes a person makes once and then spends an hour looking for.
    """
    clean: dict[str, str] = {}
    for written, spoken in (entries or {}).items():
        written, spoken = str(written).strip(), str(spoken).strip()
        if not written:
            raise DictionaryError("an entry needs a word to replace")
        if not spoken:
            raise DictionaryError(f"'{written}' has nothing to say in its place")
        if written == spoken:
            raise DictionaryError(
                f"'{written}' is written the same as it is spoken, so it would "
                f"change nothing. Remove it, or say it differently")
        if "\n" in written or "\n" in spoken:
            raise DictionaryError(f"'{written}' spans a line break")
        clean[written] = spoken
    return clean


def save(root: Path, slug: str, entries: dict) -> Path:
    """Replace the book's dictionary, atomically.

    Written by hand rather than dumped, so the file stays something a person
    can read and edit in a terminal, and so the ordering is stable rather than
    whatever a serialiser feels like today.
    """
    clean = check(entries)
    lines = [
        "# How this book says particular words.",
        "# Written form on the left, spoken form on the right.",
        "# Applied when the book is split into fragments; re-run `just chunk`",
        "# after changing it, and re-render what it affects.",
        "",
    ]
    for written in sorted(clean, key=lambda w: (-len(w), w)):
        lines.append(f"{_quote(written)}: {_quote(clean[written])}")
    return publish_text(dictionary_path(root, slug), "\n".join(lines) + "\n")


def _quote(value: str) -> str:
    """YAML-safe without a dumper, which is the whole point of writing it here."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def chunks_of(root: Path, slug: str) -> list[dict]:
    import json

    path = root / "data" / "book" / slug / "chunks.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def excerpt(text: str, start: int, end: int, width: int = CONTEXT_CHARS) -> str:
    """`text` around a span, with ellipses where it was cut."""
    room = max(width - (end - start), 0) // 2
    left, right = max(start - room, 0), min(end + room, len(text))
    return ("…" if left else "") + text[left:right].strip() + ("…" if right < len(text) else "")


def preview(root: Path, slug: str, entries: dict | None = None,
            limit: int = SAMPLE_PASSAGES) -> Preview:
    """Where a dictionary changes this book, without changing anything.

    Runs against the fragments as they stand, so it says what the book says
    today rather than what it would say after a re-chunk. That is the point:
    somebody is deciding whether a re-chunk is worth it.
    """
    chosen = check(entries) if entries is not None else read(root, slug)
    result = Preview(entries=chosen, counts={written: 0 for written in chosen})
    if not chosen:
        return result

    language = ""
    for chunk in chunks_of(root, slug):
        language = language or chunk.get("language", "")
        source = chunk.get("text") or ""
        # The dictionary alone: abbreviations and symbols are somebody else's
        # rules and showing their edits here would bury the one being judged.
        done = prepare(source, chunk.get("language") or language, chosen)
        for made in done.substitutions:
            if made.kind != "dictionary":
                continue
            written = made.source
            result.counts[written] = result.counts.get(written, 0) + 1
            if len(result.occurrences) < limit * max(len(chosen), 1):
                result.occurrences.append(Occurrence(
                    chunk_id=str(chunk.get("id") or ""),
                    chapter=str(chunk.get("chapter_title") or ""),
                    source=excerpt(source, made.start, made.end),
                    spoken=excerpt(done.text, made.spoken_start, made.spoken_end),
                    written=written,
                ))
    result.unused = sorted(w for w, n in result.counts.items() if not n)
    return result


def report(found: Preview) -> str:
    """What a dictionary does to this book, as a person needs to read it."""
    if not found.entries:
        return "no pronunciation entries for this book"

    lines = []
    for written in sorted(found.entries, key=lambda w: (-found.counts.get(w, 0), w)):
        spoken, count = found.entries[written], found.counts.get(written, 0)
        lines.append(f"  {written} -> {spoken}   {count} occurrence(s)")
        for case in [o for o in found.occurrences if o.written == written][:2]:
            lines.append(f"      {case.chapter or 'unknown chapter'}")
            lines.append(f"      was:  {case.source}")
            lines.append(f"      now:  {case.spoken}")
    if found.unused:
        lines += ["", "  These match nothing in the book as it stands:"]
        lines += [f"    {w}" for w in found.unused]
        lines.append("  Check the spelling, including any accents.")
    lines += ["", f"  {found.total} change(s) across {len(found.entries)} entry/entries"]
    if found.total:
        lines.append("  Re-run `just chunk` to apply them, then re-render what changed.")
    return "\n".join(lines)


def main() -> None:
    """`just pronounce <slug> [word] [spoken]`: show, add or remove an entry."""
    import sys

    from bookbinder.paths import project_root

    args = [a for a in sys.argv[1:] if a]
    if not args:
        print("usage: pronounce <slug> [<written> [<spoken>]]", file=sys.stderr)
        raise SystemExit(2)

    root, slug = project_root(), args[0]
    entries = read(root, slug)

    try:
        if len(args) == 2:
            removed = entries.pop(args[1], None)
            if removed is None:
                print(f"'{args[1]}' is not in this book's dictionary", file=sys.stderr)
                raise SystemExit(1)
            save(root, slug, entries)
            print(f"removed {args[1]} -> {removed}")
        elif len(args) >= 3:
            entries[args[1]] = " ".join(args[2:])
            save(root, slug, entries)
            print(f"{args[1]} -> {entries[args[1]]}")
    except DictionaryError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

    print(report(preview(root, slug, entries)))


if __name__ == "__main__":
    main()
