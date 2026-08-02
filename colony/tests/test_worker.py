"""Lifter and medic (§4.3, §5.1 lane 2) and the rescue chain they close.

The headline test is `test_the_full_chain_stabilizes_a_victim`: scout finds a
victim behind debris, a lifter clears it, a medic delivers, victim rescued —
with no robot messaging another and no human involved.
"""

import uuid

import pytest

from agents.scout import Scout
from agents.worker import ROLE_TASKS, SELF_CLAIM_AFTER_TICKS, Worker, allocation_score
from bedrock.adapter import BedrockAdapter
from fleetmem.types import Task
from sim.world import World
from world.map_format import DEBRIS, EMPTY, WALL, parse_map


def _world(width=20, height=20, spawn=None, victims=(), debris=(), walls=()):
    data = {
        "width": width,
        "height": height,
        "tile_size": 32,
        "layers": {
            "ground": [["open"] * width for _ in range(height)],
            "objects": [[EMPTY] * width for _ in range(height)],
        },
        "zones": [],
        "spawn_points": spawn or {},
        "victims": list(victims),
        "escalations": [],
    }
    for x, y in debris:
        data["layers"]["objects"][y][x] = DEBRIS
    for x, y in walls:
        data["layers"]["ground"][y][x] = WALL
    return World(parse_map(data), seed=0)


def _run(world, agents, ticks):
    for _ in range(ticks):
        world.step({a.robot_id: a.step(world) for a in agents})


# --- the chain ---------------------------------------------------------------


def test_the_full_chain_stabilizes_a_victim(mem, mission):
    """The whole product in one test: a victim behind debris gets rescued by two
    robots that never talk to each other. The lifter completing its task is what
    makes the medic's claimable — that is the handoff."""
    world = _world(
        spawn={"lifter": [{"x": 2, "y": 10}], "medic": [{"x": 2, "y": 12}]},
        victims=[{"id": "v1", "x": 10, "y": 10, "vitals_deadline": 700}],
        debris=[(9, 10)],
    )
    mem.register_victim(mission, (10, 10), reported_by="s1", blocked_by=[(9, 10)])

    lifter = Worker(robot_id="l1", role="lifter", mission_id=mission, mem=mem)
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    _run(world, [lifter, medic], 60)

    assert world.objects[10][9] == EMPTY, "the lifter never cleared the debris"
    assert world.victims["v1"].state == "stabilized", "the victim was never rescued"

    verbs = [e["verb"] for e in mem.events(mission)]
    assert "task_claimed" in verbs and "task_completed" in verbs


def test_the_medic_cannot_start_before_the_lifter_finishes(mem, mission):
    """Gating is the point. If the medic could claim early it would walk to a
    victim it cannot reach and stand there while the clock runs down."""
    world = _world(
        spawn={"medic": [{"x": 2, "y": 10}]},
        victims=[{"id": "v1", "x": 10, "y": 10, "vitals_deadline": 700}],
        debris=[(9, 10)],
    )
    mem.register_victim(mission, (10, 10), reported_by="s1", blocked_by=[(9, 10)])

    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)
    _run(world, [medic], 30)

    assert medic.task is None, "the medic claimed a blocked delivery"
    assert world.victims["v1"].state != "stabilized"


def test_a_reachable_victim_needs_no_lifter(mem, mission):
    world = _world(
        spawn={"medic": [{"x": 2, "y": 10}]},
        victims=[{"id": "v1", "x": 10, "y": 10, "vitals_deadline": 700}],
    )
    mem.register_victim(mission, (10, 10), reported_by="s1")

    _run(world, [Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)], 40)
    assert world.victims["v1"].state == "stabilized"


def test_two_victims_are_both_rescued(mem, mission):
    world = _world(
        spawn={"medic": [{"x": 2, "y": 10}]},
        victims=[
            {"id": "v1", "x": 8, "y": 10, "vitals_deadline": 700},
            {"id": "v2", "x": 14, "y": 10, "vitals_deadline": 700},
        ],
    )
    mem.register_victim(mission, (8, 10), reported_by="s1")
    mem.register_victim(mission, (14, 10), reported_by="s1")

    _run(world, [Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)], 80)
    assert [v.state for v in world.victims.values()] == ["stabilized", "stabilized"]


