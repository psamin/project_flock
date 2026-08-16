"""Run a mission headless so the next one has something to remember.

The demo's claim is that the fleet gets better at a map it has seen before, and
that claim needs a before. This runs the cold mission — the one nobody watches —
so the mission on screen is genuinely the second.

    uv run python -m sim.seed_memory              # one run on the demo map
    uv run python -m sim.seed_memory --runs 2     # more history to recall
    uv run python -m sim.seed_memory --recall     # let the seed runs learn too

Deliberately thin: `run_mission` is already the seeded, deterministic batch path
(§4.8), so this is a loop and a print, not new machinery.

By default the seed runs have `recall` off — each one learns the map from
scratch and writes down what it found, so N runs give N independent readings
rather than N copies of the first one's opinion. `--recall` turns that off and
lets them compound, which is the honest way to show a fleet improving over a
series rather than over a single step.
"""

from __future__ import annotations

import argparse
import os
import sys

from bedrock.adapter import adapter_from_env
from fleetmem.client import CockroachFleetMem
from sim import recall as recall_mod
from sim.mission import run_mission
from world.map_format import load_map

DEFAULT_MAP = os.environ.get("COLONY_MAP", "world/maps/aftershock.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", default=DEFAULT_MAP)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--recall",
        action="store_true",
        help="let each seed run read what the previous ones learned",
    )
    args = parser.parse_args()

    world_map = load_map(args.map)
    key = recall_mod.map_key(world_map)
    embedder = adapter_from_env()

    # The real client, never the fake: a memory written to an in-memory store
    # dies with this process, and the whole point is that the *next* process
    # finds it. If there is no cluster this should fail loudly rather than
    # appear to work.
    mem = CockroachFleetMem()
    try:
        before = len(mem.recall_missions(key, None, limit=100))
        for i in range(args.runs):
            run = run_mission(
                world_map,
                mem,
                coordinated=True,
                seed=world_map.seed,
                embedder=embedder,
                remember=True,
                recall_enabled=args.recall,
            )
            m = run.metrics
            print(
                f"run {i + 1}/{args.runs}: {m.victims_stabilized}/{m.victims_total} "
                f"rescued in {m.ticks} ticks"
            )
        after = mem.recall_missions(key, None, limit=100)
        print(f"\n{key}: {before} memories before, {len(after)} after")
        if after:
            print(f"  latest: {after[0].summary}")
    finally:
        mem.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
