"""The scout agent and the slice it closes: sim -> agent -> shared memory (§4.3)."""

import uuid

import pytest

from agents.scout import Scout, split_sectors
from bedrock.adapter import BedrockAdapter
from sim.protocol import IDLE, MOVE
from sim.world import World
from world.map_format import EMPTY, WALL, load_map, parse_map
from tests.test_map import MAP_PATH


@pytest.fixture
def scout_world():
    return World(load_map(MAP_PATH), seed=3)


def _scout(mem, mission, seed=0, sectors=()):
    return Scout(robot_id="s1", mission_id=mission, mem=mem,
                 embedder=BedrockAdapter(), seed=seed, sectors=sectors)


def test_a_scout_reports_what_it_sees_into_shared_memory(mem, mission):
    """The walking skeleton's whole point: a sighting has to leave the agent and
    land in shared memory, not sit in a local variable."""
    data = {
        "width": 20, "height": 20, "tile_size": 32,
        "layers": {"ground": [["open"] * 20 for _ in range(20)],
                   "objects": [[EMPTY] * 20 for _ in range(20)]},
        "zones": [], "spawn_points": {"scout": [{"x": 10, "y": 10}]},
        "victims": [{"id": "v1", "x": 12, "y": 10, "vitals_deadline": 700}],
        "escalations": [],
    }
    world = World(parse_map(data), seed=0)
    scout = _scout(mem, mission)

    scout.step(world)

    beliefs = mem.get_beliefs(mission, kind="victim")
    assert len(beliefs) == 1
    assert beliefs[0].pos == (12, 10)
    assert beliefs[0].payload["victim_id"] == "v1"


def test_a_scout_does_not_re_report_the_same_sighting_every_tick(mem, mission):
    """Without local dedup the gate is hammered with the same observation four
    times a second, and the sighting count becomes meaningless."""
    data = {
        "width": 20, "height": 20, "tile_size": 32,
        "layers": {"ground": [["open"] * 20 for _ in range(20)],
                   "objects": [[EMPTY] * 20 for _ in range(20)]},
        "zones": [], "spawn_points": {"scout": [{"x": 10, "y": 10}]},
        "victims": [{"id": "v1", "x": 10, "y": 11, "vitals_deadline": 700}],
        "escalations": [],
    }
    world = World(parse_map(data), seed=0)
    scout = _scout(mem, mission)

    for _ in range(5):
        action = scout.step(world)
        world.step({"s1": action})

    beliefs = mem.get_beliefs(mission, kind="victim")
    assert len(beliefs) == 1
    assert beliefs[0].sightings == 1, f"re-reported {beliefs[0].sightings} times"


def test_two_scouts_seeing_one_victim_produce_one_belief(mem, mission):
    """The reconcile gate, exercised through the real agent path rather than by
    calling report_observation directly."""
    data = {
        "width": 20, "height": 20, "tile_size": 32,
        "layers": {"ground": [["open"] * 20 for _ in range(20)],
                   "objects": [[EMPTY] * 20 for _ in range(20)]},
        "zones": [], "spawn_points": {"scout": [{"x": 10, "y": 10}, {"x": 12, "y": 10}]},
        "victims": [{"id": "v1", "x": 11, "y": 10, "vitals_deadline": 700}],
        "escalations": [],
    }
    world = World(parse_map(data), seed=0)
    embedder = BedrockAdapter()
    s1 = Scout(robot_id="s1", mission_id=mission, mem=mem, embedder=embedder, seed=0)
    s2 = Scout(robot_id="s2", mission_id=mission, mem=mem, embedder=embedder, seed=1)

    s1.step(world)
    s2.step(world)

    beliefs = mem.get_beliefs(mission, kind="victim")
    assert len(beliefs) == 1, f"one victim became {len(beliefs)} beliefs"
    assert beliefs[0].sightings == 2


def test_a_scout_actually_covers_ground(mem, mission, scout_world):
    """Frontier exploration has to explore. A scout that oscillates between two
    tiles would still pass every other test here."""
    scout = _scout(mem, mission)
    start = (scout_world.robots["s1"].x, scout_world.robots["s1"].y)

    for _ in range(60):
        scout_world.step({"s1": scout.step(scout_world)})

    end = (scout_world.robots["s1"].x, scout_world.robots["s1"].y)
    assert end != start
    assert len(scout.explored) > 200, f"only saw {len(scout.explored)} tiles in 60 ticks"


def _shares(count):
    """Same contiguous split the server uses."""
    return split_sectors(load_map(MAP_PATH).sectors, count)


