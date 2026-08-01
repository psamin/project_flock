"""Sim world, tick pipeline and the action contract (§4.8, §5.2 contract 2)."""

import pytest

from sim.protocol import DIRECTIONS, Action, InvalidAction, StateFrame
from sim.world import FIRE_SPREAD_TICKS, ROLES, World
from world.map_format import DEBRIS, EMPTY, FIRE, WALL, load_map, parse_map
from tests.test_map import MAP_PATH


@pytest.fixture
def world():
    return World(load_map(MAP_PATH), seed=1)


def _flat(width=10, height=10, **extra):
    """A featureless map, so a test failure means the thing under test broke."""
    data = {
        "width": width, "height": height, "tile_size": 32,
        "layers": {
            "ground": [["open"] * width for _ in range(height)],
            "objects": [[EMPTY] * width for _ in range(height)],
        },
        "zones": [], "spawn_points": {}, "victims": [], "escalations": [],
        "mission_length_ticks": 1200,
    }
    data.update(extra)
    return data


# --- contract 2: actions -----------------------------------------------------


def test_move_parses():
    assert Action.parse({"kind": "move", "direction": "n"}).direction == "n"


def test_act_parses():
    action = Action.parse({"kind": "act", "verb": "clear_debris", "target": [3, 4]})
    assert action.verb == "clear_debris" and action.target == (3, 4)


@pytest.mark.parametrize("payload", [
    {"kind": "fly"},
    {"kind": "move", "direction": "up"},
    {"kind": "move"},
    {"kind": "act", "verb": "teleport", "target": [1, 1]},
    {"kind": "act", "verb": "clear_debris"},
    {"kind": "act", "verb": "clear_debris", "target": [1]},
    {"kind": "act", "verb": "clear_debris", "target": ["a", "b"]},
])
def test_malformed_actions_are_refused(payload):
    """A confused agent gets a rejection with a reason, not a traceback in the
    tick loop."""
    with pytest.raises(InvalidAction):
        Action.parse(payload)


def test_action_round_trips_through_json():
    action = Action.act("stabilize", (5, 6))
    assert Action.parse(action.to_json()) == action


# --- movement and validation -------------------------------------------------


@pytest.mark.parametrize("role,expected_x", [("scout", 8), ("lifter", 6), ("medic", 7)])
def test_one_move_advances_a_robot_by_its_speed(role, expected_x):
    """§3.3 gives each role a tiles-per-tick speed. One action per tick keeps
    contract 2 simple; the server applies the role's speed to it."""
    world = World(parse_map(_flat(spawn_points={role: [{"x": 5, "y": 5}]})), seed=0)
    robot_id = f"{role[0]}1"
    world.step({robot_id: Action.move("e")})
    assert (world.robots[robot_id].x, world.robots[robot_id].y) == (expected_x, 5)
    assert world.robots[robot_id].battery == ROLES[role]["battery"] - (expected_x - 5)


def test_a_fast_robot_stops_at_the_first_obstacle():
    """A scout with speed 3 must not jump over a wall two tiles away."""
    data = _flat(spawn_points={"scout": [{"x": 5, "y": 5}]})
    data["layers"]["ground"][5][7] = WALL
    world = World(parse_map(data), seed=0)
    world.step({"s1": Action.move("e")})
    assert (world.robots["s1"].x, world.robots["s1"].y) == (6, 5)


def test_a_robot_cannot_walk_through_a_wall():
    data = _flat(spawn_points={"scout": [{"x": 5, "y": 5}]})
    data["layers"]["ground"][5][6] = WALL
    world = World(parse_map(data), seed=0)

    frame = world.step({"s1": Action.move("e")})

    assert (world.robots["s1"].x, world.robots["s1"].y) == (5, 5)
    assert any(e["verb"] == "action_rejected" for e in frame.events)


