"""What is waiting to happen, kept in a database rather than in a process.

A batch of twenty books is a list of steps that has to survive the browser
closing, this server restarting and the machine rebooting. `jobs.py` already
keeps *running* work on disk; this keeps the work that has not started yet.

**Why SQLite and not more JSON files.** The job store is one file per job,
which is fine for a list you only read. A queue needs one operation the
filesystem cannot give: take the next item and make certain nobody else takes
it too. Splitting that into a read and a write loses the race, and writing a
correct lock protocol around files is more machinery than a queue deserves.
SQLite's write transaction already is one.

The queue shares data/audiobook.db with the versioned library catalog. Binary
assets remain files; metadata and run history live in SQL. Back up the database
and its referenced assets together; the catalog is not a disposable cache.

**Steps, not books.** One item is one stage of one book. That is what lets a
failed chapter split hold back only that book's synthesis, and what gives the
GPU gate individual workloads to admit one at a time. Steps of the same book
run in order: a step waits while any earlier step of that book is unfinished.
Across books the order is the order they were added, so a long render does not
stop the next book's chapter split from starting.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from studio.database import Database, database_path

# Bumped when the table shape changes. Independent of the manifest schema
# version: the queue is Studio's own bookkeeping and no other environment
# reads it.
SCHEMA_VERSION = 1

# How long a claim is good for before the item is offered again. Only ever
# reached by a worker that died between claiming an item and starting its
# process, which is a window of milliseconds, so this can be generous.
LEASE_SECONDS = 120

# After this many claims that never became a running process, the item is held
# rather than offered again. A worker that crashes on one particular book would
# otherwise take it, die, and take it again forever.
MAX_CLAIMS = 3

# Long enough to outlast another request's write, short enough that a wedged
# database surfaces as an error instead of a hung page.
BUSY_TIMEOUT_MS = 5000

PENDING = "pending"
CLAIMED = "claimed"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
PAUSED = "paused"
CANCELLED = "cancelled"

# Still to happen, one way or another.
OPEN = (PENDING, CLAIMED, RUNNING, PAUSED)
SETTLED = (DONE, FAILED, CANCELLED)
STATUSES = (*OPEN, *SETTLED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    args        TEXT    NOT NULL DEFAULT '{}',
    position    INTEGER NOT NULL,
    batch       TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pending',
    claim       TEXT    NOT NULL DEFAULT '',
    claimed_at  TEXT    NOT NULL DEFAULT '',
    job_id      TEXT    NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS items_ready ON items (status, id);
CREATE INDEX IF NOT EXISTS items_book  ON items (slug, position);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# The next item that is allowed to run: pending, and with every earlier step of
# its own book already done. `<> done` rather than `in (pending, claimed, ...)`
# on purpose, so a cancelled or failed step still blocks what comes after it.
# Assembling a book whose synthesis was cancelled would produce a truncated
# audiobook, which is exactly the kind of quiet incompleteness this project
# refuses to ship.
READY_SQL = """
SELECT * FROM items AS i
 WHERE i.status = ?
   AND NOT EXISTS (SELECT 1 FROM items AS e
                    WHERE e.slug = i.slug
                      AND e.position < i.position
                      AND e.status <> ?)
 ORDER BY i.id
