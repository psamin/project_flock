"""X10 — statistical honesty across *generated scenarios*, not one map reseeded.

X1 ran 20 seeds against Aftershock and arms B and C came back with zero
variance: the shipped baseline rescues exactly 4 of 9 and true isolation exactly
0 of 9, every seed. That is not the experiment being underpowered, it is the map
being one fixed problem — the count of victims reachable without a handoff is a
property of that layout, and reseeding the RNG never changes it.

So a `±` on those arms would describe the harness rather than the fleet.

This varies the *scenario* instead: map size, debris density, victim count,
sector size and fleet composition, using the same generator `tests/test_scenarios.py`
already trusts. Each scenario is one independent problem, so a mean across them
is a claim about the fleet rather than about Aftershock.

Run: PYTHONPATH=. uv run python ../audit/experiment_x10.py [n_scenarios]
"""

from __future__ import annotations

import json
import random
import statistics
import sys
from pathlib import Path

from bedrock.adapter import REPLAY, BedrockAdapter
from fleetmem.fake import FakeFleetMem
from sim.mission import run_mission
from tests.test_scenarios import make_scenario

ARMS = (("coordinated", True), ("baseline", False))

# How many of each scenario's victims are walled in behind rubble. The generator
# in tests/test_scenarios.py deliberately clears every victim tile ("a victim is
# never buried"), which means a medic can always walk straight to one and the
# scout -> lifter -> medic chain never has to form. On those maps coordination
# has nothing to coordinate, and the measured delta is exactly 0.000 — see the
# writeup in audit/experiments.md.
#
# PRD §5.5 says the demo map is "designed so >=2 victims are unreachable without
# handoffs". This reproduces that property across generated maps so the
# comparison is measuring the same mechanism Aftershock measures.
BURIED_FRACTION = 0.5


def bury(world_map, fraction: float = BURIED_FRACTION):
    """Wall a fraction of victims in behind debris, so reaching them needs a lift.

    Mutates the parsed map's object grid before any World copies it. Only the
    four orthogonal neighbours are filled: enough that a medic cannot step in
    until a lifter clears one, without sealing the tile off from the pathfinder
    entirely.
    """
    from world.map_format import DEBRIS, WALL

    victims = list(world_map.victims)
    take = int(len(victims) * fraction)
    buried = 0
    for victim in victims[:take]:
        x, y = victim["x"], victim["y"]
        filled = 0
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < world_map.width and 0 <= ny < world_map.height):
                continue
            if world_map.ground[ny][nx] == WALL:
                continue
            world_map.objects[ny][nx] = DEBRIS
            filled += 1
        if filled:
            buried += 1
    return buried


def scenario(seed: int):
    """One randomly shaped disaster. Ranges kept inside what the generator's own
    tests exercise, so nothing here is a scenario the project has never run."""
    rng = random.Random(seed)
    return make_scenario(
        seed,
        width=rng.choice([20, 24, 28, 32]),
        height=rng.choice([14, 18, 22]),
        debris_ratio=rng.choice([0.10, 0.15, 0.20, 0.25]),
        victims=rng.choice([3, 4, 5, 6]),
        sector_size=rng.choice([5, 6, 8]),
        scouts=rng.choice([1, 2]),
        lifters=rng.choice([1, 2]),
        medics=rng.choice([1, 2]),
    )


def ci95(values):
    if len(values) < 2:
        return (values[0] if values else 0.0), 0.0
    return (
        statistics.fmean(values),
        1.96 * statistics.stdev(values) / (len(values) ** 0.5),
    )


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    rates: dict[str, list[float]] = {k: [] for k, _ in ARMS}
    lost: dict[str, list[float]] = {k: [] for k, _ in ARMS}
    paired: list[float] = []

    buried_total = 0
    for i in range(n):
        world_map = scenario(i)
        buried_total += bury(world_map)
        row = {}
        for name, coord in ARMS:
            FakeFleetMem.reset_ids()
            m = run_mission(
                world_map, FakeFleetMem(), coordinated=coord, seed=i,
                embedder=BedrockAdapter(mode=REPLAY),
            ).metrics
            rates[name].append(m.rescue_rate)
            lost[name].append(m.victims_lost)
            row[name] = m
        paired.append(row["coordinated"].rescue_rate - row["baseline"].rescue_rate)
        print(
            f"scenario {i:2d}  {world_map.width}x{world_map.height} "
            f"v={len(world_map.victims)}  "
            f"coord={row['coordinated'].rescue_rate:.3f} "
            f"base={row['baseline'].rescue_rate:.3f} "
            f"delta={paired[-1]:+.3f}",
            flush=True,
        )

    print(f"\n=== X10: {n} generated scenarios, rescue rate ===")
    out = {}
    for name, _ in ARMS:
        mean, half = ci95(rates[name])
        out[name] = {"mean": mean, "ci95": half, "distinct": len(set(rates[name]))}
        print(f"{name:14s} {mean:.3f} +/- {half:.3f}   "
              f"distinct values={len(set(rates[name]))}")

    dmean, dhalf = ci95(paired)
    out["paired_delta"] = {"mean": dmean, "ci95": dhalf}
    print(f"\npaired delta   {dmean:+.3f} +/- {dhalf:.3f}   "
          f"[{dmean-dhalf:+.3f}, {dmean+dhalf:+.3f}]")
    print("excludes zero:", (dmean - dhalf) > 0)

    lc, lb = statistics.fmean(lost["coordinated"]), statistics.fmean(lost["baseline"])
    out["mean_victims_lost"] = {"coordinated": lc, "baseline": lb}
    print(f"\nmean victims lost   coordinated {lc:.2f}   baseline {lb:.2f}")
    print(f"victims buried across all scenarios: {buried_total}")

    (Path(__file__).parent / "x10-raw.json").write_text(
        json.dumps({"summary": out, "rescue_rate": rates, "paired": paired}, indent=2)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
