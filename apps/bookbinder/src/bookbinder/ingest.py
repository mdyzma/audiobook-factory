"""Stage 2 - read an ebook into normalised chapters of plain text.

Runs in the `bookbinder` environment. Supports EPUB, PDF and plain text.
Output: data/book/<slug>/chapters.json, an ordered list of
{index, title, paragraphs[]} ready for chunking.
"""

from __future__ import annotations

from bookbinder.paths import project_root

import json
import re
import unicodedata
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)

# Headings that mark front/back matter we do not want narrated.
SKIP_TITLES = re.compile(
    r"^(spis tre|table of contents|contents|copyright|colophon|index|bibliograf|"
    r"przypisy|footnotes|acknowledg|o autorze|about the author)",
    re.IGNORECASE,
)

# Inline footnote markers left behind by ebook converters, e.g. "słowo[12]".
FOOTNOTE_MARKER = re.compile(r"\[\d{1,3}\]|\{\d{1,3}\}|\(\d{1,3}\)(?=\s|$)")


# Letters with no decomposed form. NFKD leaves them intact and the ASCII filter
# then deletes them outright, so "Sołaris" would slug to "soaris" and "Łódź" to
# "odz". Transliterate them first.
TRANSLITERATE = str.maketrans({
    "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
    "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D", "ı": "i",
})


def slugify(value: str) -> str:
    value = value.translate(TRANSLITERATE)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value) or "book"


