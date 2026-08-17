"""Operator interventions — breaking the world on purpose (issue #22).

§4.2's aftershock already proves the fleet replans when the world changes
underneath it. It is *scripted*: it fires once, at a tick the map file chooses,
and nobody watching can cause it. This module makes the same capability
something an operator invokes at will, on any tile.

Three rules shape everything here.

**A disruption is felt, not read.** `sim/world.py` already refuses to hand
agents an escalation's tile list, on the grounds that ground truth arriving
without a robot sensing it is not perception. An intervention is the same kind
of event, so it goes through the same door: agents watch a *count* and re-decide
against what they can now observe. That is why this module returns tiles to the
world and to the operator, and nothing at all to the fleet.

**The operator disrupts the world, never a robot.** There is no "send L1 to
(4,9)" here and there should not be. The project's thesis is that coordination
falls out of shared memory rather than out of a channel; an operator with a
direct line to a robot is that channel, wearing a different hat.

**A disruption may not make the mission unwinnable.** An operator exploring what
the fleet can survive is the point; an operator accidentally sealing the last
victim behind fire and watching the run die is a bug report about us. See
`would_strand`.

Deliberately **not** wired into `run_mission`: that is the seeded, deterministic
path the golden cassette is recorded from (§4.8), and an operator pressing a
button is by definition not reproducible. Interventions are live-only, the same
call §5.1 makes for the heartbeat scan.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from world.map_format import DEBRIS, FIRE, RUBBLE_HEAVY

# What an operator may drop, and the tile object each one leaves behind.
#
# Only `fire` is permanent. Debris and rubble are *work*: a lifter clears them
# in 3 and 6 ticks (§3.3), so dropping them re-prices routes and creates jobs
# rather than closing anything off forever. That asymmetry is why the stranding
# check below only has to reason about fire.
COLLAPSE, RUBBLE, BLAZE = "collapse", "rubble", "fire"

KINDS: dict[str, dict[str, Any]] = {
    COLLAPSE: {
        "object": DEBRIS,
        "severity": 2,
        "label": "collapse",
        "clearable": True,
    },
    RUBBLE: {
        "object": RUBBLE_HEAVY,
        "severity": 3,
        "label": "heavy rubble",
        "clearable": True,
    },
    BLAZE: {
        "object": FIRE,
        "severity": 4,
        "label": "fire",
        "clearable": False,
    },
}

# A diamond rather than a square: Manhattan distance is the metric robots
# actually move in, so the blast is the shape of the delay it causes.
MAX_RADIUS = 3


class InterventionError(ValueError):
    """A disruption the world refuses. Carries `reason` so the API can say why
    rather than returning a bare 400 the operator has to guess at."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


@dataclass(frozen=True)
class Intervention:
    """A validated disruption, ready to apply. Immutable so the row written to
    fleet memory and the tiles handed to the world cannot drift apart."""

    kind: str
    origin: tuple[int, int]
    radius: int
    tiles: list[tuple[int, int]]
    caused_by: str = "operator"

    @property
    def object(self) -> str:
        return KINDS[self.kind]["object"]

    @property
    def severity(self) -> int:
        return KINDS[self.kind]["severity"]

    @property
    def label(self) -> str:
        return KINDS[self.kind]["label"]

    def area(self) -> dict[str, Any]:
        """The `hazards.area` JSONB payload. Tiles are stored as pairs rather
        than as objects: the column is read back by the console and by the
        changefeed listener, and a compact shape keeps a radius-3 diamond well
        inside a sensible row."""
        return {
            "origin": list(self.origin),
            "radius": self.radius,
            "tiles": [list(t) for t in self.tiles],
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "origin": list(self.origin),
            "radius": self.radius,
            "tiles": [list(t) for t in self.tiles],
            "caused_by": self.caused_by,
        }


@dataclass
class Applied:
    """What actually changed when an intervention landed.

    `tiles` is the subset that was modified — a diamond dropped near a wall
    covers fewer tiles than its radius suggests, and the operator should be told
    what happened rather than what was asked for.
    """

    intervention: Intervention
    tiles: list[tuple[int, int]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            **self.intervention.to_json(),
            "tiles_changed": [list(t) for t in self.tiles],
        }