"""


class QueueError(RuntimeError):
    """A queue operation that will not happen, with a reason worth showing."""


def queue_path(root: Path) -> Path:
    """Queue and catalog share the application database."""
    return database_path(root)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _before(moment: str, seconds: int) -> str:
    return (datetime.fromisoformat(moment)
            - timedelta(seconds=seconds)).isoformat(timespec="seconds")


@dataclass
class Item:
    """One step of one book, and where it has got to."""

    id: int
    slug: str
    action: str
    args: dict[str, str] = field(default_factory=dict)
    position: int = 0
    batch: str = ""
    status: str = PENDING
    claim: str = ""
    claimed_at: str = ""
    job_id: str = ""
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""
    note: str = ""
    run_id: str = ""

    @property
    def open(self) -> bool:
        return self.status in OPEN

    @property
    def active(self) -> bool:
        """Held by a worker: claimed, or with a process of its own."""
        return self.status in (CLAIMED, RUNNING)

    @property
    def label(self) -> str:
        return f"{self.slug}/{self.action}"


def _item(row: sqlite3.Row) -> Item:
    try:
        args = json.loads(row["args"])
    except (TypeError, ValueError):
        args = {}
    return Item(
        id=row["id"], slug=row["slug"], action=row["action"],
        args=args if isinstance(args, dict) else {},
        position=row["position"], batch=row["batch"], status=row["status"],
        claim=row["claim"], claimed_at=row["claimed_at"], job_id=row["job_id"],
        attempts=row["attempts"], created_at=row["created_at"],
        updated_at=row["updated_at"], note=row["note"], run_id=row["run_id"] or "",
    )


class Queue:
    """The work waiting to happen, and the one safe way to take some of it."""

    def __init__(self, root: Path, lease_seconds: int = LEASE_SECONDS,
                 busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.root = root
        self.path = queue_path(root)
        self.lease = lease_seconds
        self.busy_timeout_ms = busy_timeout_ms
        self._prepare()

    # --- plumbing ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """A fresh connection per operation.

        Requests arrive on whichever thread the server picks, and a SQLite
        connection belongs to the thread that opened it. A queue holding tens
        of rows does not need a pool badly enough to earn that class of bug.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        # Readers do not block the writer, so listing the queue never waits on
        # a claim, and the page stays responsive mid-batch.
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = EXTRA")
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """A transaction that takes the write lock up front.

        `BEGIN IMMEDIATE`, not the default deferred begin: a claim reads the
        next item and then writes it, and the write lock has to be held for
        both. Deferred, the lock is only taken at the first write, so a second
        claimer walks into the gap and reads the same row; it then either loses
        the conditional update below or fails to upgrade its read at all. The
        immediate begin means that gap does not exist, and no claimer ever has
        to re-read, retry, or interpret a busy error.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _prepare(self) -> None:
        Database(self.root)

    @property
    def schema_version(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'"
                               ).fetchone()
        finally:
            conn.close()
        return int(row["value"]) if row else 0

    # --- adding --------------------------------------------------------------

    def add(self, slug: str, action: str, args: dict[str, str] | None = None,
            batch: str = "") -> Item:
        """Put one step at the end of its book's sequence, and of the queue."""
        if not slug or not action:
            raise QueueError("a queue item needs a book and an action")
        now = _now()
        with self._write() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM items "
                "WHERE slug = ?", (slug,)).fetchone()
            cursor = conn.execute(
                "INSERT INTO items (slug, action, args, position, batch, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, action, json.dumps(args or {}, sort_keys=True),
                 row["next"], batch, PENDING, now, now))
            item_id = int(cursor.lastrowid or 0)
        return self.require(item_id)

    def add_plan(self, slug: str, steps: list[tuple[str, dict[str, str]]],
                 batch: str = "", run_id: str = "") -> list[Item]:
        """Queue a whole book at once.

        In one transaction, so a batch review that queues twenty books either
        adds a book's steps or adds none of them. Half a book in the queue
        would render audio nothing later assembles.
        """
        now = _now()
        ids: list[int] = []
        with self._write() as conn:
            if run_id:
                existing = conn.execute("SELECT * FROM items WHERE run_id=? ORDER BY position", (run_id,)).fetchall()
                if existing:
                    return [_item(row) for row in existing]
            row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM items "
                "WHERE slug = ?", (slug,)).fetchone()
            position = int(row["next"])
            for offset, (action, args) in enumerate(steps):
                cursor = conn.execute(
                    "INSERT INTO items (slug, action, args, position, batch, "
                    "status, created_at, updated_at, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (slug, action, json.dumps(args or {}, sort_keys=True),
                     position + offset, batch, PENDING, now, now, run_id or None))
                ids.append(int(cursor.lastrowid or 0))
        return [self.require(i) for i in ids]

    # --- taking work ---------------------------------------------------------

    def claim(self, worker: str, skip_actions: "tuple[str, ...]" = ()) -> Item | None:
        """Take the next item that is allowed to run, or return None.

        Two workers calling this at the same moment get two different items, or
        one gets an item and the other gets None. Neither ever gets the same
        one, which is the entire reason this is a database.

        `skip_actions` passes over kinds of work that cannot start yet even
        though the queue is ready for them. That is how the one-workload-at-a-
        time rule for the GPU is honoured without this module knowing what a
        GPU is: a worker that finds the device busy skips the actions that want
        it, and takes the next book's chapter split instead of waiting.
        """
        if not worker:
            raise QueueError("a claim has to say who is claiming")
        now = _now()
        with self._write() as conn:
            self._expire(conn, now)
            row = self._next_ready(conn, skip_actions)
            if row is None:
                return None
            # Belt as well as braces: correct on its own even if the
            # transaction above is ever loosened to a deferred begin.
            changed = conn.execute(
                "UPDATE items SET status = ?, claim = ?, claimed_at = ?, "
                "attempts = attempts + 1, note = '', updated_at = ? "
                "WHERE id = ? AND status = ?",
                (CLAIMED, worker, now, now, row["id"], PENDING)).rowcount
            if not changed:
                return None
            item_id = int(row["id"])
        return self.require(item_id)

    def _next_ready(self, conn: sqlite3.Connection,
                    skip_actions: "tuple[str, ...]" = ()) -> sqlite3.Row | None:
        """The row `claim` is about to take. A seam, so a test can interleave."""
        if not skip_actions:
            return conn.execute(READY_SQL + " LIMIT 1", (PENDING, DONE)).fetchone()
        holes = ", ".join("?" for _ in skip_actions)
        return conn.execute(
            READY_SQL.replace("ORDER BY", f"AND i.action NOT IN ({holes}) ORDER BY")
            + " LIMIT 1", (PENDING, DONE, *skip_actions)).fetchone()

    def _expire(self, conn: sqlite3.Connection, now: str) -> None:
        """Offer again what a worker took and never started.

        Only items with no job: once a process exists, time says nothing about
        whether it is still going, and a twenty-hour render must not be handed
        to a second worker because it has been quiet. A running item that died
        is reconciled against the job store by the caller, not by a clock.
        """
        cutoff = _before(now, self.lease)
        conn.execute(
            "UPDATE items SET status = ?, claim = '', claimed_at = '', "
            "updated_at = ?, note = ? "
            "WHERE status = ? AND job_id = '' AND claimed_at < ? AND attempts >= ?",
            (PAUSED, now,
             f"claimed {MAX_CLAIMS} times without ever starting; held for a look",
             CLAIMED, cutoff, MAX_CLAIMS))
        conn.execute(
            "UPDATE items SET status = ?, claim = '', claimed_at = '', "
            "updated_at = ?, note = ? "
            "WHERE status = ? AND job_id = '' AND claimed_at < ?",
            (PENDING, now, "the worker that claimed this never started it",
             CLAIMED, cutoff))

    def start(self, item_id: int, job_id: str, claim: str = "") -> Item:
        """Record that the claimed item now has a process behind it."""
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE items SET status = ?, job_id = ?, updated_at = ? "
                "WHERE id = ? AND status = ? AND (? = '' OR claim = ?)",
                (RUNNING, job_id, _now(), item_id, CLAIMED, claim, claim)).rowcount
        if not changed:
            raise QueueError(f"item {item_id} is not claimed, so it cannot start")
        return self.require(item_id)

    def finish(self, item_id: int, ok: bool, note: str = "", claim: str = "") -> Item:
        """Settle an item its worker has seen through."""
        status = DONE if ok else FAILED
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE items SET status = ?, claim = '', claimed_at = '', "
                "note = ?, updated_at = ? WHERE id = ? AND status IN (?, ?) AND (? = '' OR claim = ?)",
                (status, note, _now(), item_id, CLAIMED, RUNNING, claim, claim)).rowcount
        if not changed:
            raise QueueError(f"item {item_id} is not running, so it cannot finish")
        return self.require(item_id)

    def release(self, item_id: int, note: str = "", claim: str = "") -> Item:
        """Put a claimed or running item back in the queue.

        For the case a clock cannot judge: the caller has looked at the job
        store, found the process gone with nothing recorded, and knows the work
        never happened.
        """
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE items SET status = ?, claim = '', claimed_at = '', "
                "job_id = '', note = ?, updated_at = ? WHERE id = ? AND status IN (?, ?) AND (? = '' OR claim = ?)",
                (PENDING, note, _now(), item_id, CLAIMED, RUNNING, claim, claim)).rowcount
        if not changed:
            raise QueueError(f"item {item_id} is not held by anyone")
        return self.require(item_id)

    # --- what a person does to the queue -------------------------------------

    def pause(self, item_id: int, note: str = "") -> Item:
        """Hold one item that has not started."""
        return self._set(item_id, PAUSED, (PENDING,), note,
                         f"item {item_id} has already started; cancel its job instead")

    def resume(self, item_id: int) -> Item:
        return self._set(item_id, PENDING, (PAUSED,), "",
                         f"item {item_id} is not paused")

    def cancel(self, item_id: int, note: str = "") -> Item:
        """Drop one item that has not started.

        Stopping something already running is the job runner's business: it
        owns the process group. Cancel the job, and the worker settles the item.
        """
        return self._set(item_id, CANCELLED, (PENDING, PAUSED), note,
                         f"item {item_id} has already started; cancel its job instead")

    def retry(self, item_id: int) -> Item:
        """Offer a failed or cancelled item again, at the back of nothing.

        Its position is untouched, so it goes back where it was in its book and
        the steps waiting behind it are unblocked in the right order.
        """
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE items SET status = ?, claim = '', claimed_at = '', "
                "job_id = '', attempts = 0, note = '', updated_at = ? "
                "WHERE id = ? AND status IN (?, ?)",
                (PENDING, _now(), item_id, FAILED, CANCELLED)).rowcount
        if not changed:
            raise QueueError(f"item {item_id} has not failed, so there is nothing to retry")
        return self.require(item_id)

    def _set(self, item_id: int, status: str, allowed: tuple[str, ...],
             note: str, refusal: str) -> Item:
        placeholders = ", ".join("?" for _ in allowed)
        with self._write() as conn:
            changed = conn.execute(
                f"UPDATE items SET status = ?, note = ?, updated_at = ? "
                f"WHERE id = ? AND status IN ({placeholders})",
                (status, note, _now(), item_id, *allowed)).rowcount
        if not changed:
            if self.get(item_id) is None:
                raise QueueError(f"no queue item {item_id}")
            raise QueueError(refusal)
        return self.require(item_id)

    def pause_book(self, slug: str, note: str = "") -> int:
        """Hold everything not yet started for one book.

        This is what an encoding, language or model exception does: the book it
        happened to stops, and the other nineteen carry on. Whatever is already
        running is left alone to finish or fail on its own.
        """
        with self._write() as conn:
            return conn.execute(
                "UPDATE items SET status = ?, note = ?, updated_at = ? "
                "WHERE slug = ? AND status = ?",
                (PAUSED, note, _now(), slug, PENDING)).rowcount

    def resume_book(self, slug: str) -> int:
        with self._write() as conn:
            return conn.execute(
                "UPDATE items SET status = ?, note = '', updated_at = ? "
                "WHERE slug = ? AND status = ?",
                (PENDING, _now(), slug, PAUSED)).rowcount

    def cancel_book(self, slug: str, note: str = "") -> int:
        """Drop a book's remaining steps.

        Cancelling one step alone would leave the rest of that book blocked
        behind it forever, since a step that is not done holds back what
        follows. Cancelling the book says so outright.
        """
        with self._write() as conn:
            return conn.execute(
                "UPDATE items SET status = ?, note = ?, updated_at = ? "
                "WHERE slug = ? AND status IN (?, ?)",
                (CANCELLED, note, _now(), slug, PENDING, PAUSED)).rowcount

    # --- reading -------------------------------------------------------------

    def get(self, item_id: int) -> Item | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        finally:
            conn.close()
        return _item(row) if row else None

    def require(self, item_id: int) -> Item:
        item = self.get(item_id)
        if item is None:
            raise QueueError(f"no queue item {item_id}")
        return item

    def items(self, status: str = "", slug: str = "", batch: str = "") -> list[Item]:
        where, params = ["1 = 1"], []
        if status:
            where.append("status = ?")
            params.append(status)
        if slug:
            where.append("slug = ?")
            params.append(slug)
        if batch:
            where.append("batch = ?")
            params.append(batch)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY id",
                params).fetchall()
        finally:
            conn.close()
        return [_item(r) for r in rows]

    def ready(self, skip_actions: "tuple[str, ...]" = ()) -> list[Item]:
        """Everything that could be claimed right now, in the order it would be."""
        conn = self._connect()
        try:
            if not skip_actions:
                rows = conn.execute(READY_SQL, (PENDING, DONE)).fetchall()
            else:
                holes = ", ".join("?" for _ in skip_actions)
                rows = conn.execute(
                    READY_SQL.replace("ORDER BY", f"AND i.action NOT IN ({holes}) ORDER BY"),
                    (PENDING, DONE, *skip_actions)).fetchall()
        finally:
            conn.close()
        return [_item(r) for r in rows]

    def blocked_by(self, item: Item) -> Item | None:
        """The earlier step of the same book that is holding this one up."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM items WHERE slug = ? AND position < ? AND status <> ? "
                "ORDER BY position LIMIT 1",
                (item.slug, item.position, DONE)).fetchone()
        finally:
            conn.close()
        return _item(row) if row else None

    def books(self) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT slug, MIN(id) AS first FROM items "
                                "GROUP BY slug ORDER BY first").fetchall()
        finally:
            conn.close()
        return [r["slug"] for r in rows]

    def summary(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM items "
                                "GROUP BY status").fetchall()
        finally:
            conn.close()
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[row["status"]] = row["n"]
        counts["total"] = sum(counts[s] for s in STATUSES)
        counts["open"] = sum(counts[s] for s in OPEN)
        return counts


# --- showing it -------------------------------------------------------------

def report(queue: "Queue", slug: str = "") -> str:
    """The queue as a person needs to read it before touching anything.

    Grouped by book, because that is the unit a decision is made about: a book
    is paused, retried or cancelled, not an individual chapter split.
    """
    items = queue.items(slug=slug)
    if not items:
        return "nothing queued"

    ready = {item.id for item in queue.ready()}
    lines: list[str] = []
    for book in queue.books():
        mine = [i for i in items if i.slug == book]
        if not mine:
            continue
        lines.append(book)
        # Pausing a book writes one reason onto every step of it, so printing
        # each in turn says the same sentence five times. Said once, where it
        # belongs, and per step only when the steps actually differ.
        shared = {i.note for i in mine if i.note}
        common = shared.pop() if len(shared) == 1 and len(mine) > 1 else ""
        if common:
            lines.append(f"        {common}")
        for item in mine:
            mark = "  next" if item.id in ready else ""
            lines.append(f"  {item.id:>4}  {item.status:9} {item.action}{mark}")
            if item.note and item.note != common:
                lines.append(f"        {item.note}")
            if item.status == PENDING and item.id not in ready:
                blocker = queue.blocked_by(item)
                if blocker is not None:
                    lines.append(f"        waiting for {blocker.action} "
                                 f"({blocker.status})")
        lines.append("")

    counts = queue.summary()
    lines.append(f"  {counts['total']} step(s): "
                 + ", ".join(f"{counts[s]} {s}" for s in STATUSES if counts[s]))
    return "\n".join(lines)


def main() -> None:
    """`just queue`, and the small state changes that do not need a browser."""
    import typer

    from studio.paths import project_root

    app = typer.Typer(add_completion=False)

    @app.command()
    def show(
        book: str = typer.Option("", help="Only this book"),
        retry: int = typer.Option(0, help="Offer this failed step again"),
        cancel: str = typer.Option("", help="Drop this book's remaining steps"),
        resume: str = typer.Option("", help="Un-pause this book"),
    ) -> None:
        queue = Queue(project_root())
        try:
            if retry:
                typer.echo(f"retrying {queue.retry(retry).label}")
            if cancel:
                typer.echo(f"cancelled {queue.cancel_book(cancel)} step(s) of {cancel}")
            if resume:
                typer.echo(f"resumed {queue.resume_book(resume)} step(s) of {resume}")
        except QueueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        typer.echo(report(queue, slug=book))

    app()


if __name__ == "__main__":
    main()