def _explore(mem, mission, embedder, count, ticks, sectored=True):
    """Run `count` scouts and return (their explored union, the world)."""
    world = World(load_map(MAP_PATH), seed=3)
    scouts = [
        Scout(robot_id=rid, mission_id=mission, mem=mem, embedder=embedder, seed=i,
              sectors=_shares(count)[i] if sectored else ())
        for i, rid in enumerate(["s1", "s2"][:count])
    ]
    for _ in range(ticks):
        world.step({s.robot_id: s.step(world) for s in scouts})
    return scouts, set().union(*[s.explored for s in scouts]), world


# Measured *before* a single scout saturates the 40x30 map; past that, any two
# scouts overlap ~100% and the comparison stops meaning anything. The window
# moved from 20 to 30 ticks when scouts switched to move-space planning: they
# cover ground faster now, so the interesting part of the curve sits later.
# Measured at this window: solo 595 tiles, sectored 984 (1.65x, 47% overlap),
# unsectored 664 (1.12x).
PRE_SATURATION_TICKS = 30


def test_a_second_scout_nearly_doubles_coverage(mem, mission):
    """Regression, and the product claim in miniature.

    Identical nearest-frontier logic from neighbouring spawns made both scouts
    lock together and fly in formation: measured at 1.03x the coverage of a
    single scout with 97% of ground covered twice. Two robots doing one robot's
    work is exactly the duplicated effort this product exists to remove — and on
    the demo map it looked like a rendering bug.
    """
    embedder = BedrockAdapter()
    _, solo, _ = _explore(mem, mission, embedder, 1, PRE_SATURATION_TICKS)
    scouts, together, world = _explore(mem, mission, embedder, 2, PRE_SATURATION_TICKS)

    gain = len(together) / len(solo)
    assert gain > 1.4, f"two scouts covered only {gain:.2f}x what one did"

    a, b = scouts[0].explored, scouts[1].explored
    overlap = len(a & b) / min(len(a), len(b))
    assert overlap < 0.6, f"{overlap:.0%} of explored ground was covered twice"

    assert (world.robots["s1"].x, world.robots["s1"].y) != (
        world.robots["s2"].x, world.robots["s2"].y
    ), "the scouts converged onto one tile"


def test_sector_bias_is_what_produces_the_gain(mem, mission):
    """Guards against the gain coming from somewhere incidental: with sectoring
    switched off, the same two scouts collapse back onto one path."""
    embedder = BedrockAdapter()
    _, sectored, _ = _explore(mem, mission, embedder, 2, PRE_SATURATION_TICKS)
    _, flat, _ = _explore(mem, mission, embedder, 2, PRE_SATURATION_TICKS, sectored=False)
    assert len(sectored) > len(flat) * 1.3


def test_a_scout_boxed_in_by_walls_idles_rather_than_crashing(mem, mission):
    data = {
        "width": 5, "height": 5, "tile_size": 32,
        "layers": {
            "ground": [[WALL] * 5, [WALL] * 5, [WALL, WALL, "open", WALL, WALL],
                       [WALL] * 5, [WALL] * 5],
            "objects": [[EMPTY] * 5 for _ in range(5)],
        },
        "zones": [], "spawn_points": {"scout": [{"x": 2, "y": 2}]},
        "victims": [], "escalations": [],
    }
    world = World(parse_map(data), seed=0)
    scout = _scout(mem, mission)
    assert scout.step(world).kind == IDLE


def test_the_scout_heartbeats_so_it_is_never_mistaken_for_dead(mem, mission, scout_world):
    mem.register_robot("s1", "scout", (2, 2), battery=120)
    scout = _scout(mem, mission)
    scout.step(scout_world)
    assert "s1" not in mem.stale_robots(seconds=10)


def test_the_agent_loop_is_deterministic(mem, mission):
    """Same seed, same world, same action sequence — required for the golden run."""
    def run():
        world = World(load_map(MAP_PATH), seed=11)
        scout = Scout(robot_id="s1", mission_id=uuid.uuid4(), mem=mem,
                      embedder=BedrockAdapter(), seed=5)
        actions = []
        for _ in range(40):
            action = scout.step(world)
            actions.append(action.to_json())
            world.step({"s1": action})
        return actions

    assert run() == run()


def test_a_scout_reports_fire_as_a_hazard(mem, mission):
    data = {
        "width": 20, "height": 20, "tile_size": 32,
        "layers": {"ground": [["open"] * 20 for _ in range(20)],
                   "objects": [[EMPTY] * 20 for _ in range(20)]},
        "zones": [], "spawn_points": {"scout": [{"x": 10, "y": 10}]},
        "victims": [], "escalations": [],
    }
    data["layers"]["objects"][10][12] = "fire"
    world = World(parse_map(data), seed=0)

    _scout(mem, mission).step(world)

    hazards = mem.get_beliefs(mission, kind="hazard")
    assert len(hazards) == 1 and hazards[0].pos == (12, 10)


