"""map.json format and the Aftershock reference map (§4.8, §3.3)."""

import json
from pathlib import Path

import pytest

from world.build_aftershock import build
from world.map_format import (
    DEBRIS, FIRE, MapError, WALL, load_map, parse_map,
)

MAP_PATH = Path(__file__).resolve().parents[1] / "world" / "maps" / "aftershock.json"


@pytest.fixture(scope="module")
def world():
    return load_map(MAP_PATH)


# --- the committed map matches its generator ---------------------------------


def test_committed_map_is_current():
    """If someone edits the generator and forgets `make map`, lane 3 loads a
    stale world. Catch it here rather than in a playtest."""
    assert json.loads(MAP_PATH.read_text()) == build(), "run `make map`"


# --- the Aftershock spec (§3.3) ----------------------------------------------


def test_dimensions_match_the_spec(world):
    assert (world.width, world.height, world.tile_size) == (40, 30, 32)


def test_has_the_four_zones_plus_courtyard(world):
    names = {z["name"] for z in world.zones}
    assert {"staging", "street", "residential", "office", "courtyard"} == names


def test_eight_victims_in_the_specified_access_mix(world):
    victims = world.victims
    assert len(victims) == 8
    mix = {}
    for v in victims:
        mix[v["access"]] = mix.get(v["access"], 0) + 1
    assert mix == {"open": 3, "one_debris": 4, "two_debris": 1}, mix


def test_victim_deadlines_are_in_range(world):
    for v in world.victims:
        assert 400 <= v["vitals_deadline"] <= 700, v


def test_every_victim_stands_on_a_clear_tile(world):
    """The medic has to reach the victim's tile; rubble on it would make the
    victim unstabilizable and the mission unwinnable."""
    for v in world.victims:
        assert world.object_at(v["x"], v["y"]) == "", v
        assert world.ground_at(v["x"], v["y"]) != WALL, v


def test_spawn_points_exist_for_every_role_in_the_stat_block(world):
    assert set(world.spawn_points) == {"scout", "lifter", "medic"}
    assert len(world.spawn_points["scout"]) == 2, "§3.3 lists 2 scout drones"
    assert len(world.spawn_points["lifter"]) == 1
    assert len(world.spawn_points["medic"]) == 1


def test_robots_spawn_on_passable_tiles(world):
    for role, points in world.spawn_points.items():
        for p in points:
            assert world.passable(p["x"], p["y"]), f"{role} spawns inside an obstacle at {p}"


def test_aftershock_fires_at_tick_300(world):
    shock = [e for e in world.escalations if e["kind"] == "aftershock"]
    assert len(shock) == 1
    event = shock[0]
    assert event["tick"] == 300
    assert event["block_tiles"], "the aftershock must re-block cleared corridors"
    assert event["reveal_victims"], "the aftershock reveals one new victim"
    assert event["unstable_tiles"], "the aftershock converts street to unstable"


def test_the_corridors_the_aftershock_blocks_start_clear(world):
    """Re-blocking a tile that was never open is invisible to the viewer — the
    whole point is watching a route the fleet was using get taken away."""
    shock = next(e for e in world.escalations if e["kind"] == "aftershock")
    for tile in shock["block_tiles"]:
        assert world.passable(tile["x"], tile["y"]), (
            f"aftershock blocks ({tile['x']},{tile['y']}), which is already impassable"
        )


def test_fire_is_seeded_somewhere(world):
    assert any(FIRE in row for row in world.objects), "no fire to spread"


# --- movement rules (§3.3 stat blocks) ---------------------------------------


def test_scouts_fly_over_debris_but_ground_robots_do_not(world):
    debris = next(
        (x, y)
        for y in range(world.height)
        for x in range(world.width)
        if world.object_at(x, y) == DEBRIS
    )
    assert world.passable(*debris, flying=True), "scouts fly over debris"
    assert not world.passable(*debris), "ground robots are blocked by debris"


def test_fire_blocks_everyone(world):
    fire = next(
        (x, y)
        for y in range(world.height)
        for x in range(world.width)
        if world.object_at(x, y) == FIRE
    )
    assert not world.passable(*fire, flying=True)
    assert not world.passable(*fire)


def test_out_of_bounds_is_never_passable(world):
    assert not world.passable(-1, 0)
    assert not world.passable(world.width, 0)


def test_mission_metadata_survives_loading(world):
    """The tick server needs the mission length to know when to stop, and the
    seed is what makes a run reproducible (§4.8). Dropping them at load time
    would make the map file lie about what it contains."""
    assert world.mission_length_ticks == 1200
    assert world.seed is not None
    assert world.name == "Aftershock"


