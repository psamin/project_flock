"""Scout agent — the §4.3 loop, rules only for now.

    sense -> sync -> think -> act -> report

Bedrock planning is deliberately absent at this stage: §4.3 restricts LLM calls
to plan boundaries (task selection, replan-on-aftershock, conflict resolution),
and until there are tasks to choose between there is no boundary to call at.
Frontier exploration is the §4.3 "role behaviour" the LLM would be choosing
*among*, so it is needed either way.

The agent holds no shared world model of its own. What it sees goes to fleetmem
and comes back as shared belief — that is the whole product thesis, so the
skeleton does it properly from the first tick.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sim.protocol import DIRECTIONS, Action
from sim.world import Percept, World
from world.map_format import EMPTY, WALL

# Kinds the scout reports into shared memory.
VICTIM, HAZARD = "victim", "hazard"


@dataclass
class Scout:
    """One scout drone. Deterministic given its seed."""

    robot_id: str
    mission_id: UUID
    mem: Any                       # CockroachFleetMem or FakeFleetMem
    embedder: Any = None           # BedrockAdapter; None skips embeddings
    seed: int = 0

    # Sector assignment (§4.3 "frontier-exploration bias"). Two scouts running
    # identical nearest-frontier logic from neighbouring spawns lock together and
    # fly in formation, seeing the same tiles twice — the duplicated exploration
    # this product exists to remove, on display in the demo. Each scout prefers
    # its own vertical band of the map and only leaves it once that band is
    # exhausted.
    sector: int = 0
    sector_count: int = 1

    explored: set[tuple[int, int]] = field(default_factory=set)
    reported: set[tuple[str, int, int]] = field(default_factory=set)
    frontier_target: tuple[int, int] | None = field(default=None)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    # --- the loop ---------------------------------------------------------

    def step(self, world: World) -> Action:
        """One iteration: sense, sync, think, act, report.

        Returns the action for the sim to apply. Reporting happens here rather
        than after the action so a belief is never lost if the mission ends on
        this tick.
        """
        percept = world.percept(self.robot_id)      # sense
        self._sync(percept)                          # sync: local -> shared memory
        action = self._think(world, percept)         # think
        self.mem.heartbeat(self.robot_id,
                           pos=(world.robots[self.robot_id].x, world.robots[self.robot_id].y),
                           battery=world.robots[self.robot_id].battery,
                           status="exploring")
        return action                                # act (the sim applies it)

    # --- sync -------------------------------------------------------------

    def _sync(self, percept: Percept) -> None:
        """Push new sightings through the reconcile gate.

        Deduplicated locally first, so a scout hovering over one victim does not
        hammer the gate with the same observation every tick. The gate is still
        the authority on whether two *different* robots saw the same thing.
        """
        for tile in percept.tiles:
            self.explored.add((tile["x"], tile["y"]))

        for victim in percept.victims:
            self._report(VICTIM, victim["x"], victim["y"], {
                "victim_id": victim["id"], "state": victim["state"],
                "note": "sighted by scout",
            })

        for hazard in percept.hazards:
            self._report(HAZARD, hazard["x"], hazard["y"], {"kind": hazard["kind"]})

    def _report(self, kind: str, x: int, y: int, payload: dict[str, Any]) -> None:
        key = (kind, x, y)
        if key in self.reported:
            return
        self.reported.add(key)

        embedding = None
        if self.embedder is not None:
            embedding = self.embedder.embed(
                f"{kind} at ({x},{y}): {payload.get('note', payload.get('kind', ''))}"
            )
        self.mem.report_observation(
            self.mission_id, self.robot_id, kind, (x, y),
            payload=payload, embedding=embedding,
        )
        self.mem.log_event(self.mission_id, self.robot_id, f"{kind}_reported",
                           {"x": x, "y": y, **payload})

    # --- think ------------------------------------------------------------

    def _think(self, world: World, percept: Percept) -> Action:
        """Frontier-biased exploration (§4.3): head for the nearest tile we have
        not seen, preferring to keep going rather than dithering between equally
        good options."""
        robot = world.robots[self.robot_id]
        here = (robot.x, robot.y)

        if self.frontier_target in (None, here) or not self._worth_pursuing(self.frontier_target):
            self.frontier_target = self._pick_frontier(world, here)

        if self.frontier_target is None:
            return Action.idle()
        return self._step_toward(world, here, self.frontier_target)

    def _worth_pursuing(self, target: tuple[int, int] | None) -> bool:
        return target is not None and target not in self.explored

    def _sector_bounds(self, width: int) -> tuple[int, int]:
        band = width / self.sector_count
        return int(band * self.sector), int(band * (self.sector + 1))

    def _pick_frontier(self, world: World, here: tuple[int, int]) -> tuple[int, int] | None:
        """Nearest unexplored passable tile, preferring this scout's own sector.

        A full scan every tick is fine at 40x30 and keeps the skeleton honest:
        the scout genuinely heads for unseen ground rather than wandering. Tiles
        outside its sector are still reachable — they just carry a penalty, so a
        scout finishing its band spills into a neighbour's rather than idling.
        """
        low, high = self._sector_bounds(world.map.width)
        penalty = world.map.width + world.map.height   # never beats an in-sector tile

        best: tuple[int, int] | None = None
        best_score = None
        for y in range(world.map.height):
            for x in range(world.map.width):
                if (x, y) in self.explored or world.ground[y][x] == WALL:
                    continue
                score = abs(x - here[0]) + abs(y - here[1])
                if not (low <= x < high):
                    score += penalty
                if best_score is None or score < best_score:
                    best, best_score = (x, y), score
        return best

    def _step_toward(self, world: World, here: tuple[int, int],
                     target: tuple[int, int]) -> Action:
        """Greedy step that reduces distance, preferring the larger gap first.

        Not A* — that arrives with the rescue chain. A scout flies over debris,
        so the only real obstacles are walls and fire, and greedy-with-fallback
        covers the open map well enough to prove the slice.
        """
        robot = world.robots[self.robot_id]
        dx, dy = target[0] - here[0], target[1] - here[1]

        preferred: list[str] = []
        if abs(dx) >= abs(dy):
            preferred = [self._dir_x(dx), self._dir_y(dy)]
        else:
            preferred = [self._dir_y(dy), self._dir_x(dx)]
        preferred = [d for d in preferred if d]

        # Fall back to any legal direction so a blocked scout keeps exploring
        # instead of standing still against a wall for the rest of the mission.
        options = preferred + sorted(DIRECTIONS)
        for direction in options:
            ddx, ddy = DIRECTIONS[direction]
            nx, ny = here[0] + ddx, here[1] + ddy
            if world.passable(nx, ny, flying=robot.flying) and not world.occupied(
                nx, ny, ignore=self.robot_id
            ):
                return Action.move(direction)

        self.frontier_target = None
        return Action.idle()

    @staticmethod
    def _dir_x(dx: int) -> str:
        return "e" if dx > 0 else "w" if dx < 0 else ""

    @staticmethod
    def _dir_y(dy: int) -> str:
        return "s" if dy > 0 else "n" if dy < 0 else ""
