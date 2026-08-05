"""What the renderer reads, asserted server-side (§5.2 contract 3, §4.8).

The browser is the one consumer nothing else tests: a field quietly dropped from
a frame does not fail a Python test, it fails as a blank patch in the demo video,
and usually on the day of recording. So the fields `client/app.js` actually reads
are pinned here.

This is not a schema test for its own sake. Every assertion below corresponds to
something visible: fog needs `explored` and `vision`, bubbles need `bubble`,
the sector overlay needs `world.sectors`, the ON/OFF badge needs `shared_vision`.
"""

import json

import pytest

from sim.mission import build_fleet
from sim.world import World
from tests.test_map import MAP_PATH
from world.map_format import load_map


@pytest.fixture
def world():
    return World(load_map(MAP_PATH), seed=3)


def _snapshot(world):
    return json.loads(json.dumps(world.snapshot().to_json()))


def _diff(world, mem, mission):
    agents = build_fleet(world, mem, mission, seed=3)
    frame = world.step({rid: a.step(world) for rid, a in agents.items()})
    return json.loads(json.dumps(frame.to_json()))


# --- the snapshot: everything the client needs to draw a world at all --------


def test_the_snapshot_carries_a_whole_world(world):
    """Sent once on connect; every later frame is a diff against it (§4.8), so
    anything missing here can never be filled in later."""
    snapshot = _snapshot(world)
    assert snapshot["kind"] == "snapshot"
    got = snapshot["world"]
    for field in ("width", "height", "tile_size", "ground", "objects", "zones"):
        assert field in got, f"the renderer cannot draw without world.{field}"
    assert len(got["ground"]) == got["height"]
    assert len(got["ground"][0]) == got["width"]


def test_the_snapshot_says_which_mode_it_is(world):
    """`shared_vision` is how the client knows to render baseline's private maps
    dimmed and show the badge (§3.3, §4.8). Absent, the two runs look alike —
    and looking alike is the one thing the ON/OFF demo cannot survive."""
    assert "shared_vision" in _snapshot(world)["world"]


def test_the_snapshot_carries_the_sector_grid(world):
    """FR-16's story on screen: the overlay labels each sector with the scout
    holding it."""
    sectors = _snapshot(world)["world"]["sectors"]
    assert sectors and {"id", "x", "y", "width", "height"} <= set(sectors[0])


# --- robots: the floaters layer (§4.8 layer 5) -------------------------------


@pytest.mark.parametrize(
    "field,why",
    [
        ("id", "name tags"),
        ("role", "which sprite to draw"),
        ("x", "position"),
        ("y", "position"),
        ("facing", "which way the sprite faces"),
        ("status", "animation state"),
        ("battery", "the battery meter"),
        ("kits", "the medic's satchel"),
        ("vision", "dimming ground nobody is looking at"),
        ("bubble", "the thought bubble — §3.6's signature"),
    ],
)
def test_every_robot_carries_what_the_renderer_draws(world, field, why):
    for robot in _snapshot(world)["robots"]:
        assert field in robot, f"no {field!r}: {why} breaks"


def test_a_bubble_is_always_a_string(world, fake, mission):
    """Never null. The renderer measures it before drawing, and a null there is
    a TypeError inside requestAnimationFrame — which freezes the page on the
    last good frame and looks exactly like the server died."""
    frame = _diff(world, fake, mission)
    assert all(isinstance(r["bubble"], str) for r in frame["robots"])


def test_a_working_fleet_actually_says_something(world, fake, mission):
    """The field existing is not the same as it being used: lane 2 writes a
    bubble every tick, and if that stops the demo goes quiet without failing."""
    agents = build_fleet(world, fake, mission, seed=3)
    for _ in range(6):
        world.step({rid: a.step(world) for rid, a in agents.items()})
    assert any(r.bubble for r in world.robots.values())


# --- diffs: what changes, and nothing else -----------------------------------


def test_a_diff_sends_tile_changes_not_the_grid(world, fake, mission):
    """§4.8: diffs, not snapshots, four times a second. Re-sending 1,200 tiles
    per tick would dwarf every other field in the frame."""
    frame = _diff(world, fake, mission)
    assert frame["kind"] == "diff"
    assert "world" not in frame
    assert isinstance(frame["tiles_changed"], list)


def test_a_diff_sends_newly_explored_tiles(world, fake, mission):
    """FR-8's fog fills in from these. A delta for the same reason as tiles: the
    explored set grows to the size of the map."""
    frame = _diff(world, fake, mission)
    assert frame["explored"], "no ground was revealed on the first tick"
    assert all(len(t) == 2 for t in frame["explored"])


def test_events_carry_what_the_ticker_prints(world, fake, mission):
    frame = _diff(world, fake, mission)
    for event in frame["events"]:
        assert {"tick", "actor", "verb", "detail"} <= set(event)


def test_every_frame_carries_the_lost_roster(world, fake, mission):
    """§5.1 lane 4's marking, on the wire. Sent whole and on snapshots too:
    a browser that joins mid-mission never saw the transition go past, so an
    edge-triggered field would leave it drawing a dead robot as healthy."""
    assert _snapshot(world)["lost"] == []
    assert _diff(world, fake, mission)["lost"] == []


def test_the_frame_survives_json(world, fake, mission):
    """It goes over a websocket. Anything that is not JSON — a UUID, a set, a
    datetime — takes the whole broadcast down, not just its own field."""
    frame = _diff(world, fake, mission)
    json.dumps(frame)  # raises if not
