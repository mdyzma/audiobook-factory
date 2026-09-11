"""Durable folder decisions, using Bookbinder's existing extraction rules."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast
from pathlib import Path
import shutil
import tempfile

from bookbinder.ingest import import_book, slugify
from bookbinder.library import scan
from bookbinder.manifest import publish_text

from studio.catalog import Catalog, encode, identity
from studio.database import StorageError, now


def import_sources(root: Path, sources: list[Path], **options) -> dict:
    """Serialize publication of current authoring files, without holding SQL locks."""
    import fcntl
    (root / "data").mkdir(parents=True, exist_ok=True)
    with (root / "data/.import.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _import_sources(root, sources, **options)


def _import_sources(root: Path, sources: list[Path], *, folder: str = "",
                   language: str = "", encoding: str = "", slug: str = "",
                   title: str = "", author: str = "") -> dict:
    from studio.data import check_name
    catalog = Catalog(root)
    batch_id = identity()
    with catalog.db.write() as conn:
        conn.execute("INSERT INTO import_batches VALUES (?,?,?)", (batch_id, folder, now()))
    results = []
    for position, source in enumerate(sources):
        item_id = identity()
        status, asset_id, book_id, text_id = "failed", None, None, None
        details: dict = {}
        try:
            if source.suffix.lower() not in (".txt", ".epub"):
                status = "unsupported"
                details = {"reasons": ["only TXT and EPUB are supported"]}
            else:
                asset_id = catalog.asset(source)
                existing = catalog.rows("SELECT b.slug,b.id FROM books b JOIN book_revisions r ON r.book_id=b.id "
                                        "WHERE r.source_asset_id=? AND r.id IN "
                                        "(SELECT revision_id FROM text_versions WHERE id=b.current_text_id)", (asset_id,))
                previous = catalog.rows("SELECT b.slug,b.id FROM books b JOIN book_revisions r ON r.book_id=b.id "
                                        "WHERE r.original_source=? ORDER BY r.created_at DESC", (str(source.resolve()),))
                if existing and not any((language, encoding, title, author, slug)):
                    status, book_id = "unchanged", existing[0]["id"]
                    details = {"slug": existing[0]["slug"]}
                else:
                    chosen = slug or (previous[0]["slug"] if previous else slugify(source.stem))
                    check_name(chosen)
                    if not slug and not previous:
                        stem, number = chosen, 2
                        while catalog.rows("SELECT id FROM books WHERE slug=?", (chosen,)):
                            chosen = f"{stem}-{number}"
                            number += 1
                    # Never extract from the input folder after hashing it.
                    with tempfile.TemporaryDirectory(prefix="book-import-") as scratch:
                        staged = Path(scratch) / source.name
                        shutil.copy2(catalog.asset_path(asset_id), staged)
                        result = import_book(root, staged, slug=chosen, language=language,
                                             encoding=encoding, title=title, author=author, dry_run=True)
                    details = {"slug": chosen, "reasons": result.reasons, "warnings": result.warnings}
                    if result.extraction and result.chapters:
                        found = result.extraction
                        meta = dict(found.meta)
                        meta.update({"slug": chosen, "title": result.title, "author": result.author,
                                     "language": result.language, "source_file": str(catalog.asset_path(asset_id).relative_to(root)),
                                     "original_source": str(source.resolve()), "source_sha256": asset_id,
                                     "encoding": found.encoding,
                                     "language_decision": asdict(cast(Any, result.decision)) if result.decision else {},
                                     "needs_review": result.needs_review, "review_reasons": result.reasons})
                        # Only the contract fields belong in the language record.
                        decision = meta["language_decision"]
                        meta["language_decision"] = {k: v for k, v in decision.items() if k in
                            ("method", "confidence", "detector", "metadata_language", "coverage_chars", "samples", "warnings")}
                        destination = root / "data/book" / chosen / "chapters.json"
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if (destination.parent / "book.json").exists():
                            catalog.register_book(chosen)
                            from studio.legacy import archive_audio
                            archive_audio(catalog, chosen)
                        publish_text(destination, encode({"meta": meta, "chapters": found.chapters}))
                        publish_text(destination.parent / ".needs-chunking", asset_id)
                        registered = catalog.register_book(chosen)
                        book_id, text_id = registered["book_id"], registered["text_version_id"]
                        status = "review" if result.needs_review else "imported"
                    else:
                        status = "review" if result.needs_review else "failed"
        except (OSError, ValueError) as exc:
            details = {"reasons": [str(exc)]}
        with catalog.db.write() as conn:
            conn.execute("INSERT INTO import_items VALUES (?,?,?,?,?,?,?,?,?)",
                         (item_id, batch_id, position, str(source.resolve()), asset_id,
                          book_id, text_id, status, encode(details)))
        results.append({"id": item_id, "source": str(source), "status": status, **details})
    imported = sum(row["status"] == "imported" for row in results)
    paused = sum(row["status"] in ("review", "failed") for row in results)
    return {"batch_id": batch_id, "items": results, "imported": imported, "paused": paused,
            "text": "\n".join(f"{row['source']}: {row['status']} " + "; ".join(row.get("reasons") or []) for row in results)}


def import_folder(root: Path, folder: Path, *, recursive: bool = False,
                  language: str = "", encoding: str = "") -> dict:
    found = scan(root, folder, recursive=recursive)
    sources = [candidate.path for candidate in found.books] + [candidate.path for candidate in found.unsupported]
    return import_sources(root, sources, folder=str(folder.resolve()), language=language, encoding=encoding)
