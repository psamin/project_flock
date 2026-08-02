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

from agents.pathing import find_move_plan, find_path
from sim.protocol import DIRECTIONS, Action
from sim.world import ROLES, Percept, World
from world.map_format import DEBRIS, FIRE, RUBBLE_HEAVY, WALL

# Kinds the scout reports into shared memory.
VICTIM, HAZARD = "victim", "hazard"

# What crossing a debris tile costs when planning a rescue route. Roughly the
# cost of clearing it (3 ticks, 6 for rubble-heavy), so a route digs through only
# when going around is genuinely worse.
DEBRIS_ROUTE_COST = 4

# FR-16. A short lease because a sector sweep is short: a dead scout's sector
# should return to the pool quickly, but not so quickly that a live scout keeps
# losing the one it is working.
SECTOR_LEASE_SECONDS = 20

# Coverage at which a sector counts as swept (§4.4: "~85%, tuned at playtest").
# Never 100%: walls and sealed interiors are unreachable, so a scout waiting for
# every tile would hold its sector for the whole mission.
SECTOR_DONE_COVERAGE = 0.85


def split_sectors(sectors: list[dict[str, Any]], parts: int) -> list[tuple[str, ...]]:
    """Divide the map's sector grid into `parts` **contiguous** shares.

    Contiguity is the whole point. Dealing the 12 sectors round-robin interleaves
    the scouts' territories across the map and they end up sweeping the same
    ground anyway — measured at 1.18x the coverage of a single scout, barely
    better than no assignment at all. Sorting by column first and cutting the
    list into blocks gives each scout a band of the map, which measured at 1.67x.

    Ordering is by (x, y) so the blocks come out as columns; a scout is never
    handed two sectors at opposite corners.
    """
    ordered = [s["id"] for s in sorted(sectors, key=lambda s: (s["x"], s["y"]))]
    if parts <= 1 or not ordered:
        return [tuple(ordered)] * max(1, parts)
    size, extra = divmod(len(ordered), parts)
    shares, cursor = [], 0
    for i in range(parts):
        take = size + (1 if i < extra else 0)
        shares.append(tuple(ordered[cursor : cursor + take]))
        cursor += take
    return shares


