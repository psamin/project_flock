"""map.json — the world format (PRD §4.8). Contract #2, frozen Aug 3.

Lane 5 authors maps, lane 3 loads them, the sim server treats a map as the
initial world state. The validator exists so a bad map fails at load with a
readable message instead of halfway through a demo.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Tile types (§3.3). `ground` holds terrain, `objects` holds what sits on it.
OPEN, WALL, DOOR = "open", "wall", "door"
DEBRIS, RUBBLE_HEAVY, FIRE, UNSTABLE = "debris", "rubble_heavy", "fire", "unstable"

GROUND_TILES = {OPEN, WALL, DOOR, UNSTABLE}
OBJECT_TILES = {DEBRIS, RUBBLE_HEAVY, FIRE}
EMPTY = ""

ROLES = {"scout", "lifter", "medic", "relay"}
VICTIM_STATES = {"unknown", "located", "access_blocked", "reachable", "stabilized", "lost"}


class MapError(ValueError):
    """A map that cannot be loaded, with the reason."""


DEFAULT_MISSION_TICKS = 1200   # §3.3: the mission ends at tick 1200


@dataclass(frozen=True)
class WorldMap:
    width: int
    height: int
    tile_size: int
    ground: list[list[str]]      # [y][x]
    objects: list[list[str]]     # [y][x], EMPTY where nothing sits
    zones: list[dict[str, Any]]
    spawn_points: dict[str, list[dict[str, int]]]
    victims: list[dict[str, Any]]
    escalations: list[dict[str, Any]]
    # Mission metadata. Carried through rather than dropped on load: the tick
    # server needs `mission_length_ticks` to know when the mission ends, and
    # `seed` is what makes a demo run reproducible (§4.8 "same seed + same action
    # log = same mission").
    name: str = ""
    description: str = ""
    seed: int | None = None
    mission_length_ticks: int = DEFAULT_MISSION_TICKS

    def ground_at(self, x: int, y: int) -> str:
        return self.ground[y][x]

    def object_at(self, x: int, y: int) -> str:
        return self.objects[y][x]

    def passable(self, x: int, y: int, *, flying: bool = False) -> bool:
        """Whether a robot can enter. Scouts fly, so debris does not stop them;
        fire and walls stop everyone (§3.3)."""
        if not (0 <= x < self.width and 0 <= y < self.height):
            return False
        if self.ground_at(x, y) == WALL:
            return False
        obj = self.object_at(x, y)
        if obj == FIRE:
            return False
        if flying:
            return True
        return obj not in (DEBRIS, RUBBLE_HEAVY)

    def zone_at(self, x: int, y: int) -> str | None:
        for zone in self.zones:
            if (zone["x"] <= x < zone["x"] + zone["width"]
                    and zone["y"] <= y < zone["y"] + zone["height"]):
                return zone["name"]
        return None


def load_map(path: str | Path) -> WorldMap:
    return parse_map(json.loads(Path(path).read_text()))


def parse_map(data: dict[str, Any]) -> WorldMap:
    for key in ("width", "height", "tile_size", "layers", "zones",
                "spawn_points", "victims", "escalations"):
        if key not in data:
            raise MapError(f"map is missing required key {key!r}")

    width, height = data["width"], data["height"]
    if width <= 0 or height <= 0:
        raise MapError(f"map dimensions must be positive, got {width}x{height}")

    layers = data["layers"]
    for name in ("ground", "objects"):
        if name not in layers:
            raise MapError(f"layers is missing {name!r}")

    ground = _grid(layers["ground"], width, height, "ground", GROUND_TILES)
    objects = _grid(layers["objects"], width, height, "objects", OBJECT_TILES | {EMPTY})

    for zone in data["zones"]:
        for key in ("name", "x", "y", "width", "height"):
            if key not in zone:
                raise MapError(f"zone {zone.get('name', '?')!r} is missing {key!r}")
        if zone["x"] + zone["width"] > width or zone["y"] + zone["height"] > height:
            raise MapError(f"zone {zone['name']!r} extends past the map bounds")

    for role, points in data["spawn_points"].items():
        if role not in ROLES:
            raise MapError(f"unknown spawn role {role!r}; expected one of {sorted(ROLES)}")
        for point in points:
            _in_bounds(point["x"], point["y"], width, height, f"{role} spawn")

    for victim in data["victims"]:
        _in_bounds(victim["x"], victim["y"], width, height, "victim")
        if not 400 <= victim["vitals_deadline"] <= 700:
            raise MapError(
                f"victim at ({victim['x']},{victim['y']}) has vitals_deadline "
                f"{victim['vitals_deadline']}, outside the 400-700 range in §3.3"
            )
        state = victim.get("state", "unknown")
        if state not in VICTIM_STATES:
            raise MapError(f"victim state {state!r} is not one of {sorted(VICTIM_STATES)}")

    # Type-check before comparing: a JSON string or null here would raise
    # TypeError out of the validator, which defeats the point of having one —
    # the caller gets a stack trace instead of a message naming the bad field.
    mission_ticks = _positive_int(
        data.get("mission_length_ticks", DEFAULT_MISSION_TICKS), "mission_length_ticks"
    )
    seed = data.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise MapError(f"seed must be an integer or absent, got {seed!r}")
    for key in ("name", "description"):
        if not isinstance(data.get(key, ""), str):
            raise MapError(f"{key} must be a string, got {data[key]!r}")

    for esc in data["escalations"]:
        if "tick" not in esc or "kind" not in esc:
            raise MapError("every escalation needs a 'tick' and a 'kind'")
        if isinstance(esc["tick"], bool) or not isinstance(esc["tick"], int):
            raise MapError(f"escalation tick must be an integer, got {esc['tick']!r}")
        if esc["tick"] < 0:
            raise MapError(f"escalation tick must be non-negative, got {esc['tick']}")
        # An escalation past the end of the mission never fires. Scheduling the
        # aftershock at 1300 in a 1200-tick mission would quietly remove the
        # replanning beat the demo is built around.
        if esc["tick"] >= mission_ticks:
            raise MapError(
                f"escalation {esc['kind']!r} is scheduled at tick {esc['tick']},"
                f" at or past the mission end ({mission_ticks}) — it would never fire"
            )

    # Deep-copied rather than aliased: _grid already copies the two layers, and
    # leaving the rest pointing into the caller's dict means a mutation on
    # either side silently changes the other. Not made deeply immutable — the
    # tick server will want plain dicts to work with, and frozen records
    # everywhere would be ceremony for a hazard this copy already removes.
    return WorldMap(
        width=width, height=height, tile_size=data["tile_size"],
        ground=ground, objects=objects,
        zones=deepcopy(data["zones"]),
        spawn_points=deepcopy(data["spawn_points"]),
        victims=deepcopy(data["victims"]),
        escalations=deepcopy(data["escalations"]),
        name=data.get("name", ""), description=data.get("description", ""),
        seed=seed, mission_length_ticks=mission_ticks,
    )


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MapError(f"{field} must be an integer, got {value!r}")
    if value <= 0:
        raise MapError(f"{field} must be positive, got {value}")
    return value


def _grid(rows: Any, width: int, height: int, name: str, allowed: set[str]) -> list[list[str]]:
    if len(rows) != height:
        raise MapError(f"{name} layer has {len(rows)} rows, expected {height}")
    for y, row in enumerate(rows):
        if len(row) != width:
            raise MapError(f"{name} layer row {y} has {len(row)} tiles, expected {width}")
        for x, tile in enumerate(row):
            if tile not in allowed:
                raise MapError(
                    f"{name} layer has unknown tile {tile!r} at ({x},{y});"
                    f" expected one of {sorted(allowed)}"
                )
    return [list(row) for row in rows]


def _in_bounds(x: int, y: int, width: int, height: int, what: str) -> None:
    if not (0 <= x < width and 0 <= y < height):
        raise MapError(f"{what} at ({x},{y}) is outside the {width}x{height} map")
