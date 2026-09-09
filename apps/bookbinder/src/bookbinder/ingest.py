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
from dataclasses import dataclass, field
from pathlib import Path

import typer

from bookbinder.decode import (
    DECODER_VERSION,
    decode_file,
    sha256_bytes,
    text_quality,
)

app = typer.Typer(add_completion=False)


def stage_source(root: Path, slug: str, source: Path) -> Path:
    """Copy the input under `data/sources/` and read from that copy afterwards.

    A book that has been imported must not change because someone edited,
    moved or replaced the file it came from. Everything downstream refers to
    this copy, and its hash is what says whether a re-scan found the same book
    or a new revision of it.
    """
    import shutil

    target_dir = root / "data" / "sources" / slug
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if target.exists() and target.resolve() == source.resolve():
        return target        # already the staged copy, being re-ingested

    staged = target.with_name(target.name + ".part")
    shutil.copy2(source, staged)
    staged.replace(target)
    return target


@dataclass
class Extraction:
    """Everything one source file yielded, including how it was read.

    The encoding and review fields travel with the text because a book that
    could not be decoded confidently has to be visible as such downstream,
    rather than arriving as ordinary-looking chapters.
    """

    meta: dict
    chapters: list[dict]
    encoding: dict = field(default_factory=dict)
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)

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


# Below this, a document's decoded text looks like it was read through the
# wrong table: replacement characters, C1 controls, or letters from a script
# the book has no business containing. Calibrated against the same fixtures as
# `decode.text_quality`, where correct readings score close to 1.
SUSPICIOUS_QUALITY = 0.5


def from_epub(
    path: Path,
    strip_front_matter: bool,
    strip_footnotes: bool,
    encoding: str = "",
) -> Extraction:
    """Read an EPUB as the structured container it is.

    Each content document declares its own encoding, so they are decoded
    individually by the XHTML parser rather than by running one text decoder
    over the ZIP. An `--encoding` override is therefore not applicable here and
    is reported rather than silently ignored.
    """
    import ebooklib
    from bs4 import BeautifulSoup
    from ebooklib import epub

    book = epub.read_epub(str(path))
    meta = {
        "title": (book.get_metadata("DC", "title") or [("Unknown",)])[0][0],
        "author": (book.get_metadata("DC", "creator") or [("Unknown",)])[0][0],
        "language": (book.get_metadata("DC", "language") or [("",)])[0][0],
    }

    warnings: list[str] = []
    if encoding:
        warnings.append(
            f"--encoding {encoding} was ignored: an EPUB declares an encoding "
            f"per document and those declarations were used instead"
        )

    seen_encodings: list[str] = []
    suspicious: list[str] = []
    chapters: list[dict] = []
    for item in spine_documents(book, ebooklib):
        if is_navigation(item):
            continue
        soup = BeautifulSoup(item.get_content(), "lxml")
        declared = (getattr(soup, "original_encoding", None) or "").lower()
        if declared and declared not in seen_encodings:
            seen_encodings.append(declared)

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

        # A wrong or missing declaration inside an otherwise valid EPUB shows
        # up here and nowhere else: the ZIP is intact and the XML parses.
        body = " ".join(paragraphs)
        if len(body) > 200 and text_quality(body) < SUSPICIOUS_QUALITY:
            suspicious.append(item.get_name())

        chapters.append({
            "index": len(chapters) + 1,
            "title": title or f"Rozdzial {len(chapters) + 1}",
            "source_ref": item.get_name(),
            "paragraphs": paragraphs,
        })

    review_reasons: list[str] = []
    if suspicious:
        review_reasons.append(
            f"{len(suspicious)} document(s) decoded to text that does not read as "
            f"Polish or English, so their declared encoding may be wrong: "
            f"{', '.join(suspicious[:5])}"
        )

    return Extraction(
        meta=meta, chapters=chapters,
        encoding={
            "encoding": ", ".join(seen_encodings) or "declared-per-document",
            "method": "epub", "score": 0.0, "equivalent": [],
            "decoder_version": DECODER_VERSION, "warnings": warnings,
        },
        needs_review=bool(suspicious),
        review_reasons=review_reasons,
    )


def from_pdf(path: Path, strip_footnotes: bool) -> Extraction:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    info = reader.metadata or {}
    meta = {
        "title": (info.get("/Title") or path.stem),
        "author": (info.get("/Author") or "Unknown"),
        "language": "",
    }
    # PDFs carry no reliable chapter structure. One chapter per outline entry
    # if there is an outline, otherwise the whole document as one chapter.
    paragraphs: list[str] = []
    for page in reader.pages:
        paragraphs.extend(_paragraphs(page.extract_text() or "", strip_footnotes))
    chapters = [{"index": 1, "title": meta["title"], "source_ref": path.name,
                 "paragraphs": paragraphs}] if paragraphs else []
    return Extraction(
        meta=meta, chapters=chapters,
        encoding={"encoding": "pdf-extracted", "method": "pdf", "score": 0.0,
                  "equivalent": [], "decoder_version": DECODER_VERSION,
                  "warnings": []},
    )


def from_text(path: Path, strip_footnotes: bool, encoding: str = "") -> Extraction:
    """Read a plain-text book, establishing its encoding rather than assuming.

    The previous `errors="replace"` turned every byte it could not read into
    U+FFFD, so a Windows-1250 Polish novel lost its diacritics before anything
    downstream could object.
    """
    decoded = decode_file(path, encoding)
    raw = decoded.text
    meta = {"title": path.stem, "author": "Unknown", "language": ""}
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
    return Extraction(
        meta=meta, chapters=chapters,
        encoding={
            "encoding": decoded.encoding, "method": decoded.method,
            "score": decoded.score, "equivalent": decoded.equivalent,
            "decoder_version": DECODER_VERSION, "warnings": decoded.warnings,
        },
        needs_review=decoded.needs_review,
        review_reasons=list(decoded.review_reasons),
    )


