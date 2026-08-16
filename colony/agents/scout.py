"""Scout agent — the §4.3 loop.

    sense -> sync -> think -> act -> report

Bedrock is asked one question, at one boundary: which sector to sweep next
(`agents/planning.py`). Everything else — the frontier bias inside a sector,
when a sector counts as swept, when to fly home and charge — is rules, because
§4.3 has the model choosing *among* behaviours rather than executing them. A
scout with `planner=None` is a complete scout; the planner improves a choice it
was already able to make.

The agent holds no shared world model of its own. What it sees goes to fleetmem
and comes back as shared belief — that is the whole product thesis, so the
skeleton does it properly from the first tick.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from agents import logistics, planning
from agents.pathing import find_move_plan, find_path
from agents.planning import BEDROCK, RULES
# Both agent types report status on the same cadence and there is no reason for
# them to disagree about it, so the constants have one home rather than two.
from agents.worker import HEARTBEAT_EVERY_TICKS, RENEW_EVERY_TICKS
from fleetmem.types import AFTERSHOCK, IDLE_TRIGGER, TASK_DONE
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

    # Bedrock at plan boundaries (§4.3) — which sector to sweep next. None means
    # rules only, which is a complete scout.
    planner: Any = None

    explored: set[tuple[int, int]] = field(default_factory=set)
    reported: set[tuple[str, int, int]] = field(default_factory=set)
    frontier_target: tuple[int, int] | None = field(default=None)
    # Heading home to recharge (§3.3): 120 ticks of flight does not cover a
    # 40x30 map, so a scout that never goes back strands itself mid-sweep.
    homing: bool = False
    seen_escalations: int = 0
    # tile -> tick this scout last had eyes on it. Turns "everything is
    # explored" into "this is the stalest ground I know", which is what a scout
    # needs after the map changes under it (FR-7).
    last_seen: dict[tuple[int, int], int] = field(default_factory=dict)
    # Ticks the status write and the sector-lease renewal last went out. None
    # means "never", so both fire on this scout's first tick.
    _heartbeat_at: int | None = field(default=None)
    _renew_at: int | None = field(default=None)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    # --- the loop ---------------------------------------------------------

    def step(self, world: World) -> Action:
        """One iteration: sense, sync, think, act, report.

        Returns the action for the sim to apply. Reporting happens here rather
        than after the action so a belief is never lost if the mission ends on
        this tick.
        """
        robot = world.robots[self.robot_id]
        percept = world.percept(self.robot_id)  # sense
        self._sync(percept, world)  # sync: local -> shared memory
        self._note_escalation(world)

        if robot.work_left > 0:  # mid-recharge; the sim drives it
            action = Action.idle()
        elif self.homing or logistics.needs_base(world, robot, (robot.x, robot.y)):
            action = self._go_home(world, robot)
        else:
            action = self._think(world, percept)  # think

        self._report_status(world, robot)
        return action  # act (the sim applies it)

    def _report_status(self, world: World, robot: Any) -> None:
        """Status write and sector-lease renewal, each on its own cadence.

        Two statements, two round trips, two different deadlines (§4.3, §4.4).
        The sector lease is the longer of the fleet's two at 20s, so a renewal
        every 5s leaves three that may be missed before it lapses.
        """
        tick = world.tick
        beat_due = (
            self._heartbeat_at is None
            or tick - self._heartbeat_at >= HEARTBEAT_EVERY_TICKS
        )
        renew_due = (
            self._renew_at is None or tick - self._renew_at >= RENEW_EVERY_TICKS
        )
        if beat_due:
            self.mem.heartbeat(
                self.robot_id,
                pos=(robot.x, robot.y),
                battery=robot.battery,
                status=robot.status,
                lease_seconds=SECTOR_LEASE_SECONDS,
                renew=renew_due,
            )
            self._heartbeat_at = tick
            if renew_due:
                self._renew_at = tick
        elif renew_due:
            self.mem.renew_leases(self.robot_id, SECTOR_LEASE_SECONDS)
            self._renew_at = tick

    # --- battery (§3.3) ---------------------------------------------------

    def _go_home(self, world: World, robot: Any) -> Action:
        """Fly back and recharge, giving the sector up on the way out.

        A scout that holds its sector while it charges keeps 10x10 tiles off the
        board for 40 ticks and nobody else may sweep them — the exact
        duplicate-effort problem sector claims exist to prevent, inverted. The
        lease would have expired anyway; releasing says so immediately.
        """
        if self.sector_task is not None:
            self.mem.release_task(self.sector_task.id)
            self.mem.log_event(
                self.mission_id,
                self.robot_id,
                "task_released",
                {"task": str(self.sector_task.id), "reason": "returning to base"},
            )
            self.sector_task = None
        if not self.homing:
            self.homing = True
            self.mem.log_event(
                self.mission_id,
                self.robot_id,
                "returning_to_base",
                {"battery": robot.battery},
            )

        service = logistics.service_action(world, robot)
        if service is not None:
            self._say(world, "🔌 recharging")
            return service
        if logistics.is_serviced(world, robot):
            self.homing = False
            return Action.idle()

        base = logistics.base_tile(world, "scout")
        if base is None:
            self.homing = False
            return Action.idle()
        self._say(world, "🔋 returning to base")
        return self._step_toward(world, (robot.x, robot.y), base)

    # --- reacting to the world (FR-7) -------------------------------------

    def _note_escalation(self, world: World) -> None:
        """An aftershock means the map a scout finished sweeping is not the map
        it is standing in (FR-7).

        The sector it holds goes back to the pool and its own idea of what it
        has seen is dropped, so the sweep can be redone on the merits. What it
        must *not* do is read the escalation's tile list: an aftershock is felt,
        not downloaded, and a scout that knew where the new victim was without
        flying there would be ground truth wearing a robot costume.
        """
        if world.escalations_fired <= self.seen_escalations:
            return
        self.seen_escalations = world.escalations_fired
        self.explored.clear()
        self.frontier_target = None
        if self.sector_task is not None:
            self.mem.release_task(self.sector_task.id)
            self.sector_task = None
        self._log_plan(
            world,
            trigger=AFTERSHOCK,
            chosen={"action": "explore", "sector": None, "source": RULES},
            rationale="aftershock felt; re-sweeping from what I can see now",
        )
        self._say(world, "🌐 aftershock — re-sweeping")

    def _say(self, world: World, text: str) -> None:
        """Set the thought bubble the renderer already carries (§3.6)."""
        world.robots[self.robot_id].bubble = text

    def _log_plan(
        self, world: World, *, trigger: str, chosen: dict[str, Any], rationale: str
    ) -> None:
        """Every decision, with the memories behind it (FR-17).

        A scout logged nothing at all before this: clicking one in the demo
        opened an empty panel, on the agent that does most of the deciding.
        """
        digest = planning.build_digest(
            self.mem, self.mission_id, world.robots[self.robot_id]
        )
        self.mem.log_plan(
            self.mission_id,
            self.robot_id,
            trigger=trigger,
            chosen=chosen,
            rationale=rationale,
            based_on=list(digest.ids),
        )

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
            cost=lambda p: (
                DEBRIS_ROUTE_COST
                if world.objects[p[1]][p[0]] in (DEBRIS, RUBBLE_HEAVY)
                else 1
            ),
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
            # When, not just whether: after the map changes under a finished
            # sweep, "longest since anyone looked" is the only useful question
            # left (see _pick_stale).
            self.last_seen[(tile["x"], tile["y"])] = percept.tick

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
        trigger = IDLE_TRIGGER
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
                trigger = TASK_DONE
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
            # Priority first, then distance. Priority is what semantic memory
            # sets (seed_sector_tasks): sectors earlier missions found victims
            # in get swept before empty ground. Without this term the priority
            # bump changes nothing and recall is inert while appearing to work —
            # the same failure shape as an l2 index answering a cosine query.
            #
            # Safe by construction on a cold database: with no memories every
            # sector is priority 1, `-1` is constant, and this key is
            # order-identical to the distance-only one it replaces. X2's
            # byte-identical-log condition holds unless memories exist.
            #
            # Ties break on the sector name, never on the row id. `str(t.id)` is a
            # freshly generated UUID, so two equidistant sectors were decided by
            # whichever id happened to sort first — the same seed picked C1 on one
            # run and A3 on the next, and X2 could not pass. The name is stable
            # across runs and carries the same "pick one, consistently" intent.
            key=lambda t: (
                -t.priority,
                abs((t.target[0] or 0) - robot.x) + abs((t.target[1] or 0) - robot.y),
                t.kind,
            ),
        )
        if not candidates:
            return

        # §4.3: the model picks which sector, the rules fly it there. A choice
        # it cannot justify costs one lost claim race, no more.
        digest = planning.build_digest(self.mem, self.mission_id, robot)
        plan = (
            self.planner.plan(robot, world.tick, digest, candidates)
            if self.planner is not None
            else None
        )
        if plan is not None and plan.action == "claim_task" and plan.task_id:
            candidates.sort(key=lambda t: str(t.id) != str(plan.task_id))

        for task in candidates:
            if self.mem.claim_task(
                task.id, self.robot_id, lease_seconds=SECTOR_LEASE_SECONDS
            ):
                self.sector_task = task
                sector_id = task.kind.split(":", 1)[1]
                self.mem.log_event(
                    self.mission_id,
                    self.robot_id,
                    "sector_claimed",
                    {"sector": sector_id},
                )
                self.mem.log_plan(
                    self.mission_id,
                    self.robot_id,
                    trigger=trigger,
                    chosen={
                        "action": "explore",
                        "sector": sector_id,
                        "task_id": str(task.id),
                        "considered": len(candidates),
                        "source": BEDROCK if plan is not None else RULES,
                    },
                    rationale=(
                        plan.rationale
                        if plan is not None and plan.rationale
                        else f"nearest unswept sector of {len(candidates)} open"
                    ),
                    based_on=list(digest.ids),
                )
                self._say(world, f"🔍 scanning sector {sector_id}")
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
            self.frontier_target = self._pick_stale(world, here)
        if self.frontier_target is None:
            self._say(world, "🔍 holding station")
            return Action.idle()
        # §3.6's own example is 🔍 "scanning sector C". The sector is what a
        # viewer can place on the map; a raw tile coordinate is a debug view.
        sector = self._active_sectors[0] if self._active_sectors else None
        self._say(
            world,
            f"🔍 scanning sector {sector}"
            if sector
            else f"🔍 sweeping {self.frontier_target[0]},{self.frontier_target[1]}",
        )
        return self._step_toward(world, here, self.frontier_target)

    def _pick_stale(
        self, world: World, here: tuple[int, int]
    ) -> tuple[int, int] | None:
        """The ground this scout has not looked at for longest.

        "Explored" is not "still true". Once every sector was swept the scouts
        idled for the rest of the mission, so the aftershock could re-block a
        corridor, reveal a victim and change the map with nobody watching —
        measured on the demo map: the revealed victim was never found and died
        at its deadline. Patrolling the stalest tile turns exploration into
        something a mission never finishes, which is what a disaster site
        actually is.

        Own share only: patrolling across a neighbour's ground would put the
        duplicate-effort index (§4.7) back up for no coverage in return.
        """
        oldest, oldest_at = None, None
        for y in range(world.map.height):
            for x in range(world.map.width):
                if not world.passable(x, y, flying=True):
                    continue
                if (
                    self._active_sectors
                    and world.map.sector_at(x, y) not in self._active_sectors
                ):
                    continue
                seen_at = self.last_seen.get((x, y), -1)
                # Distance breaks ties, so a scout sweeps the stale ground near
                # it rather than crossing the sector for an equally stale tile.
                key = (seen_at, abs(x - here[0]) + abs(y - here[1]), x, y)
                if oldest_at is None or key < oldest_at:
                    oldest, oldest_at = (x, y), key
        return oldest if oldest != here else None

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
            # Deliberately *not* priced by the shared hazard map, unlike the
            # ground robots (`agents/beliefs.py`). Giving fire-adjacent tiles a
            # wide berth is prudent for a speed-1 lifter that could be cut off;
            # for a speed-3 drone whose entire job is coverage it measured as an
            # 8% coverage loss at 30 ticks, buying safety from a danger a flying
            # robot can leave in a single move.
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


def seed_sector_tasks(mem, mission_id, world_map, hot_sectors=()) -> list:
    """One `explore_sector` task per sector at mission bootstrap (FR-16).

    The sector id rides in the task kind so a scout can read it back without a
    second lookup, and so the event ticker says `explore_sector:C2` rather than
    an opaque uuid — legible to a judge watching the demo.

    `hot_sectors` is where semantic memory enters the mission: sectors earlier
    runs on this map found victims in are seeded at a higher priority, so the
    fleet sweeps them first. Setting priority at creation rather than bumping it
    afterwards keeps this to one parameter instead of a new SDK method.
    """
    hot = set(hot_sectors)
    return [
        mem.create_task(
            mission_id,
            f"explore_sector:{sector['id']}",
            (sector["x"], sector["y"]),
            priority=2 if sector["id"] in hot else 1,
        )
        for sector in world_map.sectors
    ]
