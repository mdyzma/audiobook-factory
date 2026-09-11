"""Take work back out of the catalog, and stop paying for it twice.

Two gaps this closes, both found by using the thing rather than reading it.

**A run could be added and never removed.** Comparing two voices for one book
means rendering it twice, and comparing models across two languages means
several times again. Every one of those runs was permanent, and a twenty-hour
book is gigabytes. There was no command to drop one, and the versioned tables
carry triggers that refuse updates, so editing the database by hand was not a
safe fallback either.

**A run whose directory was deleted wedged its book.** Removing a run folder is
the obvious way to reclaim space. The row stayed, every stage for that book
then failed trying to open a lock file inside a directory that was gone, and
`reconcile` did not clear it. Forgetting the run is the way out, so this works
whether or not the directory is still there.

Assets are swept, not deleted outright. Their names are content hashes, so two
runs of the same book share the fragments that did not change; an asset goes
only when nothing at all still points at it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from studio.catalog import Catalog, inside, relink
from studio.database import StorageError

# Every table holding a run, children before the parents they reference.
RUN_TABLES = (
    ("render_fragments", "run_id"),
    ("exports", "run_id"),
    ("qa_runs", "run_id"),
    ("run_reports", "run_id"),
    ("run_voices", "run_id"),
    ("job_attempts", "run_id"),
)

# Everywhere an asset can still be spoken for.
ASSET_REFERENCES = (
    ("book_revisions", "source_asset_id"),
    ("voice_references", "asset_id"),
    ("render_fragments", "asset_id"),
    ("exports", "asset_id"),
    ("import_items", "asset_id"),
)


def busy_reason(catalog: Catalog, run_id: str) -> str:
    """Why this run must not be forgotten yet, if it must not."""
    rows = catalog.rows("SELECT status FROM audiobook_runs WHERE id=?", (run_id,))
    if not rows:
        raise StorageError(f"no run {run_id}")
    if rows[0]["status"] == "running":
        return f"run {run_id} is still running; stop it before forgetting it"
    queued = catalog.rows(
        "SELECT status FROM items WHERE run_id=? AND status IN ('pending','claimed','running')",
        (run_id,))
    if queued:
        return (f"run {run_id} still has {len(queued)} queued step(s); cancel them "
                f"first with `just queue-cancel`")
    return ""


def forget_run(root: Path, run_id: str, force: bool = False) -> dict:
    """Remove one run, its records, its directory, and anything only it held.

    The queue rows are unlinked rather than deleted: a finished step is history
    worth keeping, and it names the book by slug regardless.
    """
    catalog = Catalog(root)
    reason = busy_reason(catalog, run_id)
    if reason and not force:
        raise StorageError(reason)

    run = catalog.run(run_id)
    removed: dict = {"run": run_id, "slug": run["slug"], "rows": 0, "directory": False}

    with catalog.db.write() as conn:
        for table, column in RUN_TABLES:
            removed["rows"] += conn.execute(
                f"DELETE FROM {table} WHERE {column}=?", (run_id,)).rowcount
        conn.execute("UPDATE items SET run_id=NULL WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM audiobook_runs WHERE id=?", (run_id,))

    root_key = run["root_key"]
    if root_key:
        try:
            directory = inside(root, root_key)
        except StorageError:
            directory = None
        if directory is not None and directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
            removed["directory"] = True

    removed["assets"] = sweep_assets(root)
    return removed


def forget_book(root: Path, slug: str, force: bool = False) -> dict:
    """Remove a book: every run of it, its text, its plans, and its bytes.

    Deleting the runs is not enough on its own. A book imported by mistake, or
    a probe someone made while testing, otherwise stays in the library forever
    with no way out, because the versioned tables refuse updates and hand
    editing is not a safe fallback.

    The files under `data/book/<slug>/` go too. They are the classic tree the
    stages still read, and leaving them behind means the next reconcile puts
    the book straight back.
    """
    catalog = Catalog(root)
    rows = catalog.rows("SELECT id FROM books WHERE slug=?", (slug,))
    if not rows:
        raise StorageError(f"no book '{slug}'")
    book_id = rows[0]["id"]

    runs = [r["id"] for r in catalog.rows(
        "SELECT id FROM audiobook_runs WHERE book_id=?", (book_id,))]
    if not force:
        for run_id in runs:
            reason = busy_reason(catalog, run_id)
            if reason:
                raise StorageError(reason)

    # Each run sweeps as it goes, so the totals have to accumulate or the
    # summary reports zero reclaimed while the disk says otherwise.
    swept = {"removed": 0, "bytes_freed": 0}
    result: dict = {"book": slug, "runs": len(runs), "rows": 0}
    for run_id in runs:
        freed = forget_run(root, run_id, force=True)["assets"]
        swept["removed"] += freed["removed"]
        swept["bytes_freed"] += freed["bytes_freed"]

    catalog = Catalog(root)
    with catalog.db.write() as conn:
        # The book points at its current text and plan, so let go of those
        # before deleting what they name.
        conn.execute("UPDATE books SET current_text_id=NULL, current_plan_id=NULL "
                     "WHERE id=?", (book_id,))
        texts = [r[0] for r in conn.execute(
            "SELECT t.id FROM text_versions t JOIN book_revisions r ON r.id=t.revision_id "
            "WHERE r.book_id=?", (book_id,))]
        for text_id in texts:
            plans = [r[0] for r in conn.execute(
                "SELECT id FROM chunk_plans WHERE text_version_id=?", (text_id,))]
            for plan_id in plans:
                result["rows"] += conn.execute(
                    "DELETE FROM chunks WHERE plan_id=?", (plan_id,)).rowcount
            result["rows"] += conn.execute(
                "DELETE FROM chunk_plans WHERE text_version_id=?", (text_id,)).rowcount
            result["rows"] += conn.execute(
                "DELETE FROM chapters WHERE text_version_id=?", (text_id,)).rowcount
        conn.execute("UPDATE import_items SET book_id=NULL, text_version_id=NULL "
                     "WHERE book_id=?", (book_id,))
        for text_id in texts:
            result["rows"] += conn.execute(
                "DELETE FROM text_versions WHERE id=?", (text_id,)).rowcount
        result["rows"] += conn.execute(
            "DELETE FROM book_revisions WHERE book_id=?", (book_id,)).rowcount
        result["rows"] += conn.execute("DELETE FROM books WHERE id=?", (book_id,)).rowcount

    for directory in (root / "data/book" / slug, root / "data/sources" / slug,
                      root / "data/audio" / slug):
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)

    final = sweep_assets(root)
    result["assets"] = {"removed": swept["removed"] + final["removed"],
                        "bytes_freed": swept["bytes_freed"] + final["bytes_freed"]}
    return result


def unreferenced(catalog: Catalog) -> list[dict]:
    """Assets nothing points at any more."""
    clauses = " AND ".join(
        f"a.id NOT IN (SELECT {column} FROM {table} WHERE {column} IS NOT NULL)"
        for table, column in ASSET_REFERENCES)
    return [dict(row) for row in
            catalog.rows(f"SELECT a.* FROM assets a WHERE {clauses}")]


def sweep_assets(root: Path) -> dict:
    """Delete stored bytes nothing still claims.

    Content-addressed, so this is safe in the way that matters: two runs of one
    book share every fragment whose inputs did not change, and an asset leaves
    only when the last reference to it has gone.
    """
    catalog = Catalog(root)
    doomed = unreferenced(catalog)
    freed = 0
    for asset in doomed:
        try:
            path = inside(root, asset["storage_key"])
        except StorageError:
            continue
        if path.is_file():
            freed += path.stat().st_size
            path.unlink()
    if doomed:
        with catalog.db.write() as conn:
            conn.executemany("DELETE FROM assets WHERE id=?",
                             [(a["id"],) for a in doomed])
    return {"removed": len(doomed), "bytes_freed": freed}


def collapse_runs(root: Path) -> dict:
    """Fold existing run files into the assets they duplicate.

    Runs made before fragments were linked rather than copied still hold their
    own bytes. This finds the files that are already registered as assets and
    points them at the stored copy, which is what a fresh run now does on its
    own. Nothing is re-hashed: only files a run actually recorded are touched,
    and only when the two are not already one.
    """
    catalog = Catalog(root)
    linked, freed, skipped = 0, 0, 0
    for run in catalog.rows("SELECT id, root_key FROM audiobook_runs"):
        try:
            base = inside(root, run["root_key"])
        except StorageError:
            continue
        if not base.is_dir():
            continue
        rows = catalog.rows(
            "SELECT f.chunk_id, c.chunk_key, a.storage_key, a.size_bytes "
            "FROM render_fragments f "
            "JOIN chunks c ON c.id = f.chunk_id "
            "JOIN assets a ON a.id = f.asset_id WHERE f.run_id=?", (run["id"],))
        for row in rows:
            stored = inside(root, row["storage_key"])
            for candidate in base.glob(f"data/audio/*/{row['chunk_key']}.wav"):
                if not stored.is_file():
                    break
                if candidate.samefile(stored):
                    continue
                # Identical bytes by construction: the asset's name is their
                # hash and this run recorded the fragment under it. Checking
                # the size catches a file replaced behind the catalog's back.
                if candidate.stat().st_size != stored.stat().st_size:
                    skipped += 1
                    continue
                if relink(candidate, stored):
                    linked += 1
                    freed += row["size_bytes"]
                else:
                    skipped += 1
    return {"linked": linked, "bytes_freed": freed, "skipped": skipped}
