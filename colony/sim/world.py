"""Server-authoritative world state and the tick pipeline (PRD §4.8).

The pipeline runs in a fixed order every tick:

    ingest queued actions -> validate against world rules -> apply movement/work
    -> run dynamics (fire spread, vitals, scheduled escalations)
    -> derive per-robot percepts -> emit a state frame

Deterministic given a seed: the same seed and the same action log produce the
same mission, which is what makes the golden demo run reproducible. Nothing in
here calls time.time() or an unseeded random.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from sim.protocol import ACT, DIRECTIONS, IDLE, MOVE, Action, StateFrame
from world.map_format import DEBRIS, EMPTY, FIRE, RUBBLE_HEAVY, UNSTABLE, WorldMap

FIRE_SPREAD_TICKS = 25  # §3.3: fire spreads every 25 ticks
CLEAR_TICKS = {DEBRIS: 3, RUBBLE_HEAVY: 6}
STABILIZE_TICKS = 2

# §3.3 battery is quoted in *ticks* of operation, not tiles travelled: a scout
# has "120 (recharge at base)". Draining per tile made a speed-3 scout last 40
# ticks and a speed-1 lifter 300, which is neither the stat block nor a fair
# comparison between roles.
RECHARGE_TICKS = 20  # a full battery, from the staging zone
RESTOCK_TICKS = 2  # §3.3: kits come off the shelf, they are not manufactured
MEDIC_KITS = 2  # §3.3: "carries 2 supply kits"
BASE_ZONE = "staging"  # §3.3: "Staging (base + charging, top-left)"

# §3.3 stat blocks. Speed is tiles per tick: one move action advances the robot
# up to `speed` tiles in that direction, stopping early at the first obstacle.
# Keeping it one action per tick leaves contract 2 simple — an agent that had to
# submit a list would have to know each role's speed to fill it.
ROLES: dict[str, dict[str, int]] = {
    "scout": {"speed": 3, "vision": 6, "battery": 120},
    "lifter": {"speed": 1, "vision": 2, "battery": 300},
    "medic": {"speed": 2, "vision": 3, "battery": 200},
}


@dataclass
class Robot:
    id: str
    role: str
    x: int
    y: int
    facing: str = "s"
    status: str = "idle"
    battery: int = 0
    bubble: str = ""
    kits: int = 0  # medics only (§3.3); stabilizing spends one
    work_left: int = 0  # ticks remaining on the current act()
    work_target: tuple[int, int] | None = None

    @property
    def vision(self) -> int:
        return ROLES[self.role]["vision"]

    @property
    def flying(self) -> bool:
        return self.role == "scout"  # §3.3: the scout drone flies over debris

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "x": self.x,
            "y": self.y,
            "facing": self.facing,
            "status": self.status,
            "battery": self.battery,
            "kits": self.kits,
            # The renderer dims ground nobody is currently looking at (§4.8's
            # "explored-but-stale"), which it can only work out from where the
            # robots are and how far each one sees. Sending the radius keeps the
            # §3.3 stat block in one place instead of copied into JavaScript.
            "vision": self.vision,
            "bubble": self.bubble,
        }


@dataclass
class Victim:
    id: str
    x: int
    y: int
    vitals_deadline: int
    state: str = "unknown"
    found_at: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "x": self.x,
            "y": self.y,
            "state": self.state,
            "vitals_deadline": self.vitals_deadline,
            "found_at": self.found_at,
        }


@dataclass
class Percept:
    """What one robot can see this tick — its own local view, never the shared map."""

    robot_id: str
    tick: int
    tiles: list[dict[str, Any]] = field(default_factory=list)
    victims: list[dict[str, Any]] = field(default_factory=list)
    hazards: list[dict[str, Any]] = field(default_factory=list)


class World:
    def __init__(self, world_map: WorldMap, seed: int | None = None):
        self.map = world_map
        self.tick = 0
        self.rng = random.Random(seed if seed is not None else world_map.seed or 0)
        # Mutable copies; the WorldMap itself stays the pristine initial state.
        self.ground = [row[:] for row in world_map.ground]
        self.objects = [row[:] for row in world_map.objects]
        self.robots: dict[str, Robot] = {}
        self.victims: dict[str, Victim] = {
            v["id"]: Victim(
                id=v["id"],
                x=v["x"],
                y=v["y"],
                vitals_deadline=v["vitals_deadline"],
                state=v.get("state", "unknown"),
            )
            for v in world_map.victims
        }
        self.events: list[dict[str, Any]] = []
        # Fog of war (FR-8). `explored` is the *shared* set — any robot's vision
        # reveals for everyone, straight from what the fleet collectively knows,
        # which is precisely the difference the ON/OFF toggle demonstrates.
        # Baseline mode keeps per-robot sets instead, so the two runs diverge
        # visibly rather than only in the metrics.
        self.explored: set[tuple[int, int]] = set()
        self.explored_by: dict[str, set[tuple[int, int]]] = {}
        self._visited: dict[str, set[tuple[int, int]]] = {}
        self.shared_vision = True
        self._explored_delta: list[tuple[int, int]] = []
        self._tiles_changed: list[dict[str, Any]] = []
        self._fired_escalations: set[int] = set()
        self._spawn_from_map()

    # --- setup ------------------------------------------------------------

    def _spawn_from_map(self) -> None:
        for role, points in self.map.spawn_points.items():
            for i, point in enumerate(points, start=1):
                robot_id = f"{role[0]}{i}"
                self.robots[robot_id] = Robot(
                    id=robot_id,
                    role=role,
                    x=point["x"],
                    y=point["y"],
                    battery=ROLES[role]["battery"],
                    kits=MEDIC_KITS if role == "medic" else 0,
                )

    # --- queries ----------------------------------------------------------

    def passable(self, x: int, y: int, *, flying: bool = False) -> bool:
        if not (0 <= x < self.map.width and 0 <= y < self.map.height):
            return False
        if self.ground[y][x] == "wall":
            return False
        obj = self.objects[y][x]
        if obj == FIRE:
            return False
        if flying:
            return True
        return obj not in (DEBRIS, RUBBLE_HEAVY)

    def occupied(self, x: int, y: int, *, ignore: str | None = None) -> bool:
        """Two ground robots cannot share a tile; scouts fly, so they may."""
        return any(
            r.x == x and r.y == y and r.id != ignore and not r.flying
            for r in self.robots.values()
        )

    # --- the pipeline -----------------------------------------------------

    def step(self, actions: dict[str, Action] | None = None) -> StateFrame:
        """Advance exactly one tick and return the frame describing it."""
        self.tick += 1
        self._tiles_changed = []
        self._explored_delta = []
        # `events` is deliberately NOT cleared here. Agents call percept()
        # before step() (see Mission.tick_once and Scout.step), and percept
        # appends victim_found — clearing at the top of the tick threw those
        # away, so no sighting ever reached fleet memory or the event ticker.
        # The list is drained when the frame is emitted instead.

        submitted = actions or {}
        # Iterate the roster, not the submitted actions: a robot part-way
        # through clearing debris keeps working whether or not its agent sends
        # anything this tick. Driving progress off the actions dict meant a
        # quiet agent silently froze the job it had already started.
        for robot_id, robot in self.robots.items():
            if robot.work_left > 0:
                self._advance_work(robot)  # committed until the job finishes
                continue
            action = submitted.get(robot_id)
            if action is not None:
                self._apply(robot, action)

        self._run_dynamics()
        self._update_vision()
        return self._frame()

    def _apply(self, robot: Robot, action: Action) -> None:
        """Validate against world rules, then apply. An illegal action becomes a
        rejection event rather than an exception — one confused agent must not
        take down the mission."""
        if action.kind == IDLE:
            robot.status = "idle"
            return

        if action.kind == MOVE:
            # §3.3 quotes battery in ticks of operation, so a flat move is a
            # flat cost: a robot that has run its battery down is stranded where
            # it stands until someone gets it home, and the agents plan around
            # that (return-to-base) rather than being rescued by the sim.
            if robot.battery <= 0:
                self._reject(robot, "battery empty")
                # Set after the rejection, which reports "blocked" — a robot
                # that ran out of power is not blocked by anything and will not
                # come unblocked. The renderer and the event log both need to
                # say so, because it is the one failure the fleet cannot undo.
                robot.status = "stranded"
                return

            dx, dy = DIRECTIONS[action.direction]
            robot.facing = action.direction
            moved = 0
            for _ in range(ROLES[robot.role]["speed"]):
                nx, ny = robot.x + dx, robot.y + dy
                if not self.passable(nx, ny, flying=robot.flying):
                    break
                if self.occupied(nx, ny, ignore=robot.id):
                    break
                robot.x, robot.y = nx, ny
                moved += 1
            if moved == 0:
                self._reject(robot, f"cannot move {action.direction}")
                return
            robot.battery -= 1
            robot.status = "moving"
            return

        if action.kind == ACT:
            self._begin_work(robot, action)

    def _begin_work(self, robot: Robot, action: Action) -> None:
        tx, ty = action.target

        # Base services first: they are the one pair of verbs whose target is
        # the robot's own tile, so the adjacency and bounds rules below are the
        # wrong questions to ask about them.
        if action.verb in ("recharge", "restock"):
            self._begin_base_service(robot, action.verb)
            return

        # Bounds first: Action.parse validates shape only, and adjacency uses
        # absolute differences, so an off-map target slips through whenever the
        # robot stands at an edge. A negative coordinate would then index from
        # the far side and silently rewrite a tile across the map, and an
        # over-large one raises IndexError inside the tick loop — which is the
        # one thing an illegal action must never do.
        if not (0 <= tx < self.map.width and 0 <= ty < self.map.height):
            self._reject(robot, f"target ({tx},{ty}) is outside the map")
            return
        if abs(tx - robot.x) + abs(ty - robot.y) > 1:
            self._reject(robot, f"target ({tx},{ty}) is not adjacent")
            return

        if action.verb == "clear_debris":
            if robot.role != "lifter":
                self._reject(robot, f"{robot.role} cannot clear debris")
                return
            obj = self.objects[ty][tx]
            if obj not in CLEAR_TICKS:
                self._reject(robot, f"nothing to clear at ({tx},{ty})")
                return
            self._start_work(robot, "clearing", (tx, ty), CLEAR_TICKS[obj])
            return

        if action.verb == "stabilize":
            if robot.role != "medic":
                self._reject(robot, f"{robot.role} cannot stabilize")
                return
            victim = self.victim_at(tx, ty)
            if victim is None or victim.state == "stabilized":
                self._reject(robot, f"no victim to stabilize at ({tx},{ty})")
                return
            if robot.kits <= 0:
                # §3.3: two kits, then back to base. The medic plans around this
                # the same way it plans around battery — the sim only says no.
                self._reject(robot, "out of supply kits")
                return
            self._start_work(robot, "stabilizing", (tx, ty), STABILIZE_TICKS)
            return

        self._reject(robot, f"verb {action.verb!r} is not implemented yet")

    def _begin_base_service(self, robot: Robot, verb: str) -> None:
        """Recharge or restock, both of which only happen at base (§3.3)."""
        if not self.at_base(robot.x, robot.y):
            self._reject(robot, f"{verb} needs the staging zone")
            return
        if verb == "restock" and robot.role != "medic":
            self._reject(robot, f"{robot.role} carries no kits")
            return
        if verb == "recharge" and robot.battery >= ROLES[robot.role]["battery"]:
            self._reject(robot, "battery already full")
            return
        if verb == "restock" and robot.kits >= MEDIC_KITS:
            self._reject(robot, "kits already full")
            return
        self._start_work(
            robot,
            "recharging" if verb == "recharge" else "restocking",
            (robot.x, robot.y),
            RECHARGE_TICKS if verb == "recharge" else RESTOCK_TICKS,
        )

    def at_base(self, x: int, y: int) -> bool:
        """Whether this tile is inside the staging zone — base, charger and
        supply shelf in one (§3.3).

        Falls back to the spawn tiles for maps that define no zones, which is
        every fixture in the test suite: a robot that could never recharge
        because its test map has no zone list would be a fixture artefact
        showing up as agent behaviour.
        """
        for zone in self.map.zones:
            if zone.get("name") != BASE_ZONE:
                continue
            if (
                zone["x"] <= x < zone["x"] + zone["width"]
                and zone["y"] <= y < zone["y"] + zone["height"]
            ):
                return True
        if any(z.get("name") == BASE_ZONE for z in self.map.zones):
            return False
        return any(
            point["x"] == x and point["y"] == y
            for points in self.map.spawn_points.values()
            for point in points
        )

    def _start_work(
        self, robot: Robot, status: str, target: tuple[int, int], ticks: int
    ) -> None:
        """Begin a timed job. The tick that starts the work counts as the first
        tick of it, so `clear_debris` costs the 3 ticks §3.3 specifies rather
        than 4."""
        robot.status = status
        robot.work_target = target
        robot.work_left = ticks
        self._advance_work(robot)

    def _advance_work(self, robot: Robot) -> None:
        robot.work_left -= 1
        if robot.work_left <= 0:
            self._finish_work(robot)

    def _finish_work(self, robot: Robot) -> None:
        tx, ty = robot.work_target or (robot.x, robot.y)
        if robot.status == "clearing":
            self.objects[ty][tx] = EMPTY
            self._tile_changed(tx, ty)
            self._event(robot.id, "debris_cleared", {"x": tx, "y": ty})
        elif robot.status == "stabilizing":
            victim = self.victim_at(tx, ty)
            if victim is not None:
                victim.state = "stabilized"
                robot.kits = max(0, robot.kits - 1)
                self._event(robot.id, "victim_stabilized", {"victim": victim.id})
        elif robot.status == "recharging":
            robot.battery = ROLES[robot.role]["battery"]
            self._event(robot.id, "recharged", {"battery": robot.battery})
        elif robot.status == "restocking":
            robot.kits = MEDIC_KITS
            self._event(robot.id, "restocked", {"kits": robot.kits})
        robot.status = "idle"
        robot.work_target = None

    def _run_dynamics(self) -> None:
        if self.tick % FIRE_SPREAD_TICKS == 0:
            self._spread_fire()
        self._tick_vitals()
        self._run_escalations()

    def _spread_fire(self) -> None:
        """Fire claims one new tile every FIRE_SPREAD_TICKS (§3.3).

        One tile per event, not one per burning tile. Letting every burning tile
        spread doubles the fire each event, which covers a 40x30 map in about
        250 ticks — the whole block would be alight before the tick-300
        aftershock, and the mission would be unplayable rather than tense.
        """
        frontier = [
            (x, y, x + dx, y + dy)
            for y in range(self.map.height)
            for x in range(self.map.width)
            if self.objects[y][x] == FIRE
            for dx, dy in DIRECTIONS.values()
            if 0 <= x + dx < self.map.width
            and 0 <= y + dy < self.map.height
            and self.ground[y + dy][x + dx] != "wall"
            and self.objects[y + dy][x + dx] != FIRE
        ]
        if not frontier:
            return
        # sorted() so the rng draw depends only on the world state, never on
        # dict or set iteration order — determinism (§4.8) rests on this.
        _, _, nx, ny = self.rng.choice(sorted(frontier))
        self.objects[ny][nx] = FIRE
        self._tile_changed(nx, ny)
        self._event("world", "fire_spread", {"x": nx, "y": ny})

    def _tick_vitals(self) -> None:
        for victim in self.victims.values():
            if victim.state in ("stabilized", "lost"):
                continue
            if self.tick >= victim.vitals_deadline:
                victim.state = "lost"
                self._event("world", "victim_lost", {"victim": victim.id})

    def _run_escalations(self) -> None:
        for index, esc in enumerate(self.map.escalations):
            if esc["tick"] != self.tick or index in self._fired_escalations:
                continue
            self._fired_escalations.add(index)
            for tile in esc.get("block_tiles", []):
                self.objects[tile["y"]][tile["x"]] = tile.get("tile", DEBRIS)
                self._tile_changed(tile["x"], tile["y"])
            for tile in esc.get("unstable_tiles", []):
                self.ground[tile["y"]][tile["x"]] = UNSTABLE
                self._tile_changed(tile["x"], tile["y"])
            for victim in esc.get("reveal_victims", []):
                self.victims[victim["id"]] = Victim(
                    id=victim["id"],
                    x=victim["x"],
                    y=victim["y"],
                    vitals_deadline=victim["vitals_deadline"],
                    state=victim.get("state", "unknown"),
                )
            self._event(
                "world",
                esc["kind"],
                {
                    "screen_shake": esc.get("screen_shake", False),
                    "blocked": len(esc.get("block_tiles", [])),
                    "revealed": [v["id"] for v in esc.get("reveal_victims", [])],
                },
            )

    # --- percepts ---------------------------------------------------------

    def _update_vision(self) -> None:
        """Pipeline stage: derive what every robot can see (§4.8).

        A stage rather than a side effect of `percept()`, because a robot with no
        agent driving it still has eyes. Tying revelation to whoever happened to
        ask for percepts left the fog claiming ground was unseen while a robot
        stood in the middle of it, and double-logged a sighting whenever percept
        was called twice in a tick.
        """
        for robot_id, robot in self.robots.items():
            radius = robot.vision
            for y in range(
                max(0, robot.y - radius), min(self.map.height, robot.y + radius + 1)
            ):
                for x in range(
                    max(0, robot.x - radius), min(self.map.width, robot.x + radius + 1)
                ):
                    self._reveal(robot_id, (x, y))

            # Duplicate-effort (§4.7) is about redundant *visits* — ground a
            # robot travelled over that another had already covered — not about
            # overlapping vision cones. Counting what robots saw rather than
            # where they went made the metric measure the wrong thing entirely,
            # and reported coordinated runs as slightly worse than baseline.
            here = (robot.x, robot.y)
            if here not in self._visited.setdefault(robot_id, set()):
                self._visited[robot_id].add(here)
                self._event(robot_id, "tile_visited", {"x": here[0], "y": here[1]})

            for victim in self.victims.values():
                if (
                    abs(victim.x - robot.x) <= radius
                    and abs(victim.y - robot.y) <= radius
                    and victim.state == "unknown"
                ):
                    victim.state = "located"
                    victim.found_at = self.tick
                    self._event(
                        robot_id,
                        "victim_found",
                        {"victim": victim.id, "x": victim.x, "y": victim.y},
                    )

    def percept(self, robot_id: str) -> Percept:
        """Local vision only — the shared map comes from fleetmem, not from here.

        A pure read; revelation and victim-locating happen in `_update_vision`.
        Square vision radius per §3.3's "vision radius" stat; no line-of-sight
        model, which the PRD does not ask for.
        """
        robot = self.robots[robot_id]
        radius = robot.vision
        seen = Percept(robot_id=robot_id, tick=self.tick)
        # Agents call percept() before the first step(), so derive vision on
        # demand when the tick loop has not done it yet.
        if not self.explored:
            self._update_vision()

        for y in range(
            max(0, robot.y - radius), min(self.map.height, robot.y + radius + 1)
        ):
            for x in range(
                max(0, robot.x - radius), min(self.map.width, robot.x + radius + 1)
            ):
                seen.tiles.append(
                    {
                        "x": x,
                        "y": y,
                        "ground": self.ground[y][x],
                        "object": self.objects[y][x],
                    }
                )
                if self.objects[y][x] == FIRE:
                    seen.hazards.append({"kind": "fire", "x": x, "y": y})

        for victim in self.victims.values():
            if abs(victim.x - robot.x) <= radius and abs(victim.y - robot.y) <= radius:
                seen.victims.append(victim.to_json())
        return seen

    def visible_to(self, robot_id: str) -> set[tuple[int, int]]:
        """What this robot may act on.

        Coordinated mode hands back the fleet's shared knowledge; baseline hands
        back only what this robot saw itself (§3.3). Every difference between the
        two runs traces back to this one method, which is what makes the ON/OFF
        delta attributable to sharing rather than to a pile of mode flags.
        """
        if self.shared_vision:
            return self.explored
        return self.explored_by.setdefault(robot_id, set())

    def _reveal(self, robot_id: str, tile: tuple[int, int]) -> None:
        """Record that a robot can see this tile.

        In coordinated mode the fleet keeps one set; in baseline each robot keeps
        its own and the viewer sees the union dimmed (§4.8). Only newly revealed
        tiles go into the frame — the set grows to the whole map, and resending
        it four times a second would swamp every other field.
        """
        self.explored_by.setdefault(robot_id, set()).add(tile)
        if tile not in self.explored:
            self.explored.add(tile)
            self._explored_delta.append(tile)

    def coverage(self) -> float:
        """Explored share of the tiles a robot could ever stand on or see.

        Walls are excluded: counting them would cap coverage below 100% forever
        and make Coverage@500 (§4.7) unreadable.
        """
        reachable = {
            (x, y)
            for y in range(self.map.height)
            for x in range(self.map.width)
            if self.ground[y][x] != "wall"
        }
        if not reachable:
            return 1.0
        # Intersected, not just divided: vision reveals walls too, and counting
        # them against a wall-free denominator reported 116% coverage.
        return len(self.explored & reachable) / len(reachable)

    # --- frames -----------------------------------------------------------

    def snapshot(self) -> StateFrame:
        """The full world, sent once when a browser connects."""
        return StateFrame(
            tick=self.tick,
            kind="snapshot",
            robots=[r.to_json() for r in self.robots.values()],
            victims=[v.to_json() for v in self.victims.values()],
            metrics=self.metrics(),
            explored=[list(t) for t in sorted(self.explored)],
            world={
                "width": self.map.width,
                "height": self.map.height,
                "tile_size": self.map.tile_size,
                "name": self.map.name,
                "mission_length_ticks": self.map.mission_length_ticks,
                "ground": self.ground,
                "objects": self.objects,
                "zones": self.map.zones,
                "shared_vision": self.shared_vision,
                "sectors": self.map.sectors,
            },
        )

    def _frame(self) -> StateFrame:
        frame = StateFrame(
            tick=self.tick,
            kind="diff",
            robots=[r.to_json() for r in self.robots.values()],
            victims=[v.to_json() for v in self.victims.values()],
            tiles_changed=list(self._tiles_changed),
            explored=[list(t) for t in self._explored_delta],
            events=list(self.events),
            metrics=self.metrics(),
        )
        self.events = []  # drained on emit, so nothing is lost or sent twice
        return frame

    def metrics(self) -> dict[str, Any]:
        states = [v.state for v in self.victims.values()]
        return {
            "tick": self.tick,
            "victims_total": len(states),
            "victims_located": sum(1 for s in states if s != "unknown"),
            "victims_stabilized": sum(1 for s in states if s == "stabilized"),
            "victims_lost": sum(1 for s in states if s == "lost"),
            "coverage": round(self.coverage(), 3),
        }

    @property
    def finished(self) -> bool:
        if self.tick >= self.map.mission_length_ticks:
            return True
        # `all()` over no victims is vacuously true, which would report a map
        # with none as finished before it started.
        return bool(self.victims) and all(
            v.state in ("stabilized", "lost") for v in self.victims.values()
        )

    # --- helpers ----------------------------------------------------------

    @property
    def escalations_fired(self) -> int:
        """How many scheduled escalations have gone off (FR-7).

        Agents watch the count, not the contents: an aftershock is felt by
        everyone, but *what* it changed has to be discovered by looking, the
        same as anything else. Handing agents the escalation's tile list would
        be ground truth arriving without a robot ever sensing it.
        """
        return len(self._fired_escalations)

    def victim_at(self, x: int, y: int) -> Victim | None:
        """The victim on this tile, if any. Public: agents ask it to decide
        whether a delivery is still needed."""
        return next((v for v in self.victims.values() if v.x == x and v.y == y), None)

    def _tile_changed(self, x: int, y: int) -> None:
        self._tiles_changed.append(
            {
                "x": x,
                "y": y,
                "ground": self.ground[y][x],
                "object": self.objects[y][x],
            }
        )

    def _event(self, actor: str, verb: str, detail: dict[str, Any]) -> None:
        # The tick rides in the detail as well as alongside it: fleet memory
        # stores only actor/verb/detail, and §4.7's median time-to-stabilize is
        # computed from the event log after the fact, when the frame is long
        # gone.
        self.events.append(
            {
                "tick": self.tick,
                "actor": actor,
                "verb": verb,
                "detail": {**detail, "tick": self.tick},
            }
        )

    def _reject(self, robot: Robot, reason: str) -> None:
        robot.status = "blocked"
        self._event(robot.id, "action_rejected", {"reason": reason})
