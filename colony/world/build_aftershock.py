"""Generate the Aftershock reference map (PRD §3.3).

A reference instance of the map.json contract, not final level design — lane 5
owns the authored art-directed version and the stat tuning. What this pins down
is the *shape* of the file and a playable layout matching the spec: 40x30, four
zones plus a courtyard, 8 victims in the 3/4/1 access mix, fire, and the
aftershock (see ESCALATION_TICK — playtest moved it off §3.3's tick 300).

Deterministic: same seed, same map. Run `make map` to regenerate.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from world.map_format import (
    DEBRIS,
    DOOR,
    EMPTY,
    FIRE,
    OPEN,
    RUBBLE_HEAVY,
    UNSTABLE,
    WALL,
    parse_map,
)

WIDTH, HEIGHT, TILE = 40, 30, 32
SECTOR_W, SECTOR_H = 10, 10  # §3.3: a 4x3 grid of 10x10-tile sectors
SEED = 20260801
OUT = Path(__file__).resolve().parents[1] / "world" / "maps" / "aftershock.json"

# Four zones plus the courtyard that connects them (§3.3).
ZONES = [
    {"name": "staging", "x": 0, "y": 0, "width": 10, "height": 8},
    {"name": "street", "x": 0, "y": 8, "width": 40, "height": 6},
    {"name": "residential", "x": 0, "y": 14, "width": 22, "height": 16},
    {"name": "office", "x": 24, "y": 14, "width": 16, "height": 16},
    {"name": "courtyard", "x": 22, "y": 14, "width": 2, "height": 16},
]


def build() -> dict:
    rng = random.Random(SEED)
    ground = [[OPEN] * WIDTH for _ in range(HEIGHT)]
    objects = [[EMPTY] * WIDTH for _ in range(HEIGHT)]

    # Map border.
    for x in range(WIDTH):
        ground[0][x] = ground[HEIGHT - 1][x] = WALL
    for y in range(HEIGHT):
        ground[y][0] = ground[y][WIDTH - 1] = WALL

    # Staging: walled off from the street with a single door, so leaving base is
    # a deliberate choke point the fleet has to route through.
    for x in range(1, 10):
        ground[7][x] = WALL
    ground[7][4] = DOOR

    # Office interior: rooms on a grid with doors, per §3.3 "multi-room interior,
    # requires door tiles".
    for x in range(25, 39):
        ground[19][x] = WALL
        ground[24][x] = WALL
    for y in range(15, 29):
        ground[y][31] = WALL
    for door in [(19, 28), (19, 35), (24, 28), (24, 35), (21, 31), (26, 31)]:
        ground[door[0]][door[1]] = DOOR

    # Residential block: dense debris, with a heavy-rubble core.
    for y in range(15, 29):
        for x in range(1, 21):
            roll = rng.random()
            if roll < 0.22:
                objects[y][x] = DEBRIS
            elif roll < 0.28:
                objects[y][x] = RUBBLE_HEAVY

    # Two corridors kept deliberately clear — these are what the aftershock
    # re-blocks at tick 300, so they must start passable.
    for y in range(15, 29):
        objects[y][6] = EMPTY
        objects[y][15] = EMPTY

    # Fire seed in the street; it spreads every 25 ticks (§3.3).
    objects[10][30] = FIRE

    # One unstable stretch of street: half speed, scouts only until shored (P1).
    for x in range(20, 26):
        ground[12][x] = UNSTABLE

    victims = _victims()
    _clear_victim_tiles(objects, victims)

    world = {
        "name": "Aftershock",
        "description": "Post-earthquake city block: collapsed residential, office interior, "
        f"spreading fire, aftershock at tick {ESCALATION_TICK}.",
        "width": WIDTH,
        "height": HEIGHT,
        "tile_size": TILE,
        "seed": SEED,
        "mission_length_ticks": 1200,
        "layers": {"ground": ground, "objects": objects},
        "zones": ZONES,
        "sectors": _sectors(),
        "spawn_points": {
            "scout": [{"x": 2, "y": 2}, {"x": 4, "y": 2}],
            "lifter": [{"x": 2, "y": 4}],
            "medic": [{"x": 4, "y": 4}],
        },
        "victims": victims,
        "escalations": _escalations(),
    }
    parse_map(world)  # never write a map that would not load
    return world


def _sectors() -> list[dict]:
    """The 4x3 exploration grid from §3.3 — 12 sectors of 10x10 tiles.

    One `explore_sector` task per sector at mission bootstrap (FR-16), so scouts
    divide the map by claiming rather than by convention. Ids read A1..D3 so an
    event log line like "s1 claimed sector C2" is legible to a judge.
    """
    return [
        {
            "id": f"{chr(ord('A') + col)}{row + 1}",
            "x": col * SECTOR_W,
            "y": row * SECTOR_H,
            "width": SECTOR_W,
            "height": SECTOR_H,
        }
        for row in range(HEIGHT // SECTOR_H)
        for col in range(WIDTH // SECTOR_W)
    ]


def _victims() -> list[dict]:
    """8 victims in the §3.3 access mix: 3 reachable, 4 behind one debris wall,
    1 behind two — the last forces a scout->lifter->lifter->medic chain."""
    return [
        {
            "id": "v1",
            "x": 12,
            "y": 10,
            "vitals_deadline": 700,
            "access": "open",
            "state": "unknown",
        },
        {
            "id": "v2",
            "x": 26,
            "y": 10,
            "vitals_deadline": 650,
            "access": "open",
            "state": "unknown",
        },
        {
            "id": "v3",
            "x": 34,
            "y": 16,
            "vitals_deadline": 620,
            "access": "open",
            "state": "unknown",
        },
        {
            "id": "v4",
            "x": 4,
            "y": 20,
            "vitals_deadline": 560,
            "access": "one_debris",
            "state": "unknown",
        },
        {
            "id": "v5",
            "x": 10,
            "y": 24,
            "vitals_deadline": 540,
            "access": "one_debris",
            "state": "unknown",
        },
        {
            "id": "v6",
            "x": 17,
            "y": 18,
            "vitals_deadline": 520,
            "access": "one_debris",
            "state": "unknown",
        },
        {
            "id": "v7",
            "x": 19,
            "y": 26,
            "vitals_deadline": 500,
            "access": "one_debris",
            "state": "unknown",
        },
        {
            "id": "v8",
            "x": 3,
            "y": 27,
            "vitals_deadline": 470,
            "access": "two_debris",
            "state": "unknown",
        },
    ]


def _ring(x: int, y: int, distance: int) -> list[tuple[int, int]]:
    """Tiles exactly `distance` steps away, inside the map border."""
    return [
        (nx, ny)
        for nx in range(x - distance, x + distance + 1)
        for ny in range(y - distance, y + distance + 1)
        if abs(nx - x) + abs(ny - y) == distance
        and 0 < nx < WIDTH - 1
        and 0 < ny < HEIGHT - 1
    ]


def _clear_victim_tiles(objects: list[list[str]], victims: list[dict]) -> None:
    """Make each victim's `access` field actually true of the map.

    Blocking a single neighbour left every other approach open, so no victim
    ever needed a lifter: measured on the demo map as zero clear_debris tasks in
    a whole mission and the lifter idle from start to finish — with the
    scout->lifter->medic chain the MVP milestone names never once running.

    A victim's own tile stays clear; a medic has to be able to stand on or
    beside it. Difficulty comes from the ring around them, per §3.3.
    """
    for victim in victims:
        x, y = victim["x"], victim["y"]
        objects[y][x] = EMPTY
        access = victim["access"]

        if access == "open":
            for nx, ny in _ring(x, y, 1):
                objects[ny][nx] = EMPTY
            continue

        # Every approach blocked, so exactly one clear opens a way in.
        for nx, ny in _ring(x, y, 1):
            objects[ny][nx] = DEBRIS

        if access == "two_debris":
            # And a second ring behind it, so one clear is not enough: this is
            # §3.3's scout -> lifter -> lifter -> medic victim.
            for nx, ny in _ring(x, y, 2):
                objects[ny][nx] = RUBBLE_HEAVY


# §3.3 specifies tick 300. Playtest #1 says 300 is wrong: a fleet that no longer
# deadlocks clears this map at tick ~250, so the aftershock never fired and the
# replanning beat the demo is built around never happened. Measured at seed 3
# across the range — every value at or below 220 gives 9/9 stabilized with the
# mission running on past tick 300, while 300 itself gives 8/8 and no aftershock
# at all.
#
# 180 rather than the top of that range: the point is not that the shock fires,
# it is that it fires while there is still visible work in flight for it to
# invalidate. At 180 the fleet is mid-chain and the mission runs ~130 ticks
# after it, which is the beat §3.3 actually asks for.
#
# This is the §5.1 "playtest & tune" knob doing its job. It is a deliberate
# deviation from the PRD's number, recorded here rather than only in a commit
# message because the next person to read §3.3 will wonder.
ESCALATION_TICK = 180


def _escalations() -> list[dict]:
    """The aftershock (§3.3): re-blocks two cleared corridors, reveals a new
    victim, converts a street segment to unstable. This is what forces visible
    replanning mid-demo. See ESCALATION_TICK for why it is not at tick 300."""
    return [
        {
            "tick": ESCALATION_TICK,
            "kind": "aftershock",
            "screen_shake": True,
            "block_tiles": (
                [{"x": 6, "y": y, "tile": RUBBLE_HEAVY} for y in range(18, 22)]
                + [{"x": 15, "y": y, "tile": DEBRIS} for y in range(22, 26)]
            ),
            "reveal_victims": [
                {
                    "id": "v9",
                    "x": 28,
                    "y": 26,
                    "vitals_deadline": 400,
                    "state": "unknown",
                }
            ],
            "unstable_tiles": [{"x": x, "y": 9} for x in range(14, 20)],
        },
    ]


def main() -> None:
    world = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(world, indent=1))
    print(
        f"wrote {OUT.relative_to(Path.cwd())} "
        f"({world['width']}x{world['height']}, {len(world['victims'])} victims, "
        f"{len(world['zones'])} zones)"
    )


if __name__ == "__main__":
    main()
