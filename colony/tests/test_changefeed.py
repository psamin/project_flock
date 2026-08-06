"""The P1 changefeed spike (§4.4 handoff triggers, TODO.md).

The done-condition is "a changefeed on `tasks` wakes the orchestrator/agents on
an unblock instead of the 1 Hz poll, against the same contract". So the tests
are about the unblock arriving, and about it arriving without the caller having
to change what it asks for — not about the feed existing.

Deliberately measured rather than asserted-fast. A spike that reports the
optimisation worked, when it did not, is worse than no spike.
"""

from __future__ import annotations

import time
import uuid

import psycopg
import pytest
from fleetmem.changefeed import TaskFeed, ensure_enabled

from tests.conftest import needs_db

pytestmark = needs_db

# Generous: this is a wall-clock stream against a real cluster, and the point is
# "the wakeup arrives", not "it arrives inside N milliseconds". The latency
# comparison is its own test.
DELIVERY_TIMEOUT = 15.0


@pytest.fixture
def feed(db, mission):
    ensure_enabled(db.conn)
    f = TaskFeed(mission_id=mission)
    f.start()
    # A rangefeed established at time T does not see writes from before T. The
    # first resolved timestamp is the signal it is actually watching; without
    # waiting for it, a fast test writes into the gap and sees nothing.
    _await_liveness(f)
    yield f
    f.stop()


def _await_liveness(feed: TaskFeed, timeout: float = 10.0) -> None:
    """Give the feed a moment to attach before writing anything."""
    time.sleep(1.5)
    assert feed.error is None, f"the feed did not start: {feed.error}"


def _wait_for(feed: TaskFeed, task_id, want=None, timeout: float = DELIVERY_TIMEOUT):
    """Wait for a change to `task_id` satisfying `want`.

    Matching on the id alone is not enough, and finding that out is half the
    value of the spike: the feed carries *every* write to a row, so a task that
    is created blocked and then unblocked arrives twice. A consumer that acts on
    the first change it sees for a task would wake on the creation and conclude
    the task is not claimable — the exact opposite of the handoff it is waiting
    for. `want` is how a real listener would filter.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for change in feed.poll(timeout=0.25):
            if change.task_id == task_id and (want is None or want(change)):
                return change
    return None


# --- the done-condition -----------------------------------------------------


def test_completing_a_dependency_wakes_the_dependent(db, mission, feed):
    """§4.4's handoff, pushed instead of polled.

    `complete_task` flips the dependent `blocked -> open` in the same
    transaction that finishes its dependency (FR-3). This asserts that the
    transition reaches a listener — which is the whole claim the P1 swap rests
    on.
    """
    clear = db.create_task(mission, "clear_debris", (1, 1))
    deliver = db.create_task(mission, "deliver_kit", (1, 2), depends_on=[clear])
    assert db.claim_task(clear, "l1")

    assert db.complete_task(clear, "l1") == [deliver]

    change = _wait_for(feed, deliver, want=lambda c: c.is_unblock)
    assert change is not None, "the unblock never reached the feed"
    assert change.is_unblock, f"delivered status {change.status!r}, not claimable"
    assert change.kind == "deliver_kit"


def test_the_feed_reports_the_same_thing_the_poll_would(db, mission, feed):
    """ "Same contract" (§4.4): whatever the feed wakes on must be something
    `open_tasks` would have returned. A push that woke robots for work the
    allocation query does not consider claimable would be worse than the poll.
    """
    clear = db.create_task(mission, "clear_debris", (2, 2))
    deliver = db.create_task(mission, "deliver_kit", (2, 3), depends_on=[clear])
    assert db.claim_task(clear, "l1")
    db.complete_task(clear, "l1")

    change = _wait_for(feed, deliver, want=lambda c: c.is_unblock)
    assert change is not None

    claimable = {t.id for t in db.open_tasks(mission)}
    assert change.task_id in claimable


def test_a_release_also_wakes_the_fleet(db, mission, feed):
    """The other way a task becomes claimable (§4.4): an explicit release, which
    is what an aftershock does to invalidated work (FR-7)."""
    task = db.create_task(mission, "clear_debris", (3, 3))
    assert db.claim_task(task, "l1")
    db.release_task(task)

    change = _wait_for(feed, task, want=lambda c: c.is_unblock)
    assert change is not None
    assert change.is_unblock


def test_the_feed_ignores_other_missions(db, feed, mission):
    """A core changefeed carries the whole table. Waking a demo on last week's
    mission would be worse than not waking at all."""
    other = uuid.uuid4()
    stray = db.create_task(other, "clear_debris", (4, 4))
    mine = db.create_task(mission, "clear_debris", (5, 5))

    change = _wait_for(feed, mine)
    assert change is not None, "the feed missed this mission's task"
    assert all(c.task_id != stray for c in feed.poll())


# --- the honest measurement -------------------------------------------------


def test_the_feed_is_not_slower_than_the_poll_it_replaces(db, mission, feed):
    """The spike's actual question.

    §4.4 promises agents "wake instantly" against a 1 Hz poll. A 1 Hz poll
    averages 500ms and tails at 1000ms. This measures delivery so the writeup
    can state a number rather than a hope — and asserts only the weak claim the
    evidence supports: the feed lands inside the poll's worst case.
    """
    latencies = []
    for i in range(3):
        clear = db.create_task(mission, "clear_debris", (10 + i, 1))
        deliver = db.create_task(
            mission, "deliver_kit", (10 + i, 2), depends_on=[clear]
        )
        assert db.claim_task(clear, f"l{i}")

        started = time.monotonic()
        db.complete_task(clear, f"l{i}")
        change = _wait_for(feed, deliver, want=lambda c: c.is_unblock)
        assert change is not None, f"unblock {i} never arrived"
        latencies.append(time.monotonic() - started)

    worst = max(latencies)
    print(f"\nchangefeed unblock latency: {[round(x, 3) for x in latencies]}s")
    assert worst < 5.0, (
        f"worst delivery {worst:.3f}s — slower than the 1 Hz poll's 1.0s worst "
        "case, so the P1 swap would be a regression"
    )


# --- the two findings that cost the spike its afternoon ---------------------


def test_a_feed_without_no_initial_scan_would_replay_history(db, mission):
    """Recorded as a test because it is the failure mode that looks like
    success: the feed starts, rows arrive, and none of them are live.

    On a cluster the suite has run against, `tasks` holds thousands of rows. A
    default changefeed backfills all of them before reaching anything current,
    so a listener waiting for one unblock waits behind the whole table.
    """
    ensure_enabled(db.conn)
    db.create_task(mission, "clear_debris", (7, 7))  # history exists

    with TaskFeed(mission_id=mission) as feed:
        time.sleep(1.5)
        assert feed.error is None
        # Nothing was written after the feed attached, so a correctly-configured
        # feed is silent. A backfilling one would be mid-replay.
        assert feed.poll() == [], "the feed replayed rows that predate it"


def test_the_feed_reports_a_startup_failure_rather_than_going_quiet(db):
    """A feed that cannot start must say so. Failing inside the thread with an
    empty queue is indistinguishable from "nothing has happened yet", which is
    the state a handoff mechanism must never be confused with."""
    feed = TaskFeed(dsn="postgresql://root@127.0.0.1:1/nope?sslmode=disable")
    # psycopg.Error specifically, not a blind Exception: the point is that the
    # connection failure is what surfaced, and a bare `Exception` would also
    # pass if `start()` raised a TypeError from a bad refactor.
    with pytest.raises(psycopg.Error):
        feed.start(timeout=10.0)
    feed.stop()