def test_an_escalation_after_the_mission_ends_is_rejected(world):
    """An aftershock scheduled past tick 1200 never fires, quietly removing the
    replanning beat the whole demo is built around."""
    data = json.loads(MAP_PATH.read_text())
    data["escalations"][0]["tick"] = data["mission_length_ticks"] + 10
    with pytest.raises(MapError, match="never fire"):
        parse_map(data)


def test_zone_lookup(world):
    assert world.zone_at(2, 2) == "staging"
    assert world.zone_at(*(35, 20)) == "office"


# --- validator ---------------------------------------------------------------


def _minimal() -> dict:
    return {
        "width": 2, "height": 2, "tile_size": 32,
        "layers": {"ground": [["open", "open"], ["open", "open"]],
                   "objects": [["", ""], ["", ""]]},
        "zones": [], "spawn_points": {}, "victims": [], "escalations": [],
    }


def test_minimal_map_is_valid():
    parse_map(_minimal())


@pytest.mark.parametrize("key", ["width", "height", "tile_size", "layers", "zones",
                                 "spawn_points", "victims", "escalations"])
def test_missing_key_is_rejected(key):
    data = _minimal()
    del data[key]
    with pytest.raises(MapError, match=key):
        parse_map(data)


def test_wrong_row_count_is_rejected():
    data = _minimal()
    data["layers"]["ground"] = [["open", "open"]]
    with pytest.raises(MapError, match="1 rows, expected 2"):
        parse_map(data)


def test_unknown_tile_is_rejected():
    data = _minimal()
    data["layers"]["ground"][0][0] = "lava"
    with pytest.raises(MapError, match="lava"):
        parse_map(data)


def test_debris_in_the_ground_layer_is_rejected():
    """Terrain and objects are separate layers; putting debris in ground would
    silently disable the lifter's clear mechanic."""
    data = _minimal()
    data["layers"]["ground"][0][0] = DEBRIS
    with pytest.raises(MapError):
        parse_map(data)


def test_out_of_bounds_victim_is_rejected():
    data = _minimal()
    data["victims"] = [{"x": 99, "y": 0, "vitals_deadline": 500}]
    with pytest.raises(MapError, match="outside"):
        parse_map(data)


def test_victim_deadline_outside_the_spec_range_is_rejected():
    data = _minimal()
    data["victims"] = [{"x": 0, "y": 0, "vitals_deadline": 50}]
    with pytest.raises(MapError, match="400-700"):
        parse_map(data)


@pytest.mark.parametrize("bad", [None, "1200", 12.5, True])
def test_non_integer_mission_length_gives_a_map_error_not_a_crash(bad):
    """The validator exists so a bad map fails with a readable message. A JSON
    string or null here used to raise TypeError out of a comparison instead."""
    data = _minimal()
    data["mission_length_ticks"] = bad
    with pytest.raises(MapError, match="mission_length_ticks"):
        parse_map(data)


@pytest.mark.parametrize("bad", [None, "aftershock", 12.5])
def test_non_integer_escalation_tick_is_rejected(bad):
    data = _minimal()
    data["escalations"] = [{"tick": bad, "kind": "aftershock"}]
    with pytest.raises(MapError, match="tick"):
        parse_map(data)


@pytest.mark.parametrize("field,bad", [("seed", "abc"), ("name", 7), ("description", [])])
def test_bad_metadata_types_are_rejected(field, bad):
    data = _minimal()
    data[field] = bad
    with pytest.raises(MapError, match=field):
        parse_map(data)


def test_loaded_map_does_not_alias_the_source_data():
    """The parsed map used to hold references into the caller's dict, so
    mutating either silently changed the other."""
    data = _minimal()
    data["victims"] = [{"x": 0, "y": 0, "vitals_deadline": 500}]
    world = parse_map(data)

    data["victims"][0]["x"] = 99
    assert world.victims[0]["x"] == 0

    world.zones.append({"name": "injected"})
    assert data["zones"] == []


def test_unknown_spawn_role_is_rejected():
    data = _minimal()
    data["spawn_points"] = {"tank": [{"x": 0, "y": 0}]}
    with pytest.raises(MapError, match="tank"):
        parse_map(data)


def test_zone_outside_the_map_is_rejected():
    data = _minimal()
    data["zones"] = [{"name": "big", "x": 0, "y": 0, "width": 99, "height": 1}]
    with pytest.raises(MapError, match="bounds"):
        parse_map(data)