def test_a_scout_flies_over_debris_but_a_lifter_does_not():
    data = _flat(spawn_points={"scout": [{"x": 5, "y": 5}], "lifter": [{"x": 5, "y": 7}]})
    data["layers"]["objects"][5][6] = DEBRIS
    data["layers"]["objects"][7][6] = DEBRIS
    world = World(parse_map(data), seed=0)

    world.step({"s1": Action.move("e"), "l1": Action.move("e")})

    assert world.robots["s1"].x > 5, "scout should fly over debris"
    assert (world.robots["l1"].x, world.robots["l1"].y) == (5, 7), "lifter should be blocked"


def test_fire_blocks_everyone():
    data = _flat(spawn_points={"scout": [{"x": 5, "y": 5}]})
    data["layers"]["objects"][5][6] = FIRE
    world = World(parse_map(data), seed=0)
    world.step({"s1": Action.move("e")})
    assert world.robots["s1"].x == 5


def test_two_ground_robots_cannot_share_a_tile():
    data = _flat(spawn_points={"lifter": [{"x": 5, "y": 5}], "medic": [{"x": 7, "y": 5}]})
    world = World(parse_map(data), seed=0)
    world.step({"l1": Action.move("e")})       # -> (6,5)
    frame = world.step({"m1": Action.move("w")})  # would also be (6,5)
    assert (world.robots["m1"].x, world.robots["m1"].y) == (7, 5)
    assert any(e["verb"] == "action_rejected" for e in frame.events)


def test_moving_off_the_map_is_refused():
    world = World(parse_map(_flat(spawn_points={"scout": [{"x": 0, "y": 0}]})), seed=0)
    world.step({"s1": Action.move("w")})
    assert (world.robots["s1"].x, world.robots["s1"].y) == (0, 0)


# --- work verbs --------------------------------------------------------------


def test_clearing_debris_takes_three_ticks_and_only_a_lifter_can_do_it():
    data = _flat(spawn_points={"lifter": [{"x": 5, "y": 5}], "scout": [{"x": 1, "y": 1}]})
    data["layers"]["objects"][5][6] = DEBRIS
    world = World(parse_map(data), seed=0)

    world.step({"l1": Action.act("clear_debris", (6, 5))})
    assert world.objects[5][6] == DEBRIS, "cleared instantly — the work timer did nothing"
    world.step({})
    world.step({})
    assert world.objects[5][6] == EMPTY

    data2 = _flat(spawn_points={"scout": [{"x": 5, "y": 5}]})
    data2["layers"]["objects"][5][6] = DEBRIS
    world2 = World(parse_map(data2), seed=0)
    frame = world2.step({"s1": Action.act("clear_debris", (6, 5))})
    assert any("cannot clear" in e["detail"].get("reason", "") for e in frame.events)


def test_work_on_a_distant_tile_is_refused():
    data = _flat(spawn_points={"lifter": [{"x": 5, "y": 5}]})
    data["layers"]["objects"][5][9] = DEBRIS
    world = World(parse_map(data), seed=0)
    frame = world.step({"l1": Action.act("clear_debris", (9, 5))})
    assert any("not adjacent" in e["detail"].get("reason", "") for e in frame.events)


def test_a_medic_stabilizes_a_victim():
    data = _flat(
        spawn_points={"medic": [{"x": 5, "y": 5}]},
        victims=[{"id": "v1", "x": 6, "y": 5, "vitals_deadline": 700}],
    )
    world = World(parse_map(data), seed=0)
    world.step({"m1": Action.act("stabilize", (6, 5))})
    world.step({})
    assert world.victims["v1"].state == "stabilized"


# --- dynamics ----------------------------------------------------------------


def test_fire_spreads_on_schedule():
    data = _flat(spawn_points={})
    data["layers"]["objects"][5][5] = FIRE
    world = World(parse_map(data), seed=7)

    for _ in range(FIRE_SPREAD_TICKS - 1):
        world.step({})
    before = sum(row.count(FIRE) for row in world.objects)
    frame = world.step({})
    after = sum(row.count(FIRE) for row in world.objects)

    assert before == 1 and after == 2
    assert any(e["verb"] == "fire_spread" for e in frame.events)
    assert frame.tiles_changed, "a spreading fire must show up as a tile diff"


