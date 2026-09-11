"""One local catalog. Short transactions; migrations precede worker access."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

VERSION = 1


class StorageError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def database_path(root: Path) -> Path:
    return root / "data" / "audiobook.db"


def wal_safe(version: tuple[int, ...]) -> bool:
    return version >= (3, 51, 3) or (3, 50, 7) <= version < (3, 51, 0) or (3, 44, 6) <= version < (3, 45, 0)


class Database:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = database_path(self.root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        legacy = self.root / "data/.studio/queue.db"
        if not self.path.exists() and legacy.exists():
            raise StorageError("existing queue needs migration: run `just catalog-migrate` with workers stopped")
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > VERSION:
                raise StorageError(f"database version {version} is newer than supported {VERSION}")
            if version and not wal_safe(sqlite3.sqlite_version_info) and conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                try:
                    conn.execute("PRAGMA journal_mode=DELETE")
                except sqlite3.OperationalError as exc:
                    raise StorageError("close other database users before switching this SQLite runtime to safe rollback journaling") from exc
            if version == 0:
                # Schema and version commit together, including concurrent first opens.
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                    script = Path(__file__).with_name("storage_schema.sql").read_text()
                    statement = ""
                    for line in script.splitlines(keepends=True):
                        statement += line
                        if sqlite3.complete_statement(statement):
                            conn.execute(statement)
                            statement = ""
                    for table in ("assets", "book_revisions", "text_versions", "chapters", "model_snapshots",
                                  "chunk_plans", "chunks", "voice_revisions", "voice_references", "run_voices"):
                        conn.execute(f"CREATE TRIGGER immutable_{table} BEFORE UPDATE ON {table} "
                                     "BEGIN SELECT RAISE(ABORT, 'versioned records are immutable'); END")
                    conn.execute(f"PRAGMA user_version={VERSION}")
                conn.commit()
        # Journal mode is configured once for a new database, not on page reads.
        if version == 0:
            with self.connect() as conn:
                mode = "WAL" if wal_safe(sqlite3.sqlite_version_info) else "DELETE"
                conn.execute(f"PRAGMA journal_mode={mode}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=EXTRA")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def check(self) -> dict:
        with self.connect() as conn:
            integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            foreign = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
            return {"sqlite": sqlite3.sqlite_version,
                    "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                    "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
                    "integrity": integrity, "foreign_key_errors": foreign,
                    "ok": integrity == ["ok"] and not foreign}


def migrate_queue(root: Path) -> Database:
    """Preserve the old queue; refuse active recorded jobs before copying intent."""
    target = database_path(root)
    if target.exists():
        return Database(root)
    legacy = root / "data/.studio/queue.db"
    if not legacy.exists():
        return Database(root)
    for path in (root / "data/.studio/jobs").glob("*.json"):
        job = json.loads(path.read_text())
        if job.get("status") in ("running", "reserved") and job.get("pid"):
            try:
                os.kill(int(job["pid"]), 0)
            except ProcessLookupError:
                continue
            raise StorageError("stop active jobs and workers before migrating the queue")
    # Work on a consistent SQLite snapshot, never copy a live WAL main file.
    with tempfile.TemporaryDirectory(prefix="catalog-migration-") as folder:
        snapshot = Path(folder) / "queue.db"
        source = sqlite3.connect(legacy)
        try:
            dest = sqlite3.connect(snapshot)
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        old = sqlite3.connect(snapshot)
        old.row_factory = sqlite3.Row
        try:
            rows = list(old.execute("SELECT * FROM items ORDER BY id"))
        finally:
            old.close()
        if any(row["status"] in ("running", "claimed") for row in rows):
            raise StorageError("settle claimed/running queue entries before migration")
        staging_root = Path(folder) / "new"
        db = Database(staging_root)
        with db.write() as conn:
            for row in rows:
                keys = list(row.keys())
                conn.execute(f"INSERT INTO items ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(row))
        target.parent.mkdir(parents=True, exist_ok=True)
        # The staging DB has no live connections; backup folds any WAL into it.
        backup = root / "data/.studio/queue-before-catalog.db"
        if not backup.exists():
            shutil.copy2(snapshot, backup)
        fd, publication_name = tempfile.mkstemp(prefix=".catalog-migrate-", suffix=".db", dir=target.parent)
        os.close(fd)
        publication = Path(publication_name)
        try:
            with db.connect() as conn:
                published = sqlite3.connect(publication)
                try:
                    conn.backup(published)
                finally:
                    published.close()
            with publication.open("rb") as handle:
                os.fsync(handle.fileno())
            # Publish only a complete database, and never replace a concurrent one.
            os.link(publication, target)
        finally:
            publication.unlink(missing_ok=True)
    return Database(root)
