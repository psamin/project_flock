"""Run missions headless so the fleet has tactics before anyone is watching.

The demo's claim is that a fleet which has run missions before is better than
one that has not, and that claim needs a before. This runs those missions — the
ones nobody watches — deriving a lesson or two from each.

    uv run python -m sim.seed_memory              # one run on the demo map
    uv run python -m sim.seed_memory --runs 3     # more experience to draw on
    uv run python -m sim.seed_memory --map other.json

Deliberately thin: `run_mission` is already the seeded, deterministic batch path
(§4.8), so this is a loop and a print, not new machinery.

Worth running across *different* maps once there are some. What is stored are
tactics rather than places, so experience from one scenario is exactly what
should help on the next — and a fleet whose lessons all came from one map has
learned that map, not the job.
"""

from __future__ import annotations

import argparse
import os
import sys

from bedrock.adapter import adapter_from_env
from fleetmem.client import CockroachFleetMem
from sim.mission import run_mission
from world.map_format import load_map

DEFAULT_MAP = os.environ.get("COLONY_MAP", "world/maps/aftershock.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", default=DEFAULT_MAP)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()

    world_map = load_map(args.map)
    embedder = adapter_from_env()

    # The real client, never the fake: a memory written to an in-memory store
    # dies with this process, and the whole point is that the *next* process
    # finds it. If there is no cluster this should fail loudly rather than
    # appear to work.
    mem = CockroachFleetMem()
    try:
        before = len(mem.recall_lessons(None, limit=100))
        for i in range(args.runs):
            run = run_mission(
                world_map,
                mem,
                coordinated=True,
                seed=world_map.seed,
                embedder=embedder,
                remember=True,
            )
            m = run.metrics
            print(
                f"run {i + 1}/{args.runs}: {m.victims_stabilized}/{m.victims_total} "
                f"rescued in {m.ticks} ticks"
            )
        after = mem.recall_lessons(None, limit=100)
        print(f"\ntactics: {before} before, {len(after)} after")
        for m in after[: len(after) - before or None]:
            print(f"  when {m.situation}\n    -> {m.lesson}")

        # Seeding that learns nothing is the failure this script exists to
        # prevent, and it is silent by default: `derive_lessons` returns []
        # on a cassette miss (adapter.py:244), so a full mission runs, prints
        # its rescue count, and stores no tactic. `make demo` then starts with
        # SEMANTIC memory empty while announcing "a mission that draws on it".
        #
        # It misses because the cassette is keyed on a sha256 of the run
        # digest and the digest embeds the tick count, which is not stable:
        # three identical resets of the same map and seed measured 327, 327,
        # 337 ticks. So a miss is expected some of the time, and the only
        # wrong thing to do about it is nothing.
        #
        # Non-zero rather than a retry: re-running until the dice land on a
        # recorded digest would hide the non-determinism that causes this,
        # and that is a finding, not a hiccup to paper over.
        if len(after) == before:
            print(
                "\nERROR: no tactics were learned.\n"
                "  The cassette had no entry for this run's digest, so the\n"
                "  fleet finished the mission and generalised nothing.\n"
                "  Re-run to try again, or record with COLONY_BEDROCK_MODE=record.",
                file=sys.stderr,
            )
            return 1
    finally:
        mem.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