def test_fire_grows_one_tile_at_a_time_not_by_doubling():
    """§3.3 says fire spreads to *an* adjacent tile every 25 ticks. Letting each
    burning tile spread doubles it every event, which covers a 40x30 map in
    about 250 ticks — alight before the tick-300 aftershock, and unplayable."""
    data = _flat(spawn_points={})
    data["layers"]["objects"][5][5] = FIRE
    world = World(parse_map(data), seed=7)
    for _ in range(FIRE_SPREAD_TICKS * 3):
        world.step({})
    assert sum(row.count(FIRE) for row in world.objects) == 4


def test_fire_stays_containable_over_a_full_mission():
    """A sanity bound on the whole demo: the block must not be entirely on fire
    by the time the mission ends."""
    world = World(load_map(MAP_PATH), seed=5)
    for _ in range(world.map.mission_length_ticks):
        world.step({})
    burning = sum(row.count(FIRE) for row in world.objects)
    assert burning < 60, f"{burning} tiles alight by tick {world.tick}"


def test_a_victim_is_lost_when_vitals_run_out():
    data = _flat(victims=[{"id": "v1", "x": 2, "y": 2, "vitals_deadline": 405}])
    world = World(parse_map(data), seed=0)
    for _ in range(404):
        world.step({})
    assert world.victims["v1"].state != "lost"
    world.step({})
    assert world.victims["v1"].state == "lost"


def test_the_aftershock_fires_once_and_changes_the_world(world):
    for _ in range(299):
        world.step({})
    blocked_before = world.passable(6, 19)

    frame = world.step({})   # tick 300

    assert any(e["verb"] == "aftershock" for e in frame.events)
    assert blocked_before and not world.passable(6, 19), "corridor was not re-blocked"
    assert "v9" in world.victims, "the aftershock should reveal a new victim"
    assert frame.tiles_changed

    for _ in range(5):
        later = world.step({})
        assert not any(e["verb"] == "aftershock" for e in later.events), "fired twice"


# --- percepts ----------------------------------------------------------------


def test_a_scout_sees_only_within_its_vision_radius():
    data = _flat(width=40, height=30, spawn_points={"scout": [{"x": 20, "y": 15}]})
    world = World(parse_map(data), seed=0)
    percept = world.percept("s1")
    radius = ROLES["scout"]["vision"]
    assert all(abs(t["x"] - 20) <= radius and abs(t["y"] - 15) <= radius
               for t in percept.tiles)
    assert len(percept.tiles) == (2 * radius + 1) ** 2


def test_seeing_a_victim_marks_it_located_and_logs_it():
    data = _flat(
        spawn_points={"scout": [{"x": 5, "y": 5}]},
        victims=[{"id": "v1", "x": 7, "y": 5, "vitals_deadline": 700},
                 {"id": "v2", "x": 39, "y": 29, "vitals_deadline": 700}],
        width=40, height=30,
    )
    world = World(parse_map(data), seed=0)
    world.tick = 1
    percept = world.percept("s1")

    assert [v["id"] for v in percept.victims] == ["v1"]
    assert world.victims["v1"].state == "located"
    assert world.victims["v2"].state == "unknown", "a victim across the map was 'seen'"
    assert any(e["verb"] == "victim_found" for e in world.events)


# --- frames ------------------------------------------------------------------


def test_the_snapshot_carries_the_whole_world_and_diffs_do_not(world):
    snapshot = world.snapshot()
    assert snapshot.kind == "snapshot"
    assert snapshot.world["width"] == 40 and len(snapshot.world["ground"]) == 30

    frame = world.step({})
    assert frame.kind == "diff"
    assert frame.to_json().get("world") is None, "diff frames must not resend the grid"