@app.command()
def main(
    source: Path = typer.Argument(..., help="Path to .epub, .pdf or .txt"),
    slug: str = typer.Option("", help="Output name; defaults to a slug of the title"),
    language: str = typer.Option(
        "", help="Set the book language instead of detecting it, e.g. pl or en"),
    encoding: str = typer.Option(
        "", help="Read plain text as this encoding instead of establishing it, "
                 "e.g. cp1250. An EPUB declares its own and ignores this."),
    title: str = typer.Option("", help="Override the title. Plain text carries no "
                                       "metadata, so it otherwise comes from the filename"),
    author: str = typer.Option("", help="Override the author; otherwise 'Unknown'"),
    strip_front_matter: bool = typer.Option(True),
    strip_footnotes: bool = typer.Option(True),
    review: bool = typer.Option(
        False, "--review", help="Report the decoding and language evidence and stop, "
                                "without writing anything"),
) -> None:
    from bookbinder.decode import UndecodableSource
    from bookbinder import language as lang

    if not source.exists():
        raise typer.BadParameter(f"missing {source}")

    suffix = source.suffix.lower()
    try:
        if suffix == ".epub":
            found = from_epub(source, strip_front_matter, strip_footnotes, encoding)
        elif suffix == ".pdf":
            found = from_pdf(source, strip_footnotes)
        else:
            found = from_text(source, strip_footnotes, encoding)
    except UndecodableSource as exc:
        typer.echo(f"cannot read {source.name}: {exc}", err=True)
        raise typer.Exit(code=1)

    meta, chapters = found.meta, found.chapters
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

    # The language comes from the book's own words, cross-checked against
    # whatever the container claimed. Plain text used to default to `pl` and an
    # EPUB used to believe its metadata; neither survives a mixed folder.
    decision = lang.decide(chapters, metadata_language=meta.get("language", ""),
                           override=language)
    meta["language"] = decision.language or meta.get("language") or ""

    review_reasons = list(found.review_reasons)
    review_reasons += decision.review_reasons
    needs_review = found.needs_review or decision.needs_review

    for warning in found.encoding.get("warnings", []) + decision.warnings:
        typer.echo(f"warning: {warning}", err=True)

    if review or needs_review:
        typer.echo(_review_report(source, found, decision, chapters))
    if needs_review and not review:
        typer.echo(
            "not written: resolve the above with --encoding or --language, "
            "or re-run with --review to inspect it.", err=True)
        raise typer.Exit(code=2)
    if review:
        return

    book_slug = slug or slugify(meta["title"])
    root = project_root()
    staged = stage_source(root, book_slug, source)

    out_dir = root / "data" / "book" / book_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chapters.json").write_text(
        json.dumps({
            "meta": meta | {
                "slug": book_slug,
                "source_file": str(staged.relative_to(root)),
                "original_source": str(source),
                "source_sha256": sha256_bytes(staged.read_bytes()),
                "encoding": found.encoding,
                "language_decision": {
                    "method": decision.method,
                    "confidence": decision.confidence,
                    "detector": decision.detector,
                    "metadata_language": decision.metadata_language,
                    "coverage_chars": decision.coverage_chars,
                    "samples": [vars(s) for s in decision.samples],
                    "warnings": decision.warnings,
                },
                "needs_review": needs_review,
                "review_reasons": review_reasons,
            },
            "chapters": chapters,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    words = sum(len(p.split()) for ch in chapters for p in ch["paragraphs"])
    typer.echo(
        f"{meta['title']} - {meta['author']}\n"
        f"{len(chapters)} chapters, {words} words, {meta['language']} "
        f"({decision.method}), read as {found.encoding['encoding']}\n"
        f"-> data/book/{book_slug}/chapters.json"
    )


def _review_report(source: Path, found: Extraction, decision, chapters: list[dict]) -> str:
    """What was decided and on what evidence, for a person to agree or not."""
    lines = [f"{source.name}", ""]
    enc = found.encoding
    lines.append(f"  encoding   {enc.get('encoding')} via {enc.get('method')}"
                 + (f", score {enc.get('score')}" if enc.get("score") else ""))
    if enc.get("equivalent"):
        lines.append(f"             identical under {', '.join(enc['equivalent'])}")
    lines.append(f"  language   {decision.language or 'undecided'} via {decision.method}"
                 f", confidence {decision.confidence}")
    for sample in decision.samples:
        lines.append(f"             {sample.where}: {sample.language} "
                     f"{sample.confidence} ({sample.chars} chars)")
    if decision.metadata_language:
        lines.append(f"  metadata   claims {decision.metadata_language}")
    lines.append(f"  text       {len(chapters)} chapters, "
                 f"{sum(len(p) for c in chapters for p in c['paragraphs'])} characters")

    first = next((p for c in chapters for p in c["paragraphs"]), "")
    if first:
        lines += ["", f"  first paragraph: {first[:200]}"]
    reasons = list(found.review_reasons) + list(decision.review_reasons)
    if reasons:
        lines += ["", "  needs review:"] + [f"    - {r}" for r in reasons]
    return "\n".join(lines)


if __name__ == "__main__":
    app()