# --- sector claims (FR-16) ---------------------------------------------------


def _sector_world():
    """A 20x20 map split into four 10x10 sectors."""
    data = {
        "width": 20, "height": 20, "tile_size": 32,
        "layers": {"ground": [["open"] * 20 for _ in range(20)],
                   "objects": [[EMPTY] * 20 for _ in range(20)]},
        "zones": [],
        "sectors": [
            {"id": "A1", "x": 0, "y": 0, "width": 10, "height": 10},
            {"id": "B1", "x": 10, "y": 0, "width": 10, "height": 10},
            {"id": "A2", "x": 0, "y": 10, "width": 10, "height": 10},
            {"id": "B2", "x": 10, "y": 10, "width": 10, "height": 10},
        ],
        "spawn_points": {"scout": [{"x": 2, "y": 2}, {"x": 4, "y": 2}]},
        "victims": [], "escalations": [],
    }
    return World(parse_map(data), seed=0)


def test_a_scout_claims_a_sector_before_exploring(mem, mission):
    from agents.scout import seed_sector_tasks

    world = _sector_world()
    seed_sector_tasks(mem, mission, world.map)
    scout = _scout(mem, mission)

    scout.step(world)

    assert scout.sector_task is not None, "the scout explored without claiming"
    verbs = [e["verb"] for e in mem.events(mission)]
    assert "sector_claimed" in verbs


def test_two_scouts_never_hold_the_same_sector(mem, mission):
    """FR-16's whole point, and it rides on the same claiming transaction that
    stops two lifters taking one debris pile."""
    from agents.scout import seed_sector_tasks

    world = _sector_world()
    seed_sector_tasks(mem, mission, world.map)
    a = Scout(robot_id="s1", mission_id=mission, mem=mem, embedder=BedrockAdapter())
    b = Scout(robot_id="s2", mission_id=mission, mem=mem, embedder=BedrockAdapter(), seed=1)

    a.step(world)
    b.step(world)

    assert a.sector_task is not None and b.sector_task is not None
    assert a.sector_task.id != b.sector_task.id, "both scouts hold one sector"


def test_a_scout_claims_the_nearest_sector(mem, mission):
    """Sectors share a priority, so without distance ordering a scout can fly
    the width of the map past unswept ground."""
    from agents.scout import seed_sector_tasks

    world = _sector_world()
    seed_sector_tasks(mem, mission, world.map)
    scout = _scout(mem, mission)          # spawns at (2, 2), inside A1
    scout.step(world)

    assert scout.sector_task.kind == "explore_sector:A1"


def test_a_swept_sector_is_completed_and_the_next_claimed(mem, mission):
    from agents.scout import seed_sector_tasks

    world = _sector_world()
    seed_sector_tasks(mem, mission, world.map)
    scout = _scout(mem, mission)
    scout.step(world)
    first = scout.sector_task.id

    # Pretend the sector has been swept.
    sector = world.map.sector(scout.sector_task.kind.split(":", 1)[1])
    scout.explored |= {
        (x, y)
        for y in range(sector["y"], sector["y"] + sector["height"])
        for x in range(sector["x"], sector["x"] + sector["width"])
    }
    scout.step(world)

    assert scout.sector_task is not None
    assert scout.sector_task.id != first, "the scout stayed on a finished sector"
    assert "sector_swept" in [e["verb"] for e in mem.events(mission)]


def test_a_dead_scouts_sector_frees_itself(mem, mission):
    """Recovery with no supervisor: the lease lapses and the sector is claimable
    again (§4.4). This is the resilience story, applied to exploration."""
    from agents.scout import seed_sector_tasks

    world = _sector_world()
    tasks = seed_sector_tasks(mem, mission, world.map)
    # s1 claims every sector and then dies, leases already lapsed.
    for task in tasks:
        assert mem.claim_task(task, "s1", lease_seconds=-1) is True
    assert mem.open_tasks(mission), "no sector came back to the pool"

    survivor = Scout(robot_id="s2", mission_id=mission, mem=mem, embedder=BedrockAdapter())
    survivor.step(world)

    assert survivor.sector_task is not None, "the dead scout's sectors were not reclaimed"
    assert survivor.sector_task.id in tasks


def test_a_scout_falls_back_to_static_shares_without_sector_tasks(mem, mission):
    """Baseline mode seeds no sector tasks (§3.3), and small fixtures have no
    sector grid at all. Exploration must still work."""
    world = _sector_world()
    scout = Scout(robot_id="s1", mission_id=mission, mem=mem,
                  embedder=BedrockAdapter(), sectors=("A1",))
    action = scout.step(world)

    assert scout.sector_task is None
    assert action.kind in ("move", "idle")
