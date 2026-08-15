"""X2 determinism: same seed, two runs, diff the full event log.

Also probes the inverse property X1 surfaced — whether *different* seeds
produce different runs at all. A suite that is deterministic but seed-insensitive
satisfies X2 while making X10's confidence intervals meaningless.

Run: PYTHONPATH=. uv run python ../audit/experiment_x2.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bedrock.adapter import REPLAY, BedrockAdapter
from fleetmem.fake import FakeFleetMem
from sim.mission import run_mission
from world.map_format import load_map

MAP = Path(__file__).resolve().parent.parent / "colony" / "world" / "maps" / "aftershock.json"


def event_log(seed: int, *, coordinated: bool = True) -> list[tuple]:
    """The full ordered event log for one run, normalized for comparison.

    `at` timestamps and row ids are wall-clock/UUID and would differ between any
    two runs; everything else is the behavioural trace.
    """
    run = run_mission(
        load_map(MAP),
        FakeFleetMem(),
        coordinated=coordinated,
        seed=seed,
        embedder=BedrockAdapter(mode=REPLAY),
    )
    return [
        (e["actor"], e["verb"], json.dumps(e["detail"], sort_keys=True, default=str))
        for e in run.mem.events(run.mission_id)
    ]


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def behavioural_log(seed: int, *, coordinated: bool = True) -> list[tuple]:
    """The event log with row ids masked.

    Task and belief ids are freshly generated per run. Masking them separates
    "the fleet did different things" from "the same things carry different
    primary keys", which need completely different fixes.
    """
    return [
        (actor, verb, _UUID_RE.sub("<id>", detail))
        for actor, verb, detail in event_log(seed, coordinated=coordinated)
    ]


def first_divergence(a: list, b: list) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"index {i}:\n    run1={x}\n    run2={y}"
    if len(a) != len(b):
        return f"same prefix, different lengths: {len(a)} vs {len(b)}"
    return "none"


def main() -> int:
    print("=== X2: same seed, two runs ===")
    ok = True
    for seed in (0, 7, 19):
        a, b = event_log(seed), event_log(seed)
        ba, bb = behavioural_log(seed), behavioural_log(seed)
        raw_same, beh_same = a == b, ba == bb
        ok &= beh_same
        print(f"seed {seed:2d}  events={len(a):5d}  byte_identical={raw_same}  "
              f"behaviourally_identical={beh_same}")
        if not beh_same:
            print("  first behavioural divergence:", first_divergence(ba, bb))

    print("\n=== seed sensitivity: different seeds, coordinated arm ===")
    logs = {s: event_log(s) for s in (0, 1, 2, 5)}
    lengths = {s: len(v) for s, v in logs.items()}
    distinct = len({tuple(v) for v in logs.values()})
    print("event counts by seed:", lengths)
    print(f"distinct event logs across 4 seeds: {distinct}/4")

    print("\n=== seed sensitivity: baseline arm ===")
    base = {s: event_log(s, coordinated=False) for s in (0, 1, 2, 5)}
    print("event counts by seed:", {s: len(v) for s, v in base.items()})
    print(f"distinct event logs across 4 seeds: {len({tuple(v) for v in base.values()})}/4")

    print(f"\nX2 PASS: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
