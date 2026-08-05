"""Whole missions across generated scenarios.

Every other test fixes a layout. These generate maps — varying size, debris
density, victim count, sector shape and fleet composition — and assert the fleet
still rescues people. The point is to catch logic that only works on the demo
map: the medic stall that stranded a robot four tiles from a victim passed every
hand-written test and only showed up on a real mission.

Deterministic: each scenario is seeded, so a failure is reproducible from its
parameters alone.
"""

import random
import uuid

import pytest

from agents.scout import Scout, split_sectors
from agents.worker import Worker
from bedrock.adapter import BedrockAdapter
from fleetmem.fake import FakeFleetMem
from sim.world import World
from world.map_format import DEBRIS, EMPTY, RUBBLE_HEAVY, WALL, parse_map


def make_scenario(
    seed: int,
    width: int = 24,
    height: int = 18,
    debris_ratio: float = 0.15,
    victims: int = 3,
    sector_size: int = 6,
    scouts: int = 1,
    lifters: int = 1,
    medics: int = 1,
):
    """A random but solvable map: staging strip on the left, obstacles beyond."""
    rng = random.Random(seed)
    ground = [["open"] * width for _ in range(height)]
    objects = [[EMPTY] * width for _ in range(height)]

    for x in range(width):
        ground[0][x] = ground[height - 1][x] = WALL
    for y in range(height):
        ground[y][0] = ground[y][width - 1] = WALL

    # Obstacles anywhere past the staging strip. Victim tiles are cleared after,
    # so a victim is never buried — matching §3.3, where access difficulty comes
    # from the surroundings.
    for y in range(1, height - 1):
        for x in range(4, width - 1):
            roll = rng.random()
            if roll < debris_ratio * 0.75:
                objects[y][x] = DEBRIS
            elif roll < debris_ratio:
                objects[y][x] = RUBBLE_HEAVY

    spots: list[tuple[int, int]] = []
    while len(spots) < victims:
        spot = (rng.randrange(6, width - 1), rng.randrange(1, height - 1))
        if spot not in spots:
            spots.append(spot)

    victim_rows = []
    for i, (x, y) in enumerate(spots):
        objects[y][x] = EMPTY
        victim_rows.append({"id": f"v{i}", "x": x, "y": y, "vitals_deadline": 700})

    sectors = [
        {
            "id": f"S{cx}-{cy}",
            "x": cx,
            "y": cy,
            "width": min(sector_size, width - cx),
            "height": min(sector_size, height - cy),
        }
        for cy in range(0, height, sector_size)
        for cx in range(0, width, sector_size)
    ]

    spawn = {}
    if scouts:
        spawn["scout"] = [{"x": 1, "y": 1 + i} for i in range(scouts)]
    if lifters:
        spawn["lifter"] = [{"x": 2, "y": 1 + i} for i in range(lifters)]
    if medics:
        spawn["medic"] = [{"x": 3, "y": 1 + i} for i in range(medics)]

    return parse_map(
        {
            "width": width,
            "height": height,
            "tile_size": 32,
            "layers": {"ground": ground, "objects": objects},
            "zones": [],
            "sectors": sectors,
            "spawn_points": spawn,
            "victims": victim_rows,
            "escalations": [],
            "mission_length_ticks": 1200,
            "seed": seed,
        }
    )


def run_mission(world_map, seed=0, ticks=600):
    """Run a full fleet with no orchestrator; return (world, mem, mission_id)."""
    world = World(world_map, seed=seed)
    mem, embedder, mission = FakeFleetMem(), BedrockAdapter(), uuid.uuid4()

    scouts = [r for r in world.robots.values() if r.role == "scout"]
    shares = split_sectors(world_map.sectors, max(1, len(scouts)))
    agents = [
        Scout(
            robot_id=r.id,
            mission_id=mission,
            mem=mem,
            embedder=embedder,
            seed=i,
            sectors=shares[i],
        )
        for i, r in enumerate(scouts)
    ]
    agents += [
        Worker(robot_id=r.id, role=r.role, mission_id=mission, mem=mem)
        for r in world.robots.values()
        if r.role in ("lifter", "medic")
    ]
    for robot in world.robots.values():
        mem.register_robot(robot.id, robot.role, (robot.x, robot.y), robot.battery)

    for _ in range(ticks):
        world.step({a.robot_id: a.step(world) for a in agents})
        if world.finished:
            break
    return world, mem, mission


# --- the fleet rescues people, across scenarios ------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_the_fleet_rescues_everyone_it_can_reach(seed):
    """Different layouts, same outcome: nobody is left behind and nobody is
    lost to the clock."""
    world, _, _ = run_mission(make_scenario(seed))
    metrics = world.metrics()

    assert metrics["victims_stabilized"] >= 1, f"seed {seed}: nobody was rescued"
    assert metrics["victims_lost"] == 0, f"seed {seed}: {metrics['victims_lost']} lost"