# --- roles -------------------------------------------------------------------


def test_a_role_only_claims_its_own_work(mem, mission):
    """A medic that claimed clear_debris would hold a task it can never perform,
    and the lease would have to expire before anyone else could."""
    world = _world(spawn={"medic": [{"x": 2, "y": 10}]})
    mem.create_task(mission, "clear_debris", (9, 10))

    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)
    _run(world, [medic], 30)

    assert medic.task is None
    assert len(mem.open_tasks(mission)) == 1, "the medic took work it cannot do"


@pytest.mark.parametrize(
    "role,kind", [("lifter", "clear_debris"), ("medic", "deliver_kit")]
)
def test_role_task_mapping_matches_the_stat_blocks(role, kind):
    assert kind in ROLE_TASKS[role]


def test_two_lifters_never_hold_the_same_task(mem, mission):
    """Claiming is transactional (§4.4); this proves the worker loop honours it
    rather than working around it."""
    world = _world(
        spawn={"lifter": [{"x": 2, "y": 10}, {"x": 2, "y": 12}]},
        debris=[(9, 10)],
    )
    mem.create_task(mission, "clear_debris", (9, 10))

    a = Worker(robot_id="l1", role="lifter", mission_id=mission, mem=mem)
    b = Worker(robot_id="l2", role="lifter", mission_id=mission, mem=mem)
    _run(world, [a, b], 2)  # claimed on the first tick; still en route

    holders = [w.robot_id for w in (a, b) if w.task is not None]
    assert len(holders) == 1, f"both lifters hold the task: {holders}"


# --- robustness --------------------------------------------------------------


def test_an_unreachable_task_is_released_rather_than_held(mem, mission):
    """Standing forever on a task nobody can perform starves the fleet: the
    lease would keep being renewed by a robot that is not making progress."""
    world = _world(
        spawn={"lifter": [{"x": 2, "y": 10}]},
        debris=[(10, 10)],
        walls=[(9, 10), (11, 10), (10, 9), (10, 11)],
    )
    task = mem.create_task(mission, "clear_debris", (10, 10))

    lifter = Worker(robot_id="l1", role="lifter", mission_id=mission, mem=mem)
    _run(world, [lifter], 10)

    assert lifter.task is None, "the lifter is still holding unreachable work"
    assert task in {t.id for t in mem.open_tasks(mission)}, "the task was not released"


def test_work_already_done_is_completed_not_repeated(mem, mission):
    """The aftershock, or another robot, can finish a task out from under its
    owner. Re-attempting forever would deadlock the chain behind it."""
    world = _world(spawn={"lifter": [{"x": 8, "y": 10}]})  # no debris at all
    task = mem.create_task(mission, "clear_debris", (9, 10))

    lifter = Worker(robot_id="l1", role="lifter", mission_id=mission, mem=mem)
    _run(world, [lifter], 10)

    assert lifter.task is None
    assert task not in {t.id for t in mem.open_tasks(mission)}, "not marked done"


def test_a_worker_with_nothing_to_do_idles(mem, mission):
    world = _world(spawn={"lifter": [{"x": 2, "y": 10}]})
    lifter = Worker(robot_id="l1", role="lifter", mission_id=mission, mem=mem)
    assert lifter.step(world).kind == "idle"


def test_a_worker_heartbeats_every_tick(mem, mission):
    """Renewal rides on heartbeat (§4.4). A lifter that stops mid-clear has its
    task taken over — correct when it is dead, fatal when it is merely busy."""
    world = _world(spawn={"lifter": [{"x": 2, "y": 10}]})
    mem.register_robot("l1", "lifter", (2, 10), battery=300)

    lifter = Worker(robot_id="l1", role="lifter", mission_id=mission, mem=mem)
    lifter.step(world)

    assert "l1" not in mem.stale_robots(seconds=10)


