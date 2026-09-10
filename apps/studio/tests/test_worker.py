"""The queue records what should happen; this is what makes it happen.

Two properties carry the batch. One step starts per tick, so twenty books do
not all reach the graphics card at once and get marked failed for it. And a
step that fails stops its own book and nothing else, which is the whole reason
for importing a folder rather than a file.
"""

from __future__ import annotations

import os
import signal
import subprocess

import pytest

from studio import jobs
from studio.jobs import DEVICE_LOCK, Job, JobStore
from studio.queue import CLAIMED, DONE, FAILED, PENDING, RUNNING, Queue
from studio.worker import Worker

PLAN = [("chunk", {}), ("synth", {"voice": "michal"}), ("assemble", {"format": "m4b"})]

# Captured before anything is patched. `jobs.subprocess` is the stdlib module
# itself, so patching Popen there patches it everywhere, this file included.
REAL_POPEN = subprocess.Popen


class FakePopen:
    """A real child that does nothing, in a session of its own.

    Not this process: cancelling a job signals the whole process group, and a
    stand-in reporting our own pid takes pytest down with it. It has to be a
    genuine process for the liveness check to mean anything either.
    """

    spawned: "list[subprocess.Popen]" = []

    def __init__(self, *args, **kwargs) -> None:
        self._proc = REAL_POPEN(["/bin/sh", "-c", "sleep 30"],
                                start_new_session=True)
        self.pid = self._proc.pid
        FakePopen.spawned.append(self._proc)


@pytest.fixture
def worker(project, monkeypatch):
    monkeypatch.setattr(jobs.subprocess, "Popen", FakePopen)
    yield Worker(project, name="test-worker")
    for proc in FakePopen.spawned:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        proc.wait(timeout=5)
    FakePopen.spawned.clear()


def finish(worker: Worker, job_id: str, code: str = "0", log: str = "") -> None:
    """Make a started job look like one that has exited."""
    if log:
        worker.runner.store.log_path(job_id).write_text(log, encoding="utf-8")
    worker.runner.store.exit_path(job_id).write_text(code, encoding="utf-8")


def hold_the_device(project) -> Job:
    store = JobStore(project)
    store.dir.mkdir(parents=True, exist_ok=True)
    job = Job(id="dddddddddddd", action="synth", args={"slug": "elsewhere"},
              status="running", pid=os.getpid(),
              started_at="2026-01-01T00:00:00+00:00", locks_held=[DEVICE_LOCK])
    store.save(job)
    store.acquire(DEVICE_LOCK, job.id)
    return job


class TestOneStepPerTick:
    def test_an_empty_queue_does_nothing(self, worker):
        assert worker.tick().idle

    def test_a_tick_starts_the_first_step(self, worker):
        worker.queue.add_plan("solaris", PLAN)
        assert worker.tick().started == "solaris/chunk"

    def test_the_started_step_records_its_job(self, worker):
        worker.queue.add_plan("solaris", PLAN)
        worker.tick()
        item = worker.queue.items(slug="solaris")[0]
        assert item.status == RUNNING and item.job_id

    def test_a_second_tick_does_not_start_a_second_step(self, worker):
        # Starting everything that is ready is how five renders reach one
        # graphics card and four of them are marked failed for arriving.
        worker.queue.add_plan("solaris", PLAN)
        worker.queue.add_plan("eden", PLAN)
        worker.tick()
        assert worker.tick().started == "eden/chunk"
        assert len(worker.queue.items(status=RUNNING)) == 2

    def test_the_book_advances_once_its_step_finishes(self, worker):
        worker.queue.add_plan("solaris", PLAN)
        first = worker.tick()
        item = worker.queue.items(slug="solaris")[0]
        finish(worker, item.job_id)

        result = worker.tick()
        assert first.started == "solaris/chunk"
        assert result.started == "solaris/synth"
        assert worker.queue.require(item.id).status == DONE


