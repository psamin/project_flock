"""The claiming transaction under real contention (§5.1 lane 1, §4.4).

`UPDATE … WHERE status='open' RETURNING id` is the line of SQL the whole
coordination story rests on. Under CockroachDB's serializable isolation exactly
one concurrent caller should win. This proves it rather than assuming it.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from fleetmem.client import CockroachFleetMem
from fleetmem.fake import FakeFleetMem
from tests.conftest import DB_UP, needs_db

RACES = 250
ROBOTS_PER_RACE = 4          # 1,000 claim attempts total


@needs_db
def test_thousand_races_produce_zero_double_claims():
    mission = uuid.uuid4()
    setup = CockroachFleetMem()
    tasks = [setup.create_task(mission, "clear_debris", (i % 40, i % 30)) for i in range(RACES)]

    # One connection per robot — threads sharing a connection would serialize in
    # the driver and prove nothing about the database.
    robots = [f"l{i}" for i in range(ROBOTS_PER_RACE)]
    conns = {r: CockroachFleetMem() for r in robots}

    def attempt(args):
        task_id, robot = args
        return (task_id, robot, conns[robot].claim_task(task_id, robot))

    work = [(t, r) for t in tasks for r in robots]
    with ThreadPoolExecutor(max_workers=ROBOTS_PER_RACE) as pool:
        results = list(pool.map(attempt, work))

    winners: dict = {}
    for task_id, robot, won in results:
        if won:
            winners.setdefault(task_id, []).append(robot)

    doubles = {t: w for t, w in winners.items() if len(w) > 1}
    assert not doubles, f"{len(doubles)} tasks were claimed more than once: {list(doubles)[:3]}"
    assert len(winners) == RACES, f"only {len(winners)}/{RACES} tasks were claimed at all"

    for conn in conns.values():
        conn.close()
    setup.close()


@needs_db
def test_the_winner_is_the_robot_recorded_on_the_task():
    """A claim that returns True but records a different owner would let two
    robots believe they hold the same task."""
    mission = uuid.uuid4()
    mem = CockroachFleetMem()
    task = mem.create_task(mission, "deliver_kit", (14, 9))

    robots = [f"m{i}" for i in range(4)]
    conns = {r: CockroachFleetMem() for r in robots}
    with ThreadPoolExecutor(max_workers=4) as pool:
        won = list(pool.map(lambda r: (r, conns[r].claim_task(task, r)), robots))

    claimants = [r for r, ok in won if ok]
    assert len(claimants) == 1

    row = mem.conn.execute("SELECT claimed_by, status FROM tasks WHERE id = %s", (task,)).fetchone()
    assert row["claimed_by"] == claimants[0]
    assert row["status"] == "claimed"

    for conn in conns.values():
        conn.close()
    mem.close()


def test_the_fake_also_admits_exactly_one_winner():
    """Lanes 2 and 4 develop against the fake, so it has to enforce the same
    invariant or they will write code that only works locally."""
    mem = FakeFleetMem()
    mission = uuid.uuid4()
    tasks = [mem.create_task(mission, "clear_debris") for _ in range(100)]
    robots = [f"l{i}" for i in range(4)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda pair: (pair[0], mem.claim_task(pair[0], pair[1])),
            [(t, r) for t in tasks for r in robots],
        ))

    winners: dict = {}
    for task_id, won in results:
        if won:
            winners[task_id] = winners.get(task_id, 0) + 1
    assert all(count == 1 for count in winners.values())
    assert len(winners) == len(tasks)


@pytest.mark.skipif(not DB_UP, reason="no CockroachDB (make dev)")
def test_claiming_a_blocked_task_is_refused():
    mem = CockroachFleetMem()
    mission = uuid.uuid4()
    clear = mem.create_task(mission, "clear_debris")
    deliver = mem.create_task(mission, "deliver_kit", depends_on=[clear])
    assert mem.claim_task(deliver, "m1") is False
    mem.close()
