"""A* over the belief map (§4.3). The rescue chain cannot route without it."""

import pytest

from agents.pathing import direction_towards, find_path
from sim.world import World
from world.map_format import DEBRIS, EMPTY, WALL, load_map, parse_map
from tests.test_map import MAP_PATH


def _grid(width=10, height=10, walls=(), debris=()):
    data = {
        "width": width, "height": height, "tile_size": 32,
        "layers": {
            "ground": [["open"] * width for _ in range(height)],
            "objects": [[EMPTY] * width for _ in range(height)],
        },
        "zones": [], "spawn_points": {}, "victims": [], "escalations": [],
    }
    for x, y in walls:
        data["layers"]["ground"][y][x] = WALL
    for x, y in debris:
        data["layers"]["objects"][y][x] = DEBRIS
    return World(parse_map(data), seed=0)


def _walkable(world):
    return lambda p: world.passable(p[0], p[1])


# --- basics ------------------------------------------------------------------


def test_a_straight_route_is_the_short_one():
    world = _grid()
    route = find_path((0, 0), (4, 0), _walkable(world))
    assert route == [(1, 0), (2, 0), (3, 0), (4, 0)]


def test_the_starting_tile_is_not_part_of_the_route():
    """The caller is standing there; including it would waste a tick moving
    nowhere."""
    world = _grid()
    assert (0, 0) not in find_path((0, 0), (3, 0), _walkable(world))


def test_already_there_is_an_empty_route():
    world = _grid()
    assert find_path((2, 2), (2, 2), _walkable(world)) == []


def test_a_wall_is_routed_around():
    # A full-height wall at x=5 with one gap at y=9.
    walls = [(5, y) for y in range(9)]
    world = _grid(walls=walls)
    route = find_path((0, 0), (9, 0), _walkable(world))

    assert route is not None
    assert route[-1] == (9, 0)
    assert (5, 9) in route, "the only gap in the wall was not used"
    assert all(world.passable(x, y) for x, y in route)


def test_no_route_returns_none():
    """A victim sealed behind a solid wall must report unreachable rather than
    hang the agent."""
    walls = [(5, y) for y in range(10)]
    world = _grid(walls=walls)
    assert find_path((0, 0), (9, 0), _walkable(world)) is None


def test_every_step_is_adjacent_to_the_last():
    walls = [(3, y) for y in range(8)]
    world = _grid(walls=walls)
    route = find_path((0, 0), (9, 9), _walkable(world))
    previous = (0, 0)
    for step in route:
        assert abs(step[0] - previous[0]) + abs(step[1] - previous[1]) == 1
        previous = step


# --- adjacency goals (what work verbs need) ----------------------------------


def test_an_adjacent_goal_stops_beside_the_target():
    """A lifter clears debris it stands *next to*. Routing onto the debris tile
    is impossible, so without this every clear_debris route is unsolvable."""
    world = _grid(debris=[(5, 5)])
    route = find_path((0, 5), (5, 5), _walkable(world), goal_is_adjacent=True)

    assert route is not None
    assert route[-1] != (5, 5)
    assert abs(route[-1][0] - 5) + abs(route[-1][1] - 5) == 1


def test_an_adjacent_goal_is_already_satisfied_when_standing_beside_it():
    world = _grid(debris=[(5, 5)])
    assert find_path((4, 5), (5, 5), _walkable(world), goal_is_adjacent=True) == []


def test_debris_ringed_by_walls_is_unreachable():
    world = _grid(walls=[(4, 5), (6, 5), (5, 4), (5, 6)], debris=[(5, 5)])
    assert find_path((0, 0), (5, 5), _walkable(world), goal_is_adjacent=True) is None


# --- costs -------------------------------------------------------------------


def test_unstable_ground_is_avoided_when_a_cheaper_way_exists():
    """§3.3 halves speed on unstable tiles, so crossing one costs double — but
    it stays passable, since a short unstable route can still beat a long
    detour."""
    world = _grid(width=6, height=3)
    for x in range(6):
        world.ground[1][x] = "unstable"

    cost = lambda p: 2 if world.ground[p[1]][p[0]] == "unstable" else 1
    route = find_path((0, 0), (5, 0), _walkable(world), cost=cost)

    assert all(world.ground[y][x] != "unstable" for x, y in route)


def test_a_short_unstable_route_still_beats_a_long_detour():
    world = _grid(width=9, height=9)
    walls = [(4, y) for y in range(9) if y != 4]
    for x, y in walls:
        world.ground[y][x] = WALL
    world.ground[4][4] = "unstable"

    cost = lambda p: 2 if world.ground[p[1]][p[0]] == "unstable" else 1
    route = find_path((0, 4), (8, 4), _walkable(world), cost=cost)

    assert route is not None and (4, 4) in route


# --- robustness --------------------------------------------------------------


def test_the_search_is_bounded():
    """An unreachable goal on a big open map must cost a bounded amount of work
    per tick, not a full sweep."""
    world = _grid(width=40, height=30, walls=[(x, 10) for x in range(40)])
    assert find_path((0, 0), (39, 29), _walkable(world), max_expansions=50) is None


def test_routes_are_deterministic():
    """Same world, same route — the golden demo run depends on it (§4.8)."""
    world = _grid(walls=[(5, y) for y in range(9)])
    first = find_path((0, 0), (9, 9), _walkable(world))
    second = find_path((0, 0), (9, 9), _walkable(world))
    assert first == second


def test_it_routes_on_the_real_map():
    """The demo map, not a fixture: staging to the far side of the office."""
    world = World(load_map(MAP_PATH), seed=0)
    route = find_path((2, 2), (35, 20), lambda p: world.passable(p[0], p[1], flying=True))
    assert route is not None and route[-1] == (35, 20)


def test_off_map_tiles_are_never_entered():
    world = _grid()
    route = find_path((0, 0), (9, 9), _walkable(world))
    assert all(0 <= x < 10 and 0 <= y < 10 for x, y in route)


# --- direction helper --------------------------------------------------------


@pytest.mark.parametrize("there,expected", [
    ((1, 0), "e"), ((-1, 0), "w"), ((0, 1), "s"), ((0, -1), "n"),
])
def test_direction_towards_an_adjacent_tile(there, expected):
    assert direction_towards((0, 0), there) == expected


def test_direction_towards_a_non_adjacent_tile_is_none():
    """Better a None the caller must handle than a confident wrong move."""
    assert direction_towards((0, 0), (5, 5)) is None
