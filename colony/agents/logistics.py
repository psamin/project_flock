"""Battery and supply logistics, shared by every agent (§3.3, §5.1 lane 2).

A robot that runs its battery down is stranded where it stands — the sim does
not tow it home — so knowing when to break off and go back is part of doing the
job, not a nicety. The same is true of a medic's two supply kits: the third
victim needs a trip to the shelf first.

Both agents own the *movement* home, because a scout flies and a lifter walks
and each already mirrors the sim's landing rule for its own role. What lives
here is the part that is identical for all of them: how much battery getting
home costs, when that stops being affordable, and what to do on arrival.
"""

from __future__ import annotations

from typing import Any

from sim.protocol import Action
from sim.world import MEDIC_KITS, ROLES

# Ticks of battery kept in hand on top of the trip home. Covers a detour around
# a fire that started since the route was planned, plus the tick spent turning
# around. Too small and a robot dies one tile short of the charger, which on the
# demo map is indistinguishable from a bug.
BATTERY_MARGIN_TICKS = 12


def base_tile(world: Any, role: str) -> tuple[int, int] | None:
    """Where this role goes home to — its spawn, inside the staging zone (§3.3)."""
    points = world.map.spawn_points.get(role)
    return (points[0]["x"], points[0]["y"]) if points else None


def ticks_home(world: Any, role: str, here: tuple[int, int]) -> int:
    """Battery needed to get home from here, optimistically.

    Manhattan distance over the role's speed: it ignores walls, so it can
    under-estimate a route around the office block. `BATTERY_MARGIN_TICKS`
    absorbs the difference — and a robot that waits for a *pessimistic* estimate
    spends the whole mission commuting.
    """
    base = base_tile(world, role)
    if base is None:
        return 0
    distance = abs(here[0] - base[0]) + abs(here[1] - base[1])
    return -(-distance // max(1, ROLES[role]["speed"]))  # ceil


def needs_base(world: Any, robot: Any, here: tuple[int, int]) -> bool:
    """Whether this robot should break off and head for base now."""
    if robot.role == "medic" and robot.kits <= 0:
        return True
    return robot.battery <= ticks_home(world, robot.role, here) + BATTERY_MARGIN_TICKS


def service_action(world: Any, robot: Any) -> Action | None:
    """What to do standing at base: charge, restock, or nothing left to do.

    Order matters. A medic with a flat battery and no kits charges first: a
    stranded medic with a full satchel helps nobody, and the restock is two
    ticks it can spend on the way out.
    """
    if not world.at_base(robot.x, robot.y):
        return None
    if robot.battery < ROLES[robot.role]["battery"]:
        return Action.act("recharge", (robot.x, robot.y))
    if robot.role == "medic" and robot.kits < MEDIC_KITS:
        return Action.act("restock", (robot.x, robot.y))
    return None


def is_serviced(world: Any, robot: Any) -> bool:
    """Whether base has nothing further to offer this robot."""
    return service_action(world, robot) is None and world.at_base(robot.x, robot.y)
