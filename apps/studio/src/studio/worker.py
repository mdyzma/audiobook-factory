"""Turn the queue into running work, one step at a time.

The queue records what should happen; nothing in it moves on its own. This is
what moves it: on each tick it brings finished jobs back into the queue, then
claims at most one new step and starts it.

**One step per tick, deliberately.** Starting everything that is ready would
put five renders on one graphics card. The device gate would refuse four of
them, but they would be refused as *failures*, and a batch that marks four
books failed because it started them all at once is worse than a batch that
takes its turn. So a tick that finds the device busy skips the work wanting it
and takes something else, and takes nothing at all if there is nothing else.

**A step that fails stops its own book and nothing else.** No pausing is needed
for that: a step which is not done already blocks what follows it, so the book
stands still with the reason recorded against the step that failed, while the
other nineteen carry on. Retrying that step puts the book back in motion, which
is the explicit continuation a person is expected to give.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from studio.jobs import GPU_ACTIONS, ORPHANED, JobError, JobRunner, ResourceBusy
from studio.queue import CLAIMED, RUNNING, Item, Queue, QueueError

# How long between ticks when nothing happened. Short enough that finishing one
# fragment-render and starting the next feels immediate, long enough that an
# idle dashboard is not reading the disk continuously.
IDLE_SECONDS = 3.0

# How much of a failed job's log to keep as the reason. Enough to carry the
# exception, not so much that the queue becomes a second log file.
REASON_CHARS = 300


@dataclass
class Tick:
    """What one pass did, for a caller that wants to say so."""

    settled: int = 0
    started: str = ""
    skipped_gpu: bool = False

    @property
    def idle(self) -> bool:
        return not self.settled and not self.started


def failure_reason(runner: JobRunner, job_id: str, fallback: str) -> str:
    """Why a step failed, in the words the stage itself used.

    The last thing a stage prints before dying is nearly always the reason, and
    it is what a person would have scrolled to. Carrying it onto the queue
    entry saves opening the log to find out which of twenty books needs a look.
    """
    tail = runner.tail(job_id, lines=40)
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return lines[-1][:REASON_CHARS] if lines else fallback


class Worker:
    """Drains one queue. Safe to run several of, though one is usually enough."""

    def __init__(self, root: Path, name: str = "") -> None:
        self.root = root
        self.name = name or f"studio-{os.getpid()}"
        self.queue = Queue(root)
        self.runner = JobRunner(root)

    # --- bringing the queue up to date ---------------------------------------

    def settle(self) -> int:
        """Match running queue entries to what their jobs actually did."""
        settled = 0
        for item in self.queue.items(status=CLAIMED):
            if item.run_id and self._settle_attempt(item):
                settled += 1
        for item in self.queue.items(status=RUNNING):
            if self._settle_one(item):
                settled += 1
        return settled

    def _settle_attempt(self, item: Item) -> bool:
        from studio.catalog import Catalog
        from studio.jobs import _alive
        catalog = Catalog(self.root)
        attempts = catalog.rows("SELECT * FROM job_attempts WHERE queue_id=? ORDER BY started_at DESC LIMIT 1", (item.id,))
        if not attempts:
            return False
        attempt = attempts[0]
        if attempt["status"] in ("done", "failed", "interrupted", "cancelled"):
            self._try(self.queue.finish, item.id, attempt["status"] == "done", attempt["error"])
            return True
        job = self.runner.store.load(attempt["job_id"]) if attempt["job_id"] else None
        if job and job.running or attempt["pid"] and _alive(attempt["pid"]):
            if item.status == CLAIMED:
                self._try(self.queue.start, item.id, attempt["job_id"] or attempt["id"])
            return False
        if attempt["status"] == "running" and attempt["pid"]:
            reason = "the stage process disappeared; retry to resume its completed fragments"
            with catalog.db.write() as conn:
                conn.execute("UPDATE job_attempts SET status='interrupted',error=? WHERE id=?", (reason, attempt["id"]))
                conn.execute("UPDATE audiobook_runs SET status='failed',error=? WHERE id=?", (reason, item.run_id))
            self._try(self.queue.finish, item.id, False, reason)
            return True
        return False

    def _settle_one(self, item: Item) -> bool:
        if item.run_id and self._settle_attempt(item):
            return True
        job = self.runner.store.load(item.job_id) if item.job_id else None
        if job is None:
            if item.run_id:
                from studio.catalog import Catalog
                from studio.jobs import _alive
                active = Catalog(self.root).rows("SELECT pid FROM job_attempts WHERE queue_id=? AND status='running'", (item.id,))
                if any(row["pid"] and _alive(row["pid"]) for row in active):
                    return False
            # The record was pruned, or never written. Nothing observed the
            # work, so the honest thing is to offer it again rather than to
            # call it done or failed on no evidence.
            self._try(self.queue.release, item.id,
                      "no job record was found, so this was never seen through")
            return True
        if job.running:
            return False
        if job.status == "succeeded":
            self._try(self.queue.finish, item.id, True, "")
            return True

        reason = {
            "cancelled": "the job was cancelled",
            ORPHANED: "the job disappeared; the machine may have gone down",
        }.get(job.status, "")
        self._try(self.queue.finish, item.id, False,
                  reason or failure_reason(self.runner, job.id,
                                           f"the job {job.status}"))
        return True

    def _try(self, call, *args, **kwargs) -> None:
        """Queue transitions race with a person clicking in the dashboard.

        Someone cancelling a book between this worker reading an item and
        writing it back is not an error worth stopping a batch over; their
        decision simply wins.
        """
        try:
            call(*args, **kwargs)
        except QueueError:
            pass

    # --- starting the next thing ---------------------------------------------

    def step(self) -> tuple[str, bool]:
        """Claim one item and start it. Returns what was started, and whether
        anything was held back for the device."""
        busy = self.runner.device_busy()
        skip = tuple(sorted(GPU_ACTIONS)) if busy else ()
        import uuid
        item = self.queue.claim(f"{self.name}:{uuid.uuid4().hex}", skip_actions=skip)
        if item is None:
            return "", bool(busy)

        args = dict(item.args)
        args.setdefault("slug", item.slug)
        try:
            if item.run_id:
                from studio.catalog import Catalog, identity
                from studio.database import now
                attempt = identity()
                with Catalog(self.root).db.write() as conn:
                    conn.execute("INSERT INTO job_attempts(id,run_id,queue_id,stage,job_id,status,started_at) "
                                 "VALUES (?,?,?,?,?,?,?)",
                                 (attempt, item.run_id, item.id, item.action, attempt, "launching", now()))
                job = self.runner.start(item.action, args, run_id=item.run_id, attempt_id=attempt)
            else:
                job = self.runner.start(item.action, args)
        except ResourceBusy as exc:
            # Something else has the book, the voice or the device: a matter of
            # timing, not a fault. Put it back and let the next tick have it.
            self._try(self.queue.release, item.id, str(exc), claim=item.claim)
            self._failed_launch(item, str(exc))
            return "", bool(busy)
        except JobError as exc:
            # A bad argument, or no room on the disk. This needs a person, and
            # the book stops here until one arrives.
            self._try(self.queue.finish, item.id, False, str(exc), claim=item.claim)
            self._failed_launch(item, str(exc))
            return "", bool(busy)

        self._try(self.queue.start, item.id, job.id, claim=item.claim)
        return item.label, bool(busy)

    def _failed_launch(self, item: Item, reason: str) -> None:
        if item.run_id:
            from studio.catalog import Catalog
            with Catalog(self.root).db.write() as conn:
                conn.execute("UPDATE job_attempts SET status='not_started',error=? WHERE queue_id=? AND status='launching'", (reason, item.id))

    def tick(self) -> Tick:
        settled = self.settle()
        started, skipped = self.step()
        return Tick(settled=settled, started=started, skipped_gpu=skipped)

    def run(self, idle_seconds: float = IDLE_SECONDS,
            max_ticks: int = 0) -> int:
        """Keep going until the queue has nothing left that can move.

        `max_ticks` bounds the loop for tests and for a one-shot drain; zero
        means until the work runs out.
        """
        ticks = 0
        while not max_ticks or ticks < max_ticks:
            ticks += 1
            result = self.tick()
            if not result.idle:
                continue
            if not self.queue.items(status=RUNNING) and not self.queue.ready():
                return ticks
            time.sleep(idle_seconds)
        return ticks


def main() -> None:
    """`just drain`: run the queue from a terminal instead of the dashboard."""
    import typer

    from studio.paths import project_root
    from studio.queue import report

    app = typer.Typer(add_completion=False)

    @app.command()
    def drain(
        once: bool = typer.Option(False, "--once", help="One tick, then stop"),
        idle: float = typer.Option(IDLE_SECONDS, help="Seconds between quiet ticks"),
    ) -> None:
        worker = Worker(project_root())
        if once:
            result = worker.tick()
            typer.echo(f"settled {result.settled}, "
                       f"started {result.started or 'nothing'}")
        else:
            typer.echo(f"drained after {worker.run(idle_seconds=idle)} tick(s)")
        typer.echo(report(worker.queue))

    app()


if __name__ == "__main__":
    main()