def test_a_held_task_keeps_its_lease_alive(mem, mission):
    world = _world(spawn={"lifter": [{"x": 2, "y": 10}]}, debris=[(9, 10)])
    task = mem.create_task(mission, "clear_debris", (9, 10))
    lifter = Worker(robot_id="l1", role="lifter", mission_id=mission, mem=mem)

    _run(world, [lifter], 2)  # claimed, walking to the debris
    assert lifter.task is not None

    # Another robot must not be able to take live work.
    assert mem.claim_task(task, "l2") is False


# --- allocation score (§4.4) -------------------------------------------------


def _task(kind="clear_debris", target=(5, 5), priority=1):
    return Task(
        id=uuid.uuid4(),
        mission_id=uuid.uuid4(),
        kind=kind,
        target=target,
        status="open",
        priority=priority,
    )


def test_role_match_dominates_the_score():
    """2.0·role_match is the largest single term for a reason: a distant lifter
    should still beat a nearby medic for clearing debris."""
    world = _world(
        width=40,
        height=30,
        spawn={"lifter": [{"x": 30, "y": 10}], "medic": [{"x": 5, "y": 5}]},
    )
    far_lifter = world.robots["l1"]
    near_medic = world.robots["m1"]
    task = _task("clear_debris", (5, 5))

    assert allocation_score("lifter", far_lifter, task) > allocation_score(
        "medic", near_medic, task
    )


def test_closer_wins_between_equals():
    world = _world(spawn={"lifter": [{"x": 4, "y": 5}, {"x": 18, "y": 5}]})
    task = _task("clear_debris", (5, 5))
    assert allocation_score("lifter", world.robots["l1"], task) > allocation_score(
        "lifter", world.robots["l2"], task
    )


def test_priority_outranks_distance():
    """1.2·priority against a distance term that never exceeds 1.0 — an urgent
    victim across the map beats a routine one next door."""
    world = _world(spawn={"lifter": [{"x": 5, "y": 5}]})
    robot = world.robots["l1"]
    urgent_far = _task("clear_debris", (19, 19), priority=9)
    routine_near = _task("clear_debris", (5, 6), priority=1)
    assert allocation_score("lifter", robot, urgent_far) > allocation_score(
        "lifter", robot, routine_near
    )


def test_a_task_with_no_target_scores_without_crashing():
    world = _world(spawn={"lifter": [{"x": 5, "y": 5}]})
    assert (
        allocation_score("lifter", world.robots["l1"], _task(target=(None, None))) > 0
    )


# --- scout hand-off into the chain ------------------------------------------


def test_a_scout_sighting_leads_to_a_rescue(mem, mission):
    """End to end from sensing: the scout reports, the chain exists, the medic
    finishes the job. No orchestrator in the loop."""
    world = _world(
        spawn={"scout": [{"x": 5, "y": 10}], "medic": [{"x": 2, "y": 10}]},
        victims=[{"id": "v1", "x": 9, "y": 10, "vitals_deadline": 700}],
    )
    scout = Scout(robot_id="s1", mission_id=mission, mem=mem, embedder=BedrockAdapter())
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    _run(world, [scout, medic], 60)

    assert mem.get_beliefs(mission, kind="victim"), "the scout never reported"
    assert world.victims["v1"].state == "stabilized"


def test_with_no_orchestrator_a_robot_claims_immediately(mem, mission):
    """§4.4's decentralized fallback, and today the only path: there is no
    orchestrator pushing assignments, so a robot that waited SELF_CLAIM_AFTER_TICKS
    before helping would leave victims waiting five seconds for nothing."""
    world = _world(spawn={"lifter": [{"x": 2, "y": 10}]}, debris=[(9, 10)])
    mem.create_task(mission, "clear_debris", (9, 10))

    lifter = Worker(robot_id="l1", role="lifter", mission_id=mission, mem=mem)
    lifter.step(world)

    assert lifter.task is not None, "the fleet sat idle with work available"
    assert SELF_CLAIM_AFTER_TICKS > 0  # kept for when one exists
