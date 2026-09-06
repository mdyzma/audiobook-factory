"""Stage 3 - split chapters into TTS-sized fragments carrying metadata.

XTTS-v2 truncates text past a per-language character limit without warning,
so chunks are packed up to just under that limit on sentence boundaries.
Each chunk keeps its chapter, order and trailing-pause metadata, which is
what lets stage 5 rebuild chapter marks and natural spacing.

Output: data/book/<slug>/chunks.jsonl + book.json (see manifest.py).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import typer

from bookbinder.manifest import BookManifest, Chunk, char_limit

app = typer.Typer(add_completion=False)


def load_config(root: Path) -> dict:
    path = Path(__file__).resolve().parents[3] / "config" / "pipeline.toml"
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def split_sentences(text: str, language: str) -> list[str]:
    import pysbd

    lang = language if language in pysbd.languages.LANGUAGE_CODES else "en"
    return [s.strip() for s in pysbd.Segmenter(language=lang, clean=False).segment(text) if s.strip()]


def hard_split(sentence: str, limit: int) -> list[str]:
    """Last resort for a single sentence longer than the model limit.

    Prefer clause punctuation, then whitespace. Never cut mid-word.
    """
    if len(sentence) <= limit:
        return [sentence]

    pieces, current = [], ""
    for token in sentence.replace(";", ";\x00").replace(",", ",\x00").split("\x00"):
        for word in ([token] if len(token) <= limit else token.split(" ")):
            candidate = f"{current} {word}".strip() if current else word.strip()
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    pieces.append(current)
                current = word.strip()[:limit]
    if current:
        pieces.append(current)
    return pieces


def pack(sentences: list[str], limit: int, min_chars: int) -> list[str]:
    """Greedily fill chunks up to `limit`, then fold away runt chunks."""
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        for piece in hard_split(sentence, limit):
            candidate = f"{current} {piece}".strip() if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current)

    # A two-word chunk makes XTTS clip its prosody, so fold runts into a
    # neighbour: backwards where there is one, forwards for a leading runt.
    merged: list[str] = []
    for chunk in chunks:
        if merged and len(chunk) < min_chars and len(merged[-1]) + len(chunk) + 1 <= limit:
            merged[-1] = f"{merged[-1]} {chunk}"
        else:
            merged.append(chunk)

    while (len(merged) > 1 and len(merged[0]) < min_chars
           and len(merged[0]) + len(merged[1]) + 1 <= limit):
        merged[0] = f"{merged[0]} {merged.pop(1)}"

    return merged


@app.command()
def main(
    slug: str = typer.Argument(..., help="Book slug under data/book/"),
    voice: str = typer.Option("", help="Voice name to record in the manifest"),
    max_chars: int = typer.Option(0, help="0 = use the XTTS limit for the book language"),
) -> None:
    root = Path(__file__).resolve().parents[3]
    book_dir = root / "data" / "book" / slug
    chapters_path = book_dir / "chapters.json"
    if not chapters_path.exists():
        raise typer.BadParameter(f"missing {chapters_path}; run `just ingest` first")

    config = load_config(root)
    book_cfg = config.get("book", {})
    chunk_cfg = config.get("chunk", {})

    payload = json.loads(chapters_path.read_text(encoding="utf-8"))
    meta, chapters = payload["meta"], payload["chapters"]
    language = meta.get("language", "pl")

    limit = max_chars or chunk_cfg.get("max_chars") or char_limit(language)
    min_chars = chunk_cfg.get("min_chars", 40)
    heading_pause = book_cfg.get("heading_pause_ms", 900)
    paragraph_pause = book_cfg.get("paragraph_pause_ms", 350)

    manifest = BookManifest(
        slug=slug,
        title=meta.get("title", slug),
        author=meta.get("author", "Unknown"),
        language=language,
        source_file=meta.get("source_file", ""),
        voice=voice,
    )

    order = 0
    for chapter in chapters:
        ch_index, ch_title = chapter["index"], chapter["title"]
        first_of_chapter = len(manifest.chunks)

        # The chapter title is narrated as its own fragment with a long pause,
        # which is also the anchor for the m4b chapter mark in stage 5.
        manifest.chunks.append(Chunk(
            id=f"ch{ch_index:03d}_{order:04d}",
            chapter_index=ch_index, chapter_title=ch_title, order=order,
            text=ch_title, kind="heading", language=language,
            pause_after_ms=heading_pause, source_ref=chapter.get("source_ref", ""),
        ))
        order += 1

        for p_index, paragraph in enumerate(chapter["paragraphs"]):
            sentences = split_sentences(paragraph, language)
            packed = pack(sentences, limit, min_chars)
            for i, text in enumerate(packed):
                last_in_paragraph = i == len(packed) - 1
                manifest.chunks.append(Chunk(
                    id=f"ch{ch_index:03d}_{order:04d}",
                    chapter_index=ch_index, chapter_title=ch_title, order=order,
                    text=text, kind="paragraph", language=language,
                    pause_after_ms=paragraph_pause if last_in_paragraph
                    else book_cfg.get("sentence_pause_ms", 120),
                    source_ref=f"{chapter.get('source_ref', '')}#p{p_index}",
                ))
                order += 1

        manifest.chapters.append({
            "index": ch_index,
            "title": ch_title,
            "first_chunk": first_of_chapter,
            "chunk_count": len(manifest.chunks) - first_of_chapter,
        })

    meta_path, chunks_path = manifest.write(book_dir)
    oversize = sum(1 for c in manifest.chunks if c.chars > limit)
    typer.echo(
        f"{len(manifest.chunks)} chunks across {len(manifest.chapters)} chapters "
        f"(limit {limit} chars for '{language}', {oversize} oversize)\n"
        f"~{manifest.est_hours} h estimated -> {chunks_path}"
    )


if __name__ == "__main__":
    app()