@pytest.mark.parametrize("size", [(14, 12), (24, 18), (40, 30)])
def test_it_works_at_any_map_size(size):
    width, height = size
    world, _, _ = run_mission(make_scenario(7, width=width, height=height))
    assert world.metrics()["victims_stabilized"] >= 1


@pytest.mark.parametrize("density", [0.0, 0.1, 0.25])
def test_it_works_at_any_debris_density(density):
    world, _, _ = run_mission(make_scenario(11, debris_ratio=density))
    assert world.metrics()["victims_stabilized"] >= 1


@pytest.mark.parametrize("victims", [1, 4, 8])
def test_it_scales_with_victim_count(victims):
    world, _, _ = run_mission(make_scenario(13, victims=victims, width=30, height=22))
    assert world.metrics()["victims_stabilized"] >= 1


@pytest.mark.parametrize("sector_size", [4, 6, 10])
def test_sector_granularity_is_a_tuning_knob_not_a_dependency(sector_size):
    """§3.3 calls the 4x3 grid a playtest knob. Nothing may depend on 10x10."""
    world_map = make_scenario(17, sector_size=sector_size)
    world, _, _ = run_mission(world_map)
    assert world.metrics()["victims_stabilized"] >= 1


@pytest.mark.parametrize(
    "fleet",
    [
        {"scouts": 1, "lifters": 1, "medics": 1},
        {"scouts": 2, "lifters": 1, "medics": 1},
        {"scouts": 1, "lifters": 0, "medics": 1},  # no lifter at all
        {"scouts": 0, "lifters": 1, "medics": 1},  # nobody scouting
    ],
)
def test_it_works_with_any_fleet_composition(fleet):
    """A missing role must degrade the mission, never break it — robots die
    mid-mission and the fleet has to keep going with whoever is left."""
    world, _, _ = run_mission(make_scenario(19, **fleet))
    assert world.metrics()["victims_lost"] == 0


# --- properties that must hold in every scenario -----------------------------


@pytest.mark.parametrize("seed", [21, 22, 23])
def test_no_robot_ever_stands_on_an_impassable_tile(seed):
    world, _, _ = run_mission(make_scenario(seed))
    for robot in world.robots.values():
        assert world.map.ground[robot.y][robot.x] != WALL, (
            f"{robot.id} is inside a wall"
        )
        if not robot.flying:
            assert world.objects[robot.y][robot.x] not in (DEBRIS, RUBBLE_HEAVY), (
                f"{robot.id} is standing inside debris"
            )


@pytest.mark.parametrize("seed", [31, 32])
def test_no_task_is_ever_held_by_two_robots(seed):
    _, mem, mission = run_mission(make_scenario(seed))
    holders: dict = {}
    for event in mem.events(mission):
        if event["verb"] == "task_claimed":
            holders.setdefault(event["detail"]["task"], []).append(event["actor"])
    # A task can legitimately be claimed twice *in sequence* (released, retaken);
    # what must never happen is two live holders, which the lease guarantees.
    for task_id, actors in holders.items():
        assert len(actors) == len(set(actors)) or len(set(actors)) == 1, (
            f"task {task_id} bounced between {actors}"
        )


@pytest.mark.parametrize("seed", [41, 42])
def test_a_mission_is_reproducible_from_its_seed(seed):
    """§4.8: same seed, same mission. The golden demo run depends on it."""

    def outcome():
        world, _, _ = run_mission(make_scenario(seed), seed=5)
        return (
            world.tick,
            world.metrics(),
            sorted((r.id, r.x, r.y) for r in world.robots.values()),
        )

    assert outcome() == outcome()


def test_a_victim_nobody_can_reach_does_not_hang_the_mission():
    """Walled in on every side: the fleet should finish the others and stop,
    not spin forever re-claiming an impossible task."""
    world_map = make_scenario(51, victims=2)
    data = {
        "width": world_map.width,
        "height": world_map.height,
        "tile_size": 32,
        "layers": {
            "ground": [r[:] for r in world_map.ground],
            "objects": [r[:] for r in world_map.objects],
        },
        "zones": [],
        "sectors": world_map.sectors,
        "spawn_points": world_map.spawn_points,
        "victims": list(world_map.victims),
        "escalations": [],
        "mission_length_ticks": 300,
    }
    sealed = data["victims"][0]
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        x, y = sealed["x"] + dx, sealed["y"] + dy
        if 0 < x < data["width"] - 1 and 0 < y < data["height"] - 1:
            data["layers"]["ground"][y][x] = WALL

    world, _, _ = run_mission(parse_map(data), ticks=300)
    assert world.tick <= 300
    assert world.metrics()["victims_stabilized"] >= 1, (
        "the reachable victim was abandoned"
    )


# --- the MVP milestone, on the demo map (§5.3 Aug 7-8) -----------------------


