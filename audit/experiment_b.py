"""AUDIT B evidence: lease contention, provenance depth, cross-agent dependency.

Instruments one coordinated run against the fake and answers B.4, B.6, B.7 with
counted data rather than inspection.

Run: PYTHONPATH=. uv run python ../audit/experiment_b.py
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from bedrock.adapter import REPLAY, BedrockAdapter
from fleetmem.fake import FakeFleetMem
from sim.mission import run_mission
from world.map_format import load_map

MAP = Path(__file__).resolve().parent.parent / "colony" / "world" / "maps" / "aftershock.json"


class CountingMem(FakeFleetMem):
    """Counts claim attempts and how many lost the race (B.4)."""

    def __init__(self):
        super().__init__()
        self.claim_attempts = 0
        self.claim_wins = 0
        self.contended: Counter = Counter()
        self.db_calls = Counter()

    def claim_task(self, task_id, robot_id, *a, **k):
        self.claim_attempts += 1
        won = super().claim_task(task_id, robot_id, *a, **k)
        if won:
            self.claim_wins += 1
        else:
            self.contended[str(task_id)] += 1
        return won

    # X7: query counter
    def open_tasks(self, *a, **k):
        self.db_calls["open_tasks"] += 1
        return super().open_tasks(*a, **k)

    def get_beliefs(self, *a, **k):
        self.db_calls["get_beliefs"] += 1
        return super().get_beliefs(*a, **k)

    def report_observation(self, *a, **k):
        self.db_calls["report_observation"] += 1
        return super().report_observation(*a, **k)


def main() -> int:
    mem = CountingMem()
    run = run_mission(
        load_map(MAP), mem, coordinated=True, seed=0, embedder=BedrockAdapter(mode=REPLAY)
    )
    mid = run.mission_id

    print("=== B.4 lease contention ===")
    lost = mem.claim_attempts - mem.claim_wins
    print(f"claim attempts: {mem.claim_attempts}")
    print(f"claims won:     {mem.claim_wins}")
    print(f"claims lost (contended): {lost}")
    print(f"distinct contended tasks: {len(mem.contended)}")
    print("VERDICT:", "contention EXERCISED" if lost else
          "contention NEVER OCCURRED — the interesting branch is unproven")

    print("\n=== B.6 provenance (plans.based_on) ===")
    plans = mem.plans_for(mid)
    with_sources = [p for p in plans if p.based_on]
    depths = [len(p.based_on) for p in plans]
    print(f"plan rows: {len(plans)}")
    print(f"with non-null based_on: {len(with_sources)} "
          f"({100 * len(with_sources) / max(1, len(plans)):.0f}%)")
    print(f"max based_on width: {max(depths) if depths else 0}")
    print(f"mean based_on width: {sum(depths) / max(1, len(depths)):.2f}")
    srcs = Counter((p.chosen or {}).get("source", "?") for p in plans)
    print("decision source:", dict(srcs))

    print("\n=== B.7 cross-agent dependency: A acted only because B wrote ===")
    beliefs = {str(b.id): b for b in mem.get_beliefs(mid)}
    found = None
    for p in plans:
        for src in p.based_on:
            b = beliefs.get(str(src))
            if b is not None and b.robot_id and b.robot_id != p.robot_id:
                found = (p, b)
                break
        if found:
            break
    if found:
        p, b = found
        print("FOUND")
        print(f"  observation row: id={b.id} author={b.robot_id} kind={b.kind} "
              f"pos={b.pos} sightings={b.sightings}")
        print(f"  plan row:        robot={p.robot_id} trigger={p.trigger} "
              f"chosen={p.chosen}")
        print(f"  rationale:       {p.rationale!r}")
        print(f"  -> {p.robot_id} acted on a belief authored by {b.robot_id}")
    else:
        print("NOT FOUND — no plan cites a belief authored by another robot.")
        print("This would be the headline finding of the whole audit.")

    print("\n=== X7 query counter ===")
    print(dict(mem.db_calls), f"over {run.metrics.ticks} ticks")
    total = sum(mem.db_calls.values())
    print(f"total store reads/writes: {total}  "
          f"({total / max(1, run.metrics.ticks):.1f} per tick)")

    print("\n=== headline metrics for this run ===")
    print(run.metrics.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
