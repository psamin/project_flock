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


@needs_db
def test_thousand_expired_lease_takeover_races_produce_zero_double_claims():
    """§5.4 and the lane 1 checklist both call for this alongside the open-claim
    races, and it is the harder half: recovery has no lock, no sweep and no
    coordinator — every robot in the fleet may notice a dead lease at the same
    instant. Exactly one must win, or two robots dig at the same rubble while a
    victim's clock runs down.
    """
    mission = uuid.uuid4()
    setup = CockroachFleetMem()
    tasks = [setup.create_task(mission, "clear_debris", (i % 40, i % 30)) for i in range(RACES)]

    # Claim every task with an already-dead lease, standing in for a fleet of
    # robots that died mid-task.
    for task in tasks:
        assert setup.claim_task(task, "ghost", lease_seconds=-1) is True

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
    assert not doubles, f"{len(doubles)} dead-lease tasks were taken over twice"
    assert len(winners) == RACES, f"only {len(winners)}/{RACES} were recovered at all"
    assert all("ghost" not in w for w in winners.values())

    for conn in conns.values():
        conn.close()
    setup.close()


@needs_db
def test_a_live_lease_cannot_be_stolen():
    """The other half of the guarantee. If a healthy robot's work could be taken
    because someone else asked, recovery would be indistinguishable from theft."""
    mission = uuid.uuid4()
    mem = CockroachFleetMem()
    task = mem.create_task(mission, "clear_debris", (1, 1))

    assert mem.claim_task(task, "l1") is True          # 15s lease
    assert mem.claim_task(task, "l2") is False, "a live lease was stolen"

    row = mem.conn.execute(
        "SELECT claimed_by, lease_expires_at > now() AS alive FROM tasks WHERE id = %s",
        (task,),
    ).fetchone()
    assert row["claimed_by"] == "l1" and row["alive"]
    mem.close()


@needs_db
def test_renewing_a_lease_keeps_the_task():
    """Heartbeat renewal is what separates "slow" from "dead" (§4.4).

    Uses a unique robot id: renewal is scoped by `claimed_by` alone, exactly as
    §4.4's SQL specifies, so a shared id would sweep up every other mission's
    rows in this database.
    """
    mission = uuid.uuid4()
    robot = f"l-{uuid.uuid4().hex[:8]}"
    mem = CockroachFleetMem()
    task = mem.create_task(mission, "clear_debris", (2, 2))

    mem.claim_task(task, robot, lease_seconds=-1)      # already expired
    assert mem.renew_leases(robot, lease_seconds=60) == 1
    assert mem.claim_task(task, "l2") is False, "a renewed lease was still stolen"
    mem.close()


@needs_db
def test_heartbeat_renews_leases():
    """The agent loop calls heartbeat(), not renew_leases() — if heartbeat did
    not renew, every robot would lose its work every 15 seconds."""
    mission = uuid.uuid4()
    mem = CockroachFleetMem()
    mem.register_robot("l1", "lifter", (2, 4), battery=300)
    task = mem.create_task(mission, "clear_debris", (3, 3))

    mem.claim_task(task, "l1", lease_seconds=-1)
    mem.heartbeat("l1", lease_seconds=60)
    assert mem.claim_task(task, "l2") is False
    mem.close()


@needs_db
def test_a_completed_task_is_never_taken_over():
    """A finished task with a stale lease must not look like abandoned work."""
    mission = uuid.uuid4()
    mem = CockroachFleetMem()
    task = mem.create_task(mission, "clear_debris", (4, 4))

    mem.claim_task(task, "l1", lease_seconds=-1)
    mem.complete_task(task, "l1")
    assert mem.claim_task(task, "l2") is False
    mem.close()


@needs_db
def test_released_work_returns_to_the_pool():
    """Explicit release (aftershock invalidation, FR-7) clears the lease too."""
    mission = uuid.uuid4()
    mem = CockroachFleetMem()
    task = mem.create_task(mission, "clear_debris", (5, 5))

    mem.claim_task(task, "l1")
    mem.release_task(task)
    assert task in {t.id for t in mem.open_tasks(mission)}
    assert mem.claim_task(task, "l2") is True
    mem.close()


def test_the_fake_enforces_lease_takeover_identically():
    """Lanes 2 and 4 build recovery behaviour against the fake. If its lease
    semantics differ, they will write code that only works locally."""
    mem = FakeFleetMem()
    mission = uuid.uuid4()
    task = mem.create_task(mission, "clear_debris")

    assert mem.claim_task(task, "l1") is True
    assert mem.claim_task(task, "l2") is False          # live lease

    mem.renew_leases("l1", lease_seconds=-1)            # l1 goes silent; lease lapses
    assert mem.claim_task(task, "l2") is True, "the fake ignored an expired lease"

    mem.renew_leases("l2", lease_seconds=60)
    assert mem.claim_task(task, "l3") is False


def test_the_fake_admits_one_winner_on_an_expired_lease_under_contention():
    mem = FakeFleetMem()
    mission = uuid.uuid4()
    tasks = [mem.create_task(mission, "clear_debris") for _ in range(100)]
    for task in tasks:
        mem.claim_task(task, "ghost", lease_seconds=-1)

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