def normalise(text: str, strip_footnotes: bool = True) -> str:
    """Clean text the way a narrator would want it read aloud.

    Line breaks must still be present when this runs: de-hyphenation matches
    them, and collapsing whitespace beforehand leaves "prze- rwa" where the
    source had a wrapped "przerwa". Callers pass blocks unchanged and let the
    final collapse below fold the newlines away.
    """
    import ftfy

    text = ftfy.fix_text(text)
    text = text.replace("­", "")                 # soft hyphen
    # A hyphen at a line end is a wrap, not punctuation. A real compound word
    # broken across lines is indistinguishable without a dictionary and comes
    # out joined; see TEXT-01 in docs/WORKPLAN.md.
    text = re.sub(r"(\w)-[ \t]*\r?\n[ \t]*(\w)", r"\1\2", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("…", "...").replace("—", " - ").replace("–", " - ")
    if strip_footnotes:
        text = FOOTNOTE_MARKER.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _paragraphs(raw: str, strip_footnotes: bool) -> list[str]:
    out = []
    for block in re.split(r"\n\s*\n", raw):
        cleaned = normalise(block, strip_footnotes)
        if len(cleaned) > 1:
            out.append(cleaned)
    return out


# The EPUB navigation document is a real content item, so it comes back with the
# chapters. Narrating it means reading the table of contents aloud at the end of
# the book. Its heading is often the book title, so a title-based skip misses it.
NAV_FILENAMES = re.compile(r"(^|/)(nav|toc|contents|ncx)\.x?html?$", re.IGNORECASE)


def is_navigation(item) -> bool:
    if NAV_FILENAMES.search(item.get_name() or ""):
        return True
    # EPUB 3 marks it in the manifest rather than by name.
    return "nav" in (getattr(item, "properties", None) or [])


# Block elements that hold a narratable paragraph. A blockquote or list item
# usually wraps its own <p>, so both would match and the text would be read
# twice; only the outermost match of this set is taken.
BLOCK_TAGS = ["p", "blockquote", "li"]


def spine_documents(book, ebooklib) -> list:
    """Content documents in reading order.

    The manifest that `get_items_of_type` walks is unordered in practice, so
    taking it directly can narrate a book's chapters shuffled. The spine is
    what defines reading order; fall back to the manifest only when a malformed
    EPUB has no usable spine.
    """
    ordered, seen = [], set()
    for idref, _linear in getattr(book, "spine", None) or []:
        item = book.get_item_with_id(idref)
        if item is not None and item.get_type() == ebooklib.ITEM_DOCUMENT:
            ordered.append(item)
            seen.add(idref)

    # Anything the spine forgot still holds text, so it follows in manifest
    # order rather than being dropped.
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        if getattr(item, "id", None) not in seen:
            ordered.append(item)
    return ordered


def epub_paragraphs(soup, strip_footnotes: bool) -> list[str]:
    """Narratable paragraphs from one content document, each read once."""
    paragraphs = []
    for node in soup.find_all(BLOCK_TAGS):
        # An ancestor in the same set already contributed this text.
        if node.find_parent(BLOCK_TAGS) is not None:
            continue
        cleaned = normalise(node.get_text(" "), strip_footnotes)
        if len(cleaned) > 1:
            paragraphs.append(cleaned)

    if paragraphs:
        return paragraphs

    # Some converters emit chapters as bare <div>s or loose text with no block
    # element anywhere. Dropping those silently loses whole chapters, so fall
    # back to the document's own text split on blank lines.
    body = soup.find("body") or soup
    return _paragraphs(body.get_text("\n"), strip_footnotes)


def from_epub(path: Path, strip_front_matter: bool, strip_footnotes: bool) -> tuple[dict, list[dict]]:
    import ebooklib
    from bs4 import BeautifulSoup
    from ebooklib import epub

    book = epub.read_epub(str(path))
    meta = {
        "title": (book.get_metadata("DC", "title") or [("Unknown",)])[0][0],
        "author": (book.get_metadata("DC", "creator") or [("Unknown",)])[0][0],
        "language": (book.get_metadata("DC", "language") or [("pl",)])[0][0],
    }

    chapters: list[dict] = []
    for item in spine_documents(book, ebooklib):
        if is_navigation(item):
            continue
        soup = BeautifulSoup(item.get_content(), "lxml")
        for tag in soup(["script", "style", "sup", "table", "figure"]):
            tag.decompose()

        heading = soup.find(["h1", "h2", "h3"])
        title = normalise(heading.get_text(" "), strip_footnotes) if heading else ""
        if strip_front_matter and title and SKIP_TITLES.match(title):
            continue
        # Chunking narrates the title as its own fragment, so the heading must
        # not also reach the fallback below as a paragraph.
        if heading is not None:
            heading.decompose()

        paragraphs = epub_paragraphs(soup, strip_footnotes)
        if not paragraphs:
            continue
        chapters.append({
            "index": len(chapters) + 1,
            "title": title or f"Rozdzial {len(chapters) + 1}",
            "source_ref": item.get_name(),
            "paragraphs": paragraphs,
        })
    return meta, chapters


def from_pdf(path: Path, strip_footnotes: bool) -> tuple[dict, list[dict]]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    info = reader.metadata or {}
    meta = {
        "title": (info.get("/Title") or path.stem),
        "author": (info.get("/Author") or "Unknown"),
        "language": "pl",
    }
    # PDFs carry no reliable chapter structure. One chapter per outline entry
    # if there is an outline, otherwise the whole document as one chapter.
    paragraphs: list[str] = []
    for page in reader.pages:
        paragraphs.extend(_paragraphs(page.extract_text() or "", strip_footnotes))
    chapters = [{"index": 1, "title": meta["title"], "source_ref": path.name,
                 "paragraphs": paragraphs}] if paragraphs else []
    return meta, chapters


def from_text(path: Path, strip_footnotes: bool) -> tuple[dict, list[dict]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta = {"title": path.stem, "author": "Unknown", "language": "pl"}
    # Split on markdown-ish headings if present.
    parts = re.split(r"^\s{0,3}#{1,3}\s+(.+)$", raw, flags=re.MULTILINE)
    chapters: list[dict] = []
    if len(parts) > 1:
        # parts[0] is everything before the first heading: a dedication, an
        # epigraph, an author's note, or a whole untitled opening chapter. The
        # loop below starts at the first heading, so this used to be discarded.
        preamble = _paragraphs(parts[0], strip_footnotes)
        if preamble:
            chapters.append({"index": 1, "title": meta["title"],
                             "source_ref": f"{path.name}#0",
                             "paragraphs": preamble})

        for i in range(1, len(parts), 2):
            paragraphs = _paragraphs(parts[i + 1], strip_footnotes)
            if paragraphs:
                chapters.append({"index": len(chapters) + 1,
                                 "title": normalise(parts[i], strip_footnotes),
                                 "source_ref": f"{path.name}#{i}",
                                 "paragraphs": paragraphs})
    else:
        paragraphs = _paragraphs(raw, strip_footnotes)
        chapters = [{"index": 1, "title": meta["title"], "source_ref": path.name,
                     "paragraphs": paragraphs}] if paragraphs else []
    return meta, chapters


@app.command()
def main(
    source: Path = typer.Argument(..., help="Path to .epub, .pdf or .txt"),
    slug: str = typer.Option("", help="Output name; defaults to a slug of the title"),
    language: str = typer.Option("", help="Override the language detected in metadata"),
    title: str = typer.Option("", help="Override the title. Plain text carries no "
                                       "metadata, so it otherwise comes from the filename"),
    author: str = typer.Option("", help="Override the author; otherwise 'Unknown'"),
    strip_front_matter: bool = typer.Option(True),
    strip_footnotes: bool = typer.Option(True),
) -> None:
    if not source.exists():
        raise typer.BadParameter(f"missing {source}")

    suffix = source.suffix.lower()
    if suffix == ".epub":
        meta, chapters = from_epub(source, strip_front_matter, strip_footnotes)
    elif suffix == ".pdf":
        meta, chapters = from_pdf(source, strip_footnotes)
    else:
        meta, chapters = from_text(source, strip_footnotes)

    if not chapters:
        typer.echo("no readable text found", err=True)
        raise typer.Exit(code=1)

    # These belong to ingestion rather than to a later hand-edit of book.json,
    # because chunking rebuilds book.json from chapters.json and would discard
    # anything written there afterwards.
    if title:
        meta["title"] = title
    if author:
        meta["author"] = author
    if language:
        meta["language"] = language
    meta["language"] = meta["language"].split("-")[0] if meta["language"] != "zh-cn" else "zh-cn"
    book_slug = slug or slugify(meta["title"])

    root = project_root()
    out_dir = root / "data" / "book" / book_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chapters.json").write_text(
        json.dumps({"meta": meta | {"slug": book_slug, "source_file": str(source)},
                    "chapters": chapters}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    words = sum(len(p.split()) for ch in chapters for p in ch["paragraphs"])
    typer.echo(
        f"{meta['title']} - {meta['author']}\n"
        f"{len(chapters)} chapters, {words} words -> data/book/{book_slug}/chapters.json"
    )


if __name__ == "__main__":
    app()