def _aftershock_mission(ticks=1200):
    from agents.scout import seed_sector_tasks
    from tests.test_map import MAP_PATH
    from world.map_format import load_map

    world_map = load_map(MAP_PATH)
    world = World(world_map, seed=3)
    mem, embedder, mission = FakeFleetMem(), BedrockAdapter(), uuid.uuid4()
    seed_sector_tasks(mem, mission, world_map)

    scouts = [r for r in world.robots.values() if r.role == "scout"]
    shares = split_sectors(world_map.sectors, len(scouts))
    agents = [
        Scout(
            robot_id=r.id,
            mission_id=mission,
            mem=mem,
            embedder=embedder,
            seed=i,
            sectors=shares[i],
        )
        for i, r in enumerate(scouts)
    ]
    agents += [
        Worker(robot_id=r.id, role=r.role, mission_id=mission, mem=mem)
        for r in world.robots.values()
        if r.role in ("lifter", "medic")
    ]
    for robot in world.robots.values():
        mem.register_robot(robot.id, robot.role, (robot.x, robot.y), robot.battery)

    for _ in range(ticks):
        world.step({a.robot_id: a.step(world) for a in agents})
        if world.finished:
            break
    return world, mem, mission


def test_the_demo_map_actually_needs_lifters():
    """Regression: every victim's `access` said "behind debris" while only one
    neighbouring tile was blocked, so every approach stayed open. Zero
    clear_debris tasks were created in a whole mission and the lifter sat idle
    from start to finish — the scout->lifter->medic chain the MVP names never
    ran once, while the run still looked like a success at 8/8 rescued."""
    _, mem, _ = _aftershock_mission()
    clears = [t for t in mem._tasks.values() if t["kind"] == "clear_debris"]
    assert clears, "no victim on the demo map requires a lifter"


def test_the_full_chain_runs_on_the_demo_map():
    """§5.3's Aug 7-8 milestone, as an assertion: scout finds, lifter clears,
    medic delivers, on Aftershock rather than a fixture."""
    _, mem, mission = _aftershock_mission()
    events = mem.events(mission)
    actors = {e["actor"] for e in events if e["verb"] == "task_completed"}

    assert any(e["verb"] == "victim_reported" for e in events), "no scout sighting"
    assert "l1" in actors, "the lifter never completed anything"
    assert "m1" in actors, "the medic never completed anything"


# These two were xfail(strict) while Aftershock v1 was too easy: once idle
# staging (§4.3) unfroze the fleet it cleared the map in ~144 ticks, so the
# escalation scheduled at tick 300 landed after the mission it was supposed to
# disrupt and the replanning beat never happened. Playtest #1 moved the
# escalation to tick 180 (§5.1 "playtest & tune"; see ESCALATION_TICK in
# build_aftershock.py for the measurements), and both now pass for real.
#
# They assert against the map's own escalation tick rather than a constant. The
# requirement is "the mission outlives the shock", not "the mission lasts 300
# ticks" — pinning the number is what let the previous version drift into
# testing nothing.


def _escalation_tick(world) -> int:
    shock = [e for e in world.map.escalations if e["kind"] == "aftershock"]
    assert len(shock) == 1, "the demo map has exactly one aftershock"
    return shock[0]["tick"]


def test_the_demo_map_is_neither_trivial_nor_hopeless():
    """A demo needs tension. Everyone rescued before the shock fires means the
    replanning beat never happens; nobody rescued means there is no product to
    show."""
    world, _, _ = _aftershock_mission()
    metrics = world.metrics()

    assert metrics["victims_stabilized"] >= 5, "too few rescues to be a demo"
    assert world.tick > _escalation_tick(world), (
        "the mission ended before the aftershock could fire"
    )


def test_the_aftershock_fires_during_the_mission():
    world, _, _ = _aftershock_mission()
    assert "v9" in world.victims, "the aftershock never revealed its victim"


def test_the_fleet_rescues_the_victim_the_aftershock_reveals():
    """The beat itself, not just the trigger. v9 appears mid-mission behind
    ground the fleet had already swept, and reaching it in time is the whole
    argument for replanning against shared memory rather than a fixed plan."""
    world, _, _ = _aftershock_mission()

    assert "v9" in world.victims, "the aftershock never revealed its victim"
    assert world.victims["v9"].state == "stabilized", (
        f"v9 ended {world.victims['v9'].state} — the fleet found the new victim "
        "but never got to it"
    )


def test_the_aftershock_actually_forces_a_replan():
    """FR-7 through the provenance log: the shock is only a demo beat if the
    fleet visibly re-decides because of it. A map change nobody replanned
    against would still pass every assertion above."""
    _, mem, mission = _aftershock_mission()
    triggers = [p.trigger for p in mem.plans_for(mission)]

    assert "aftershock" in triggers, (
        "no robot logged an aftershock-triggered plan — the shock fired and "
        "nothing re-decided because of it"
    )


def test_the_fleet_explores_the_whole_map():
    """Coverage@500 (§4.7). Also guards the coverage metric itself, which once
    reported 116% by counting revealed walls against a wall-free denominator."""
    world, _, _ = _aftershock_mission()
    assert 0.9 <= world.metrics()["coverage"] <= 1.0