@dataclass
class Scout:
    """One scout drone. Deterministic given its seed."""

    robot_id: str
    mission_id: UUID
    mem: Any  # CockroachFleetMem or FakeFleetMem
    embedder: Any = None  # BedrockAdapter; None skips embeddings
    seed: int = 0

    # Static fallback share of the sector grid, used only when sector tasks are
    # not seeded (baseline mode, and small fixtures). FR-16's claimed sectors
    # take precedence whenever they exist.
    sectors: tuple[str, ...] = ()

    # The sector this scout currently holds a claim on (FR-16). Claimed one at a
    # time under a short lease, so two live scouts can never sweep the same
    # ground and a dead scout's sector frees itself with no supervisor involved.
    sector_task: Any = field(default=None)

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
        percept = world.percept(self.robot_id)  # sense
        self._sync(percept, world)  # sync: local -> shared memory
        action = self._think(world, percept)  # think
        self.mem.heartbeat(
            self.robot_id,
            pos=(world.robots[self.robot_id].x, world.robots[self.robot_id].y),
            battery=world.robots[self.robot_id].battery,
            status="exploring",
            lease_seconds=SECTOR_LEASE_SECONDS,
        )
        return action  # act (the sim applies it)

    # --- sync -------------------------------------------------------------

    def _blockers(self, world: World, pos: tuple[int, int]) -> list[tuple[int, int]]:
        """Debris a ground robot must clear before it can reach this victim.

        Not just the tiles touching the victim. §3.3's victims sit *behind* a
        debris wall in a dense block, and the wall is usually somewhere along the
        route rather than against the victim — ordering only adjacent clears left
        medics pathing to victims they could never reach, abandoning the task,
        and re-claiming it forever while the clock ran out.

        So the route decides: path from where the ground robots start, treating
        debris as passable but expensive, and every debris tile the cheapest
        route crosses becomes a `clear_debris`. If a clean route already exists,
        no lifter is needed.
        """
        origin = self._ground_origin(world)
        if origin is None:
            return []

        route = find_path(
            origin,
            pos,
            passable=lambda p: (
                0 <= p[0] < world.map.width
                and 0 <= p[1] < world.map.height
                and world.ground[p[1]][p[0]] != WALL
                and world.objects[p[1]][p[0]] != FIRE
            ),
            # Debris is crossable only by clearing it, so it costs about what
            # clearing costs; the route then prefers going around when going
            # around is cheap and digs through when it is not.
            cost=lambda p: DEBRIS_ROUTE_COST
            if world.objects[p[1]][p[0]] in (DEBRIS, RUBBLE_HEAVY)
            else 1,
            goal_is_adjacent=True,
        )
        if route is None:
            return []
        return [p for p in route if world.objects[p[1]][p[0]] in (DEBRIS, RUBBLE_HEAVY)]

    def _ground_origin(self, world: World) -> tuple[int, int] | None:
        """Where ground robots come from. Their spawn, or failing that, a lifter
        or medic's current tile — a scout's own position is no use, since it can
        fly over the very debris the question is about."""
        for role in ("lifter", "medic"):
            points = world.map.spawn_points.get(role)
            if points:
                return (points[0]["x"], points[0]["y"])
        for robot in world.robots.values():
            if not robot.flying:
                return (robot.x, robot.y)
        return None

    def _sync(self, percept: Percept, world: World) -> None:
        """Push new sightings through the reconcile gate, and turn victims into
        the rescue chain that reaches them.

        Deduplicated locally first, so a scout hovering over one victim does not
        hammer the gate with the same observation every tick. The gate is still
        the authority on whether two *different* robots saw the same thing, and
        register_victim is idempotent per position, so a second scout arriving
        later cannot dispatch the fleet twice.
        """
        for tile in percept.tiles:
            self.explored.add((tile["x"], tile["y"]))

        for victim in percept.victims:
            new = self._report(
                VICTIM,
                victim["x"],
                victim["y"],
                {
                    "victim_id": victim["id"],
                    "state": victim["state"],
                    "note": "sighted by scout",
                },
            )
            if not new:
                continue
            # §4.2 step 3: the sighting creates the work. Without this a scout
            # reports victims into shared memory that nobody is ever dispatched
            # to reach.
            self.mem.register_victim(
                self.mission_id,
                (victim["x"], victim["y"]),
                reported_by=self.robot_id,
                blocked_by=self._blockers(world, (victim["x"], victim["y"])),
                vitals_deadline=victim.get("vitals_deadline"),
            )

        for hazard in percept.hazards:
            self._report(HAZARD, hazard["x"], hazard["y"], {"kind": hazard["kind"]})

    def _report(self, kind: str, x: int, y: int, payload: dict[str, Any]) -> bool:
        """Report a sighting. False when this scout has already reported it."""
        key = (kind, x, y)
        if key in self.reported:
            return False
        self.reported.add(key)

        embedding = None
        if self.embedder is not None:
            embedding = self.embedder.embed(
                f"{kind} at ({x},{y}): {payload.get('note', payload.get('kind', ''))}"
            )
        self.mem.report_observation(
            self.mission_id,
            self.robot_id,
            kind,
            (x, y),
            payload=payload,
            embedding=embedding,
        )
        self.mem.log_event(
            self.mission_id,
            self.robot_id,
            f"{kind}_reported",
            {"x": x, "y": y, **payload},
        )
        return True

    # --- think ------------------------------------------------------------

    # --- sector claims (FR-16) --------------------------------------------

    def _sector_tiles(self, world: World, sector_id: str) -> list[tuple[int, int]]:
        sector = world.map.sector(sector_id)
        return [
            (x, y)
            for y in range(sector["y"], sector["y"] + sector["height"])
            for x in range(sector["x"], sector["x"] + sector["width"])
            if world.ground[y][x] != WALL
        ]

    def _sector_coverage(self, world: World, sector_id: str) -> float:
        tiles = self._sector_tiles(world, sector_id)
        if not tiles:
            return 1.0
        known = self._known(world)
        return sum(1 for t in tiles if t in known) / len(tiles)

    def _known(self, world: World) -> set[tuple[int, int]]:
        """Ground this scout may treat as explored.

        Coordinated mode reads the fleet's shared set — any robot's vision
        reveals for all, which is the whole product. Baseline reads only its own,
        so the two runs diverge for exactly one reason (§3.3).
        """
        return self.explored | world.visible_to(self.robot_id)

    def _sector_is_swept(self, world: World, sector_id: str) -> bool:
        """Whether this scout is done with a sector.

        Coverage alone is not a usable test. A sector holding walled interiors or
        sealed rooms can never reach the threshold, and a scout waiting for it
        sits there for the rest of the mission — measured on the demo map as
        coverage stalling at 67% with six victims never found. So the real
        criterion is "nothing left in here I can reach", with the coverage
        threshold as an early exit for sectors that are mostly open.
        """
        if self._sector_coverage(world, sector_id) >= SECTOR_DONE_COVERAGE:
            return True
        known = self._known(world)
        return not any(
            tile not in known and world.passable(*tile, flying=True)
            for tile in self._sector_tiles(world, sector_id)
        )

    def _manage_sector(self, world: World) -> None:
        """Complete a swept sector and claim the next one.

        Claiming is what makes exploration non-duplicating: the same transaction
        that stops two lifters taking one debris pile stops two scouts sweeping
        one sector, with no coordinator in the middle.
        """
        if self.sector_task is not None:
            sector_id = self.sector_task.kind.split(":", 1)[1]
            if self._sector_is_swept(world, sector_id):
                self.mem.complete_task(self.sector_task.id, self.robot_id)
                self.mem.log_event(
                    self.mission_id,
                    self.robot_id,
                    "sector_swept",
                    {"sector": sector_id},
                )
                self.sector_task = None
            else:
                return

        robot = world.robots[self.robot_id]
        # Nearest first: sectors all carry the same priority, so without this a
        # scout takes whatever the query happens to return and can fly the width
        # of the map past unswept ground to reach it.
        candidates = sorted(
            (
                t
                for t in self.mem.open_tasks(self.mission_id)
                if t.kind.startswith("explore_sector:")
            ),
            key=lambda t: (
                abs((t.target[0] or 0) - robot.x) + abs((t.target[1] or 0) - robot.y),
                str(t.id),
            ),
        )
        for task in candidates:
            if self.mem.claim_task(
                task.id, self.robot_id, lease_seconds=SECTOR_LEASE_SECONDS
            ):
                self.sector_task = task
                self.mem.log_event(
                    self.mission_id,
                    self.robot_id,
                    "sector_claimed",
                    {"sector": task.kind.split(":", 1)[1]},
                )
                return

    @property
    def _active_sectors(self) -> tuple[str, ...]:
        """The claimed sector if there is one, else the static share."""
        if self.sector_task is not None:
            return (self.sector_task.kind.split(":", 1)[1],)
        return self.sectors

    def _think(self, world: World, percept: Percept) -> Action:
        """Frontier-biased exploration (§4.3): head for the nearest tile we have
        not seen, preferring to keep going rather than dithering between equally
        good options."""
        self._manage_sector(world)
        robot = world.robots[self.robot_id]
        here = (robot.x, robot.y)

        if self.frontier_target in (None, here) or not self._worth_pursuing(
            self.frontier_target
        ):
            self.frontier_target = self._pick_frontier(world, here)

        if self.frontier_target is None:
            return Action.idle()
        return self._step_toward(world, here, self.frontier_target)

    def _worth_pursuing(self, target: tuple[int, int] | None) -> bool:
        return target is not None and target not in self.explored

    def _pick_frontier(
        self, world: World, here: tuple[int, int]
    ) -> tuple[int, int] | None:
        """Nearest unexplored passable tile, preferring this scout's own sector.

        A full scan every tick is fine at 40x30 and keeps the skeleton honest:
        the scout genuinely heads for unseen ground rather than wandering. Tiles
        outside its sector are still reachable — they just carry a penalty, so a
        scout that finishes its sector spills into a neighbour's rather than
        idling while ground goes unswept.
        """
        penalty = world.map.width + world.map.height  # never beats an in-sector tile

        best: tuple[int, int] | None = None
        best_score = None
        known = self._known(world)
        for y in range(world.map.height):
            for x in range(world.map.width):
                if (x, y) in known or world.ground[y][x] == WALL:
                    continue
                score = abs(x - here[0]) + abs(y - here[1])
                if (
                    self._active_sectors
                    and world.map.sector_at(x, y) not in self._active_sectors
                ):
                    score += penalty
                if best_score is None or score < best_score:
                    best, best_score = (x, y), score
        return best

    def _step_toward(
        self, world: World, here: tuple[int, int], target: tuple[int, int]
    ) -> Action:
        """Move along a plan searched over moves — the same planner the workers
        use.

        Greedy stepping was good enough while a scout only drifted toward open
        ground in its own half of the map. Sector claims changed that: a scout
        assigned a sector across the staging wall has to route through a single
        door, and greedy stepping pinned it against the wall for the whole
        mission. Measured on the demo map: both scouts frozen by tick 150,
        coverage stalled at 60%, six victims never found.
        """
        plan = find_move_plan(
            here,
            target,
            landing=lambda p, d: self._landing(world, p, d),
            speed=ROLES[world.robots[self.robot_id].role]["speed"],
        )
        if plan:
            return Action.move(plan[0])

        # Nothing reachable here: drop the target so the next tick picks another
        # frontier rather than standing against a wall.
        self.explored.add(target)
        self.frontier_target = None
        return Action.idle()

    def _landing(
        self, world: World, here: tuple[int, int], direction: str
    ) -> tuple[int, int]:
        """Where one `move` leaves this scout — the sim's rule, mirrored."""
        dx, dy = DIRECTIONS[direction]
        x, y = here
        for _ in range(ROLES[world.robots[self.robot_id].role]["speed"]):
            nx, ny = x + dx, y + dy
            if not world.passable(nx, ny, flying=True):
                break
            if world.occupied(nx, ny, ignore=self.robot_id):
                break
            x, y = nx, ny
        return (x, y)


def seed_sector_tasks(mem, mission_id, world_map) -> list:
    """One `explore_sector` task per sector at mission bootstrap (FR-16).

    The sector id rides in the task kind so a scout can read it back without a
    second lookup, and so the event ticker says `explore_sector:C2` rather than
    an opaque uuid — legible to a judge watching the demo.
    """
    return [
        mem.create_task(
            mission_id,
            f"explore_sector:{sector['id']}",
            (sector["x"], sector["y"]),
            priority=1,
        )
        for sector in world_map.sectors
    ]
