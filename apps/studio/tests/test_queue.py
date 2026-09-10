"""The queue's job is to be right about two things: order and exclusivity.

Order, because a book assembled before its synthesis finished is a truncated
audiobook that looks complete. Exclusivity, because two workers rendering one
book share an audio directory and a fragment list, and neither notices.

Everything else here is bookkeeping. These two are the reasons the queue is a
database instead of a list of files.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from studio.queue import (
    CANCELLED,
    DONE,
    FAILED,
    MAX_CLAIMS,
    PAUSED,
    PENDING,
    RUNNING,
    Queue,
    QueueError,
    report,
)

BOOK_STEPS = [("chunk", {}), ("synth", {"voice": "michal"}), ("assemble", {"format": "m4b"})]


@pytest.fixture
def queue(project):
    return Queue(project)


def age(queue: Queue, item_id: int, seconds: int) -> None:
    """Backdate a claim, to stand in for a worker that died some time ago."""
    from studio.queue import _before, _now

    conn = sqlite3.connect(queue.path)
    with conn:
        conn.execute("UPDATE items SET claimed_at = ? WHERE id = ?",
                     (_before(_now(), seconds), item_id))
    conn.close()


class TestItArrivesInOrder:
    def test_steps_of_one_book_are_positioned_as_given(self, queue):
        items = queue.add_plan("solaris", BOOK_STEPS)
        assert [i.action for i in items] == ["chunk", "synth", "assemble"]
        assert [i.position for i in items] == [0, 1, 2]

    def test_each_book_counts_its_own_positions(self, queue):
        queue.add_plan("solaris", BOOK_STEPS)
        second = queue.add_plan("eden", BOOK_STEPS)
        assert [i.position for i in second] == [0, 1, 2]

    def test_a_later_step_waits_for_the_earlier_one(self, queue):
        queue.add_plan("solaris", BOOK_STEPS)
        assert [i.action for i in queue.ready()] == ["chunk"]

    def test_finishing_a_step_releases_the_next(self, queue):
        items = queue.add_plan("solaris", BOOK_STEPS)
        queue.claim("w")
        queue.start(items[0].id, "job1")
        queue.finish(items[0].id, ok=True)
        assert [i.action for i in queue.ready()] == ["synth"]

    def test_books_are_offered_in_the_order_they_were_added(self, queue):
        queue.add_plan("solaris", BOOK_STEPS)
        queue.add_plan("eden", BOOK_STEPS)
        assert [i.slug for i in queue.ready()] == ["solaris", "eden"]

    def test_a_long_render_does_not_stop_the_next_book_starting(self, queue):
        # The point of claiming one item at a time: while solaris is being
        # synthesised, eden's chapter split is still the next thing available.
        queue.add_plan("solaris", BOOK_STEPS)
        queue.add_plan("eden", BOOK_STEPS)
        first = queue.claim("w")
        assert first is not None and first.label == "solaris/chunk"
        second = queue.claim("w")
        assert second is not None and second.label == "eden/chunk"


class TestAnUnfinishedStepBlocksWhatFollows:
    """Not `pending or running`: anything that is not done."""

    def _stall(self, queue, status):
        items = queue.add_plan("solaris", BOOK_STEPS)
        queue.claim("w")
        queue.start(items[0].id, "job1")
        queue.finish(items[0].id, ok=(status == DONE))
        return items

    def test_a_failed_step_holds_back_the_rest(self, queue):
        self._stall(queue, FAILED)
        assert queue.ready() == []

    def test_a_cancelled_step_holds_back_the_rest(self, queue):
        items = queue.add_plan("solaris", BOOK_STEPS)
        queue.cancel(items[0].id, "not this one")
        assert queue.ready() == []

    def test_retrying_the_failed_step_lets_the_book_continue(self, queue):
        items = self._stall(queue, FAILED)
        queue.retry(items[0].id)
        assert [i.action for i in queue.ready()] == ["chunk"]

    def test_the_report_says_what_a_waiting_step_is_waiting_for(self, queue):
        self._stall(queue, FAILED)
        text = report(queue)
        assert "waiting for chunk (failed)" in text


class TestOnlyOneWorkerGetsAnItem:
    def test_two_threads_claiming_together_get_different_items(self, queue):
        # Each thread opens its own connection, so this exercises SQLite's own
        # locking rather than a lock in this process. A deferred transaction
        # fails here: both threads read the same next item before either wrote.
        for index in range(8):
            queue.add(f"book-{index}", "chunk")

        workers = 8
        start = threading.Barrier(workers)
        taken: list[int] = []
        guard = threading.Lock()

        def grab(name: str) -> None:
            start.wait()
            item = queue.claim(name)
            if item is not None:
                with guard:
                    taken.append(item.id)

        threads = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(taken) == len(set(taken)) == 8

    def test_nobody_gets_into_the_gap_between_choosing_and_taking(self, project):
        """The interleaving the threads above never actually produce.

        A claim reads which item is next and then writes that it is taken.
        Everything turns on whether a second claimer can act between those two.
        Here one is forced to try, mid-flight, from its own connection: it is
        locked out and times out. With a deferred transaction it strolls in and
        both claimers come away holding the same book.
        """
        intruded: list[object] = []
        Queue(project).add("solaris", "chunk")
        # Built before the claim starts, so what gets locked out is its claim
        # and not the connection it opens to make one.
        other = Queue(project, busy_timeout_ms=50)

        class Interleaved(Queue):
            def _expire(self, conn, now):
                # Expiry runs first in a real claim and issues an UPDATE, which
                # takes the write lock as a side effect. Skipped here so what is
                # under test is the transaction the claim opens, rather than a
                # coincidence upstream of it that a later tidy-up could remove.
                return

            def _next_ready(self, conn, skip_actions=()):
                row = super()._next_ready(conn, skip_actions)
                if row is not None and not intruded:
                    try:
                        intruded.append(other.claim("intruder"))
                    except sqlite3.OperationalError as exc:
                        intruded.append(exc)
                return row

        mine = Interleaved(project).claim("holder")

        assert mine is not None and mine.claim == "holder"
        assert isinstance(intruded[0], sqlite3.OperationalError)
        assert "locked" in str(intruded[0])

    def test_a_claimed_item_is_not_offered_again(self, queue):
        queue.add("solaris", "chunk")
        assert queue.claim("a") is not None
        assert queue.claim("b") is None

    def test_a_paused_item_is_never_claimed(self, queue):
        item = queue.add("solaris", "chunk")
        queue.pause(item.id, "waiting on an encoding decision")
        assert queue.claim("a") is None

    def test_a_claim_records_who_holds_it(self, queue):
        queue.add("solaris", "chunk")
        item = queue.claim("studio-1")
        assert item is not None and item.claim == "studio-1"


class TestAClaimIsALease:
    def test_a_worker_that_died_before_starting_gives_the_item_back(self, queue):
        item = queue.add("solaris", "chunk")
        queue.claim("doomed")
        age(queue, item.id, 300)
        again = queue.claim("healthy")
        assert again is not None and again.id == item.id

    def test_a_running_item_is_never_taken_back_on_time_alone(self, queue):
        # A twenty-hour render is quiet for twenty hours. Handing it to a
        # second worker because of that would be the worst kind of helpful.
        item = queue.add("solaris", "synth")
        queue.claim("w")
        queue.start(item.id, "job1")
        age(queue, item.id, 100_000)
        assert queue.claim("other") is None
        assert queue.require(item.id).status == RUNNING

    def test_an_item_that_keeps_being_abandoned_is_held_rather_than_looped(self, queue):
        item = queue.add("solaris", "chunk")
        for _ in range(MAX_CLAIMS):
            assert queue.claim("crasher") is not None
            age(queue, item.id, 300)
        assert queue.claim("crasher") is None
        assert queue.require(item.id).status == PAUSED

    def test_each_claim_is_counted(self, queue):
        item = queue.add("solaris", "chunk")
        queue.claim("w")
        age(queue, item.id, 300)
        queue.claim("w")
        assert queue.require(item.id).attempts == 2


class TestTheLifecycleRefusesShortcuts:
    def test_starting_something_nobody_claimed_is_refused(self, queue):
        item = queue.add("solaris", "chunk")
        with pytest.raises(QueueError, match="not claimed"):
            queue.start(item.id, "job1")

    def test_finishing_something_that_never_ran_is_refused(self, queue):
        item = queue.add("solaris", "chunk")
        with pytest.raises(QueueError, match="not running"):
            queue.finish(item.id, ok=True)

    def test_retrying_something_that_has_not_failed_is_refused(self, queue):
        item = queue.add("solaris", "chunk")
        with pytest.raises(QueueError, match="nothing to retry"):
            queue.retry(item.id)

    def test_cancelling_a_running_step_points_at_the_job_instead(self, queue):
        item = queue.add("solaris", "synth")
        queue.claim("w")
        queue.start(item.id, "job1")
        with pytest.raises(QueueError, match="cancel its job"):
            queue.cancel(item.id)

    def test_an_unknown_item_says_so(self, queue):
        with pytest.raises(QueueError, match="no queue item"):
            queue.pause(999)

    def test_releasing_returns_a_running_item_to_the_queue(self, queue):
        item = queue.add("solaris", "chunk")
        queue.claim("w")
        queue.start(item.id, "job1")
        queue.release(item.id, "the process was gone with nothing recorded")
        back = queue.require(item.id)
        assert (back.status, back.job_id) == (PENDING, "")


class TestOneBookStoppingDoesNotStopTheBatch:
    def test_pausing_a_book_leaves_the_others_running(self, queue):
        queue.add_plan("solaris", BOOK_STEPS)
        queue.add_plan("eden", BOOK_STEPS)
        held = queue.pause_book("solaris", "the language could not be settled")
        assert held == 3
        assert [i.slug for i in queue.ready()] == ["eden"]

    def test_resuming_puts_the_book_back_in_line(self, queue):
        queue.add_plan("solaris", BOOK_STEPS)
        queue.pause_book("solaris", "encoding")
        assert queue.resume_book("solaris") == 3
        assert [i.action for i in queue.ready()] == ["chunk"]

    def test_a_paused_book_keeps_its_reason(self, queue):
        queue.add_plan("solaris", BOOK_STEPS)
        queue.pause_book("solaris", "two encodings are equally plausible")
        assert "equally plausible" in report(queue)

    def test_pausing_leaves_what_is_already_running_alone(self, queue):
        items = queue.add_plan("solaris", BOOK_STEPS)
        queue.claim("w")
        queue.start(items[0].id, "job1")
        queue.pause_book("solaris", "held")
        assert queue.require(items[0].id).status == RUNNING
        assert queue.require(items[1].id).status == PAUSED

    def test_cancelling_a_book_drops_its_remaining_steps(self, queue):
        queue.add_plan("solaris", BOOK_STEPS)
        assert queue.cancel_book("solaris", "wrong file") == 3
        assert {i.status for i in queue.items(slug="solaris")} == {CANCELLED}


class TestItSurvivesTheServer:
    def test_a_new_queue_on_the_same_root_sees_the_same_work(self, project):
        Queue(project).add_plan("solaris", BOOK_STEPS)
        assert [i.action for i in Queue(project).items()] == \
            ["chunk", "synth", "assemble"]

    def test_a_claim_held_across_a_restart_is_still_held(self, project):
        Queue(project).add("solaris", "chunk")
        Queue(project).claim("before")
        assert Queue(project).claim("after") is None

    def test_the_schema_version_is_recorded(self, project):
        from studio.queue import SCHEMA_VERSION

        assert Queue(project).schema_version == SCHEMA_VERSION

    def test_arguments_come_back_as_they_went_in(self, project):
        Queue(project).add("solaris", "assemble", {"format": "m4b"})
        assert Queue(project).items()[0].args == {"format": "m4b"}


class TestReading:
    def test_an_empty_queue_says_so(self, queue):
        assert report(queue) == "nothing queued"

    def test_the_summary_counts_every_state(self, queue):
        items = queue.add_plan("solaris", BOOK_STEPS)
        queue.claim("w")
        queue.start(items[0].id, "job1")
        counts = queue.summary()
        assert (counts["running"], counts[PENDING], counts["total"]) == (1, 2, 3)

    def test_blocked_by_names_the_step_in_the_way(self, queue):
        items = queue.add_plan("solaris", BOOK_STEPS)
        blocker = queue.blocked_by(items[2])
        assert blocker is not None and blocker.action == "chunk"

    def test_the_first_step_is_blocked_by_nothing(self, queue):
        items = queue.add_plan("solaris", BOOK_STEPS)
        assert queue.blocked_by(items[0]) is None

    def test_items_can_be_filtered_by_book(self, queue):
        queue.add_plan("solaris", BOOK_STEPS)
        queue.add_plan("eden", BOOK_STEPS)
        assert {i.slug for i in queue.items(slug="eden")} == {"eden"}

    def test_a_shared_pause_reason_is_said_once(self, queue):
        queue.add_plan("solaris", BOOK_STEPS)
        queue.pause_book("solaris", "the language could not be settled")
        assert report(queue).count("the language could not be settled") == 1

    def test_no_line_ends_in_whitespace(self, queue):
        queue.add_plan("solaris", BOOK_STEPS)
        assert all(line == line.rstrip() for line in report(queue).splitlines())

    def test_items_can_be_filtered_by_batch(self, queue):
        queue.add_plan("solaris", BOOK_STEPS, batch="tuesday")
        queue.add_plan("eden", BOOK_STEPS)
        assert len(queue.items(batch="tuesday")) == 3