class TestAFailureStopsOneBook:
    def _failed(self, worker, log="RuntimeError: the language could not be settled"):
        worker.queue.add_plan("solaris", PLAN)
        worker.queue.add_plan("eden", PLAN)
        worker.tick()
        item = worker.queue.items(slug="solaris")[0]
        finish(worker, item.job_id, code="1", log=log)
        return item

    def test_the_step_is_marked_failed(self, worker):
        item = self._failed(worker)
        worker.tick()
        assert worker.queue.require(item.id).status == FAILED

    def test_the_reason_is_the_last_thing_the_stage_said(self, worker):
        item = self._failed(worker)
        worker.tick()
        assert "language could not be settled" in worker.queue.require(item.id).note

    def test_the_rest_of_that_book_stands_still(self, worker):
        self._failed(worker)
        worker.tick()                     # settles the failure, starts eden
        assert "solaris" not in {i.slug for i in worker.queue.ready()}
        # Still pending rather than paused, so retrying the failed step alone
        # is enough to set the book going again.
        assert [i.status for i in worker.queue.items(slug="solaris")[1:]] == \
            [PENDING, PENDING]

    def test_the_other_books_carry_on(self, worker):
        self._failed(worker)
        assert worker.tick().started == "eden/chunk"

    def test_retrying_puts_the_book_back_in_motion(self, worker):
        item = self._failed(worker)
        worker.tick()
        worker.queue.retry(item.id)
        assert "solaris/chunk" in [i.label for i in worker.queue.ready()]

    def test_a_cancelled_job_says_so_rather_than_blaming_the_book(self, worker):
        worker.queue.add_plan("solaris", PLAN)
        worker.tick()
        item = worker.queue.items(slug="solaris")[0]
        worker.runner.cancel(item.job_id)
        worker.tick()
        assert "cancelled" in worker.queue.require(item.id).note


class TestTheDeviceIsRespected:
    def test_a_busy_device_sends_the_tick_to_another_book(self, worker, project):
        hold_the_device(project)
        worker.queue.add_plan("solaris", [("synth", {"voice": "michal"})])
        worker.queue.add_plan("eden", [("chunk", {})])
        assert worker.tick().started == "eden/chunk"

    def test_nothing_starts_when_only_gpu_work_is_left(self, worker, project):
        hold_the_device(project)
        worker.queue.add_plan("solaris", [("synth", {"voice": "michal"})])
        result = worker.tick()
        assert result.started == "" and result.skipped_gpu

    def test_the_held_back_step_is_not_marked_failed(self, worker, project):
        # It is waiting its turn, not broken. Marking it failed would need a
        # person to retry every book in a batch that behaved correctly.
        hold_the_device(project)
        worker.queue.add_plan("solaris", [("synth", {"voice": "michal"})])
        worker.tick()
        assert worker.queue.items(slug="solaris")[0].status == PENDING


class TestRefusalsAreToldApart:
    def test_something_else_holding_the_book_puts_the_step_back(self, worker, project):
        # A person clicking Synthesise in the dashboard is a matter of timing,
        # not a fault in the book.
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        store.save(Job(id="aaaaaaaaaaaa", action="synth", args={"slug": "solaris"},
                       status="running", pid=os.getpid(),
                       started_at="2026-01-01T00:00:00+00:00"))

        worker.queue.add_plan("solaris", [("chunk", {})])
        worker.tick()
        assert worker.queue.items(slug="solaris")[0].status == PENDING

    def test_a_full_disk_stops_the_book_and_says_why(self, worker, project, monkeypatch):
        from bookbinder import preflight

        book_dir = project / "data" / "book" / "solaris"
        (book_dir / "chunks.jsonl").write_text(
            '{"id": "ch001_0000", "est_seconds": 36000.0}\n', encoding="utf-8")
        monkeypatch.setattr(preflight, "free_bytes", lambda path: 1024)

        worker.queue.add_plan("solaris", [("synth", {"voice": "michal"})])
        worker.tick()
        item = worker.queue.items(slug="solaris")[0]
        assert item.status == FAILED and "Free some space" in item.note


class TestSettlingWhatIsAlreadyRunning:
    def test_a_job_whose_record_is_gone_is_offered_again(self, worker):
        # Nothing observed the work, so calling it done or failed would be
        # inventing a result. Pruned logs are the ordinary cause.
        worker.queue.add_plan("solaris", PLAN)
        worker.tick()
        item = worker.queue.items(slug="solaris")[0]
        worker.runner.store._path(item.job_id).unlink()

        worker.tick()
        back = worker.queue.require(item.id)
        assert back.status in (PENDING, CLAIMED, RUNNING)
        assert back.status != DONE

    def test_settling_reports_how_much_it_moved(self, worker):
        worker.queue.add_plan("solaris", PLAN)
        worker.tick()
        finish(worker, worker.queue.items(slug="solaris")[0].job_id)
        assert worker.tick().settled == 1


class TestDraining:
    def test_it_stops_when_there_is_nothing_left_to_do(self, worker):
        assert worker.run(idle_seconds=0) == 1

    def test_it_keeps_going_while_steps_finish(self, worker):
        worker.queue.add_plan("solaris", [("chunk", {}), ("assemble", {"format": "m4b"})])
        worker.tick()
        for item in worker.queue.items(status=RUNNING):
            finish(worker, item.job_id)
        assert worker.run(idle_seconds=0, max_ticks=6) >= 1
        assert worker.queue.items(slug="solaris")[0].status == DONE