def diamond(x: int, y: int, radius: int) -> list[tuple[int, int]]:
    """Every tile within `radius` Manhattan steps of (x, y), origin included."""
    return [
        (x + dx, y + dy)
        for dy in range(-radius, radius + 1)
        for dx in range(-radius, radius + 1)
        if abs(dx) + abs(dy) <= radius
    ]


def plan(
    world: Any,
    kind: str,
    x: int,
    y: int,
    radius: int = 1,
    caused_by: str = "operator",
) -> Intervention:
    """Validate a requested disruption and return it, or raise.

    Everything an operator can get wrong is caught here rather than partway
    through mutating the grid: a half-applied intervention would leave the world
    in a state no seed reproduces.
    """
    if kind not in KINDS:
        raise InterventionError(
            f"unknown intervention kind {kind!r}",
            {"known": sorted(KINDS)},
        )
    if not isinstance(radius, int) or not 0 <= radius <= MAX_RADIUS:
        raise InterventionError(
            f"radius must be an integer in 0..{MAX_RADIUS}",
            {"radius": radius},
        )
    if not (0 <= x < world.map.width and 0 <= y < world.map.height):
        raise InterventionError(
            f"({x},{y}) is outside the {world.map.width}x{world.map.height} map",
            {"width": world.map.width, "height": world.map.height},
        )

    # Walls are already impassable, so writing an object onto one changes
    # nothing and would only make `tiles_changed` lie to the renderer.
    tiles = [
        (tx, ty)
        for tx, ty in diamond(x, y, radius)
        if 0 <= tx < world.map.width
        and 0 <= ty < world.map.height
        and world.ground[ty][tx] != "wall"
    ]
    if not tiles:
        raise InterventionError(
            f"nothing to disrupt at ({x},{y}) — every tile in range is wall",
            {"origin": [x, y], "radius": radius},
        )

    intervention = Intervention(
        kind=kind,
        origin=(x, y),
        radius=radius,
        tiles=tiles,
        caused_by=caused_by,
    )

    stranded = would_strand(world, intervention)
    if stranded:
        raise InterventionError(
            "that would seal off "
            + ", ".join(sorted(stranded))
            + " with no route left for a ground robot",
            {"stranded": sorted(stranded)},
        )
    return intervention


def would_strand(world: Any, intervention: Intervention) -> list[str]:
    """Victim ids this intervention would cut off for good, or [].

    Only fire can do it. Debris and rubble are clearable by a lifter, so a
    route through them is slow rather than absent — treating them as blocking
    here would refuse interventions that merely make the mission harder, which
    is the entire point of the feature.

    Reachability is computed for a *ground* robot, because scouts fly and
    scouts cannot rescue anybody. A victim only a scout can reach is stranded
    as far as the mission is concerned.
    """
    if KINDS[intervention.kind]["clearable"]:
        return []

    blocked = set(intervention.tiles)
    pending = [
        v
        for v in world.victims.values()
        if v.state not in ("stabilized", "lost")
    ]
    if not pending:
        return []

    # Fire dropped directly on a victim is a judgement call the operator is
    # allowed to make — it kills that victim rather than hiding them, and the
    # vitals clock already models exactly that. What we refuse is *unreachable*.
    reachable = _ground_reachable(world, blocked)
    return [v.id for v in pending if (v.x, v.y) not in reachable]


def from_area(kind: str, area: dict[str, Any]) -> Intervention:
    """Rebuild an intervention from a `hazards.area` payload.

    The tile list is stored rather than re-derived from the origin and radius.
    Re-deriving would mean the listener recomputing what the API already
    validated — and the two would agree right up until one of them learned to
    skip walls and the other did not.
    """
    return Intervention(
        kind=kind,
        origin=(int(area["origin"][0]), int(area["origin"][1])),
        radius=int(area.get("radius", 0)),
        tiles=[(int(t[0]), int(t[1])) for t in area.get("tiles", [])],
        caused_by=area.get("caused_by", "operator"),
    )


