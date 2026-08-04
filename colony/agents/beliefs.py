"""The shared belief map, as a route planner sees it (§4.3, §5.1 lane 2).

`agents/pathing.py` searches; this decides what the search should believe. The
two are kept apart because they answer different questions — "what is the
cheapest way there" versus "what do we collectively think is in the way" — and
only the second one needs CockroachDB.

What a robot routes around is not what it can see. Fire spreads (§3.3), so the
tile beside a burning one is a bad place to be walking in ten ticks' time, and
the robot that reported that fire is almost always somebody else: a lifter has
vision 2 and is nose-down in rubble. In coordinated mode this map is the fleet's
hazard beliefs, read straight from shared memory; in baseline it is empty,
because a baseline robot has no shared memory to read and no senses of its own
to replace it. That difference is the ON/OFF delta expressed as route quality
rather than as another mode flag.

Deliberately *not* here: any preference for ground the fleet has already
explored. It sounds sensible and it is not — scouts exist to fly at unexplored
ground, and pricing the unknown above the known measured as a 10% coverage loss
at 30 ticks with nothing bought in exchange.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sim.protocol import DIRECTIONS

# Beliefs are re-read on the §4.3 cadence (~1s at 4 Hz), not per tick.
BELIEF_REFRESH_TICKS = 4

# Route costs. A hazard tile is not forbidden — the sim refuses to move onto it
# anyway, and forbidding it here would tell a robot behind a line of fire that
# it is trapped when the fire may be out before it arrives — it is simply
# expensive. Its neighbours cost something too, because that is where fire goes
# next (§3.3: it spreads every 25 ticks).
HAZARD_COST = 12
HAZARD_ADJACENT_COST = 4
NORMAL_COST = 1


@dataclass(frozen=True)
class BeliefMap:
    """Where the fleet believes the dangerous ground is."""

    hazards: frozenset[tuple[int, int]] = frozenset()

    def cost(self, point: tuple[int, int]) -> int:
        if point in self.hazards:
            return HAZARD_COST
        if any(
            (point[0] + dx, point[1] + dy) in self.hazards
            for dx, dy in DIRECTIONS.values()
        ):
            return HAZARD_ADJACENT_COST
        return NORMAL_COST


def load(mem: Any, mission_id: UUID, *, coordinated: bool = True) -> BeliefMap:
    """Read the fleet's hazard beliefs, or none at all in baseline mode.

    Baseline robots keep private world models (§3.3), so the read is skipped
    rather than filtered — a baseline run should not touch shared memory at all,
    or the comparison is measuring a filtered version of coordination instead of
    the absence of it.
    """
    if not coordinated:
        return BeliefMap()
    return BeliefMap(
        hazards=frozenset(b.pos for b in mem.get_beliefs(mission_id, kind="hazard"))
    )
