"""Interface contracts 2 and 3 (PRD §5.2) — frozen Aug 3.

Contract 2, agent -> sim:  move(dir) | act(verb, target) | idle
Contract 3, sim -> browser: a state frame per tick, full snapshot then diffs.

Both live here so neither side can quietly redefine the wire format, and both
are validated: an agent that sends nonsense gets a rejection with a reason, not
a traceback in the tick loop.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# --- contract 2: actions ----------------------------------------------------

MOVE, ACT, IDLE = "move", "act", "idle"

# Screen convention: +y is down, matching map.json row order and the renderer.
DIRECTIONS: dict[str, tuple[int, int]] = {
    "n": (0, -1),
    "s": (0, 1),
    "e": (1, 0),
    "w": (-1, 0),
}

# Verbs a robot can perform on a target tile. Roles restrict these further
# (§3.3): only a lifter clears debris, only a medic stabilizes.
#
# `recharge` and `restock` are part of the vocabulary but the world rejects them
# with "not implemented yet" — battery and kit logistics are lane 2's work. They
# parse so the contract does not have to change when that lands; they are listed
# as unimplemented in the README so nobody builds against a rejection.
IMPLEMENTED_VERBS = {"clear_debris", "stabilize"}
VERBS = IMPLEMENTED_VERBS | {"recharge", "restock"}


class InvalidAction(ValueError):
    """An action the server refuses, with a reason the agent can log."""


@dataclass(frozen=True)
class Action:
    kind: Literal["move", "act", "idle"]
    direction: str | None = None
    verb: str | None = None
    target: tuple[int, int] | None = None

    @classmethod
    def move(cls, direction: str) -> "Action":
        return cls(kind=MOVE, direction=direction)

    @classmethod
    def act(cls, verb: str, target: tuple[int, int]) -> "Action":
        return cls(kind=ACT, verb=verb, target=target)

    @classmethod
    def idle(cls) -> "Action":
        return cls(kind=IDLE)

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Action":
        """Validate an action off the wire. Shape only — whether the move is
        *legal* depends on the world and is decided in World.apply()."""
        kind = data.get("kind")
        if kind == MOVE:
            direction = data.get("direction")
            if direction not in DIRECTIONS:
                raise InvalidAction(
                    f"unknown direction {direction!r}; expected one of {sorted(DIRECTIONS)}"
                )
            return cls.move(direction)
        if kind == ACT:
            verb = data.get("verb")
            if verb not in VERBS:
                raise InvalidAction(
                    f"unknown verb {verb!r}; expected one of {sorted(VERBS)}"
                )
            target = data.get("target")
            if (
                not isinstance(target, (list, tuple))
                or len(target) != 2
                or not all(
                    isinstance(v, int) and not isinstance(v, bool) for v in target
                )
            ):
                raise InvalidAction(
                    f"act needs an integer [x, y] target, got {target!r}"
                )
            return cls.act(verb, (target[0], target[1]))
        if kind == IDLE:
            return cls.idle()
        raise InvalidAction(f"unknown action kind {kind!r}; expected move, act or idle")

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


# --- contract 3: state frames -----------------------------------------------


@dataclass
class StateFrame:
    """One tick of truth, as the browser receives it.

    `snapshot` frames carry the whole world and are sent once on connect;
    every later frame is a diff — `tiles_changed` rather than the full grid
    (§4.8). Robots and victims are small enough to send whole each tick.
    """

    tick: int
    kind: Literal["snapshot", "diff"] = "diff"
    robots: list[dict[str, Any]] = field(default_factory=list)
    victims: list[dict[str, Any]] = field(default_factory=list)
    tiles_changed: list[dict[str, Any]] = field(default_factory=list)
    # Tiles revealed this tick (FR-8). Sent as a delta for the same reason as
    # tiles_changed: the explored set grows to the size of the map, and resending
    # it every tick at 4 Hz would dwarf every other field in the frame.
    explored: list[list[int]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    # snapshot only — the initial world the client renders before diffs arrive
    world: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        if self.world is None:
            data.pop("world")
        return data