def test_a_quiet_tick_produces_no_tile_diff(world):
    frame = world.step({})
    assert frame.tiles_changed == []


# --- determinism (§4.8) ------------------------------------------------------


def test_same_seed_and_actions_give_the_same_mission():
    """The property the golden demo run depends on."""
    def run():
        world = World(load_map(MAP_PATH), seed=42)
        frames = []
        for i in range(60):
            direction = ["e", "s", "w", "n"][i % 4]
            frames.append(world.step({"s1": Action.move(direction)}).to_json())
        return frames

    assert run() == run()


def test_different_seeds_diverge():
    """If the seed did nothing, the determinism test above would be vacuous."""
    def run(seed):
        data = _flat(spawn_points={})
        data["layers"]["objects"][5][5] = FIRE
        world = World(parse_map(data), seed=seed)
        for _ in range(FIRE_SPREAD_TICKS * 4):
            world.step({})
        return [row[:] for row in world.objects]

    assert run(1) != run(999)


def test_the_mission_ends_at_the_map_s_tick_limit():
    world = World(parse_map(_flat(mission_length_ticks=5)), seed=0)
    for _ in range(4):
        world.step({})
        assert not world.finished
    world.step({})
    assert world.finished


# --- regressions from review ------------------------------------------------


def test_percept_events_survive_the_tick_that_follows():
    """Regression: step() cleared `events` at the top of the tick, but agents
    call percept() *before* step() (Mission.tick_once, Scout.step). Every
    victim_found was silently dropped — never logged to fleet memory, never
    shown in the ticker. The old tests missed it by calling percept() without a
    following step()."""
    data = _flat(
        width=20, height=20,
        spawn_points={"scout": [{"x": 10, "y": 10}]},
        victims=[{"id": "v1", "x": 12, "y": 10, "vitals_deadline": 700}],
    )
    world = World(parse_map(data), seed=0)

    world.percept("s1")                       # sees the victim
    frame = world.step({"s1": Action.idle()})  # the tick that follows

    assert any(e["verb"] == "victim_found" for e in frame.events), (
        f"victim_found was discarded: {frame.events}"
    )


def test_events_are_not_delivered_twice():
    """The other half: draining on emit rather than never clearing."""
    data = _flat(
        width=20, height=20,
        spawn_points={"scout": [{"x": 10, "y": 10}]},
        victims=[{"id": "v1", "x": 12, "y": 10, "vitals_deadline": 700}],
    )
    world = World(parse_map(data), seed=0)
    world.percept("s1")

    first = world.step({"s1": Action.idle()})
    second = world.step({"s1": Action.idle()})

    assert any(e["verb"] == "victim_found" for e in first.events)
    assert not any(e["verb"] == "victim_found" for e in second.events), "replayed"


@pytest.mark.parametrize("target", [(-1, 5), (5, -1), (999, 5), (5, 999)])
def test_work_on_an_off_map_target_is_refused(target):
    """Regression: adjacency used absolute differences, so an off-map target
    passed whenever the robot stood at an edge. A negative coordinate then
    indexed from the far side and rewrote a tile across the map; an over-large
    one raised IndexError inside the tick loop, which an illegal action must
    never do."""
    data = _flat(spawn_points={"lifter": [{"x": 0, "y": 5}]})
    world = World(parse_map(data), seed=0)

    frame = world.step({"l1": Action.act("clear_debris", target)})

    assert any("outside the map" in e["detail"].get("reason", "") for e in frame.events)
    assert world.robots["l1"].work_left == 0


def test_an_off_map_target_does_not_corrupt_a_distant_tile():
    data = _flat(spawn_points={"lifter": [{"x": 0, "y": 5}]})
    data["layers"]["objects"][5][9] = DEBRIS
    world = World(parse_map(data), seed=0)
    before = [row[:] for row in world.objects]

    world.step({"l1": Action.act("clear_debris", (-1, 5))})
    for _ in range(5):
        world.step({})

    assert world.objects == before, "an out-of-bounds target changed the world"
