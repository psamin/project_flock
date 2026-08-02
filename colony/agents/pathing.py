"""A* over the shared belief map (§4.3, §5.1 lane 2).

Greedy stepping was enough for a scout that flies over debris and only needs to
drift toward unexplored ground. A lifter cannot do that: it walks, the
residential block is dense with debris, and "reduce the distance" walks it into
a wall and leaves it there. The rescue chain needs real routes.

Costs come from the world's tiles. Once beliefs drive planning rather than
ground truth, the caller passes a belief-derived grid instead — the search does
not care where `passable` comes from.
"""

from __future__ import annotations

import heapq
from typing import Callable, Iterable

from sim.protocol import DIRECTIONS

Point = tuple[int, int]

# Unstable ground is half speed (§3.3), so it costs twice as much to cross but
# is not forbidden — a route through it can still beat a long way around.
UNSTABLE_COST = 2
NORMAL_COST = 1


def neighbours(point: Point) -> Iterable[tuple[str, Point]]:
    x, y = point
    for direction, (dx, dy) in DIRECTIONS.items():
        yield direction, (x + dx, y + dy)


def find_path(
    start: Point,
    goal: Point,
    passable: Callable[[Point], bool],
    cost: Callable[[Point], int] | None = None,
    *,
    goal_is_adjacent: bool = False,
    max_expansions: int = 20_000,
) -> list[Point] | None:
    """Cheapest route from `start` to `goal`, or None if there isn't one.

    `goal_is_adjacent` searches for a tile *next to* the goal instead of the goal
    itself — which is what work verbs need, since a lifter clears debris it is
    standing beside and could never stand on. Without it every clear_debris route
    would be unsolvable by construction.

    `max_expansions` bounds the search so an unreachable victim behind a sealed
    wall costs a bounded amount of work per tick rather than sweeping the map.
    """
    if start == goal and not goal_is_adjacent:
        return []

    def reached(point: Point) -> bool:
        if goal_is_adjacent:
            # Distance <= 1, not == 1, to match the sim's rule for work verbs:
            # a robot standing *on* the target can act on it too. Requiring
            # exactly 1 made a speed-2 medic stall two tiles out, because the
            # single adjacent tile is not a position it can stop at and landing
            # on the victim scored no better than staying put. Targets that
            # cannot be stood on — debris, rubble — are impassable anyway, so
            # this never routes a lifter onto its own work.
            return abs(point[0] - goal[0]) + abs(point[1] - goal[1]) <= 1
        return point == goal

    if reached(start):
        return []

    tile_cost = cost or (lambda _: NORMAL_COST)
    # Ties broken by a counter so the heap never compares Points; equal-cost
    # routes then resolve in insertion order, which keeps runs reproducible.
    counter = 0
    frontier: list[tuple[int, int, Point]] = [(0, counter, start)]
    came_from: dict[Point, Point] = {}
    best: dict[Point, int] = {start: 0}
    expansions = 0

    while frontier:
        _, _, current = heapq.heappop(frontier)
        if reached(current):
            return _rebuild(came_from, start, current)

        expansions += 1
        if expansions > max_expansions:
            return None

        for _, nxt in neighbours(current):
            if not passable(nxt):
                continue
            step = best[current] + tile_cost(nxt)
            if step >= best.get(nxt, 1 << 30):
                continue
            best[nxt] = step
            came_from[nxt] = current
            counter += 1
            heapq.heappush(
                frontier,
                (step + abs(nxt[0] - goal[0]) + abs(nxt[1] - goal[1]), counter, nxt),
            )
    return None


def find_move_plan(
    start: Point,
    goal: Point,
    landing: Callable[[Point, str], Point],
    *,
    goal_is_adjacent: bool = False,
    speed: int = 1,
    max_expansions: int = 20_000,
) -> list[str] | None:
    """Cheapest sequence of *moves* from `start`, or None if there isn't one.

    Searching tiles and then walking the result one tile at a time is wrong for
    any robot whose speed exceeds one: a single `move` carries it up to `speed`
    tiles (§3.3), so the tile where a route turns is not a position it can stop
    at. In practice the robot overshoots, replans from the far side, and either
    paces between two tiles forever or — worse — stalls outright, because no
    single-tile step improves its distance.

    So the search runs over moves. Each edge is "issue one move in direction d"
    and lands wherever the world actually puts the robot, which makes the plan
    executable by construction rather than by luck. Speed is a parameter, not an
    assumption, so this works unchanged for a speed-1 lifter and a speed-3 scout.
    """
    def reached(point: Point) -> bool:
        if goal_is_adjacent:
            return abs(point[0] - goal[0]) + abs(point[1] - goal[1]) <= 1
        return point == goal

    if reached(start):
        return []

    def heuristic(point: Point) -> int:
        # Manhattan distance divided by the furthest one move can carry us:
        # admissible, so the search stays optimal.
        gap = abs(point[0] - goal[0]) + abs(point[1] - goal[1])
        return (max(0, gap - (1 if goal_is_adjacent else 0)) + speed - 1) // max(1, speed)

    counter = 0
    frontier: list[tuple[int, int, Point]] = [(heuristic(start), counter, start)]
    came_from: dict[Point, tuple[Point, str]] = {}
    best: dict[Point, int] = {start: 0}
    expansions = 0

    while frontier:
        _, _, current = heapq.heappop(frontier)
        if reached(current):
            return _rebuild_moves(came_from, start, current)

        expansions += 1
        if expansions > max_expansions:
            return None

        for direction in sorted(DIRECTIONS):     # sorted: deterministic ties
            nxt = landing(current, direction)
            if nxt == current:
                continue                          # this move goes nowhere
            steps = best[current] + 1
            if steps >= best.get(nxt, 1 << 30):
                continue
            best[nxt] = steps
            came_from[nxt] = (current, direction)
            counter += 1
            heapq.heappush(frontier, (steps + heuristic(nxt), counter, nxt))
    return None


def _rebuild_moves(
    came_from: dict[Point, tuple[Point, str]], start: Point, end: Point
) -> list[str]:
    moves: list[str] = []
    cursor = end
    while cursor != start:
        cursor, direction = came_from[cursor]
        moves.append(direction)
    moves.reverse()
    return moves


def _rebuild(came_from: dict[Point, Point], start: Point, end: Point) -> list[Point]:
    route = [end]
    while route[-1] != start:
        route.append(came_from[route[-1]])
    route.reverse()
    return route[1:]        # drop the tile we are standing on


def direction_towards(here: Point, there: Point) -> str | None:
    """The move direction that steps from `here` to the adjacent tile `there`."""
    delta = (there[0] - here[0], there[1] - here[1])
    for name, offset in DIRECTIONS.items():
        if offset == delta:
            return name
    return None