class InterventionWatch:
    """How a written intervention reaches the running mission (issue #22).

    Two transports behind one `pending()`:

        changefeed  a `HazardFeed` on `hazards`. The spike measured an unblock
                    reaching a listener in 0.09-0.11s against a 1 Hz poll's
                    1.0s tail, and that gap is the difference between a button
                    that responds and one that appears broken.
        poll        `active_hazards`, for the fake and for any cluster the feed
                    will not start against.

    The fallback is not a nicety. `changefeed.py` has been a spike precisely
    because nothing depended on it; putting it on the critical path of the
    headline feature means it now has to fail like everything else here does —
    softly, and out loud. A cluster with rangefeeds off degrades to a poll and
    says so, rather than taking the demo down.

    Dedupe by id is required on **both** paths and for different reasons: a
    changefeed carries every write to a row, so a hazard later deactivated
    arrives twice, and a poll re-reads rows it has already returned by design.
    """

    def __init__(self, mem: Any, mission_id: Any, dsn: str | None = None):
        self.mem = mem
        self.mission_id = mission_id
        self.dsn = dsn
        self.transport = "poll"
        self.error: str | None = None
        self._feed: Any = None
        self._seen: set[Any] = set()

    def start(self) -> InterventionWatch:
        """Try the changefeed; fall back to polling if it will not start.

        Only attempted against a real client — the fake has nothing to stream
        from, and `hasattr` rather than an isinstance check keeps this module
        from importing the client just to name it.
        """
        if not hasattr(self.mem, "conn"):
            return self
        try:
            from fleetmem.changefeed import HazardFeed, ensure_enabled

            ensure_enabled(self.mem.conn)
            self._feed = HazardFeed(mission_id=self.mission_id, dsn=self.dsn).start()
            self.transport = "changefeed"
        except Exception as exc:  # noqa: BLE001 - any failure means poll instead
            self._feed = None
            self.error = f"{type(exc).__name__}: {exc}"
            self.transport = "poll"
        return self

    def pending(self) -> list[Intervention]:
        """Interventions written since the last call, oldest first."""
        if self._feed is not None:
            # A feed that died mid-mission must not silently stop delivering
            # interventions — drop to polling and keep the operator working.
            if self._feed.error is not None:
                self.error = f"feed stopped: {self._feed.error}"
                self.transport = "poll"
                self._feed.stop()
                self._feed = None
            else:
                return self._collect(
                    (c.hazard_id, c.intervention_kind, c.area)
                    for c in self._feed.interventions()
                )
        try:
            rows = self.mem.active_hazards(self.mission_id, interventions_only=True)
        except Exception as exc:  # noqa: BLE001 - a disruption, not a mission
            self.error = f"{type(exc).__name__}: {exc}"
            return []
        return self._collect((h.id, h.intervention_kind, h.area) for h in rows)

    def _collect(self, rows: Any) -> list[Intervention]:
        found: list[Intervention] = []
        for hazard_id, kind, area in rows:
            if hazard_id in self._seen or kind not in KINDS:
                continue
            self._seen.add(hazard_id)
            try:
                found.append(from_area(kind, area))
            except (KeyError, TypeError, ValueError, IndexError):
                # A malformed area is one bad row, not a dead mission. It is
                # already marked seen, so it cannot spin.
                continue
        return found

    def stop(self) -> None:
        if self._feed is not None:
            self._feed.stop()
            self._feed = None


def _ground_reachable(world: Any, extra_blocked: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Flood fill from every ground robot over tiles a lifter could eventually
    open. Walls and fire stop it; debris and rubble do not."""
    starts = [(r.x, r.y) for r in world.robots.values() if not r.flying]
    seen: set[tuple[int, int]] = set()
    frontier: deque[tuple[int, int]] = deque()
    for start in starts:
        if start not in seen:
            seen.add(start)
            frontier.append(start)

    while frontier:
        x, y = frontier.popleft()
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < world.map.width and 0 <= ny < world.map.height):
                continue
            if (nx, ny) in seen or (nx, ny) in extra_blocked:
                continue
            if world.ground[ny][nx] == "wall":
                continue
            if world.objects[ny][nx] == FIRE:
                continue
            seen.add((nx, ny))
            frontier.append((nx, ny))
    return seen
