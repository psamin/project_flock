"""X1 ablation + X2 determinism, run against the in-memory fake (no cluster needed).

Three arms, because AUDIT B found the shipped baseline is not a clean ablation:

    A  coordinated      as shipped
    B  baseline         as shipped — still calls open_tasks() on the shared
                        task table (worker.py:264, scout.py:444), so it reads
                        rows authored by other robots
    C  isolated         the ablation the plan actually asks for: every robot
                        gets its own fleet memory, so writes still happen but
                        no robot can read another's rows. Events go to a shared
                        log ONLY so metrics remain computable.

Arm C is the honest "disable cross-agent reads" condition. If A and C do not
separate, the project's central claim is false.

Run: uv run python ../audit/experiment_x1.py [n_seeds]
"""

from __future__ import annotations

import json
import statistics
import sys
import uuid
from pathlib import Path

from agents.scout import Scout
from bedrock.adapter import REPLAY, BedrockAdapter
from fleetmem.fake import FakeFleetMem
from sim import metrics as metrics_mod
from sim.metrics import COVERAGE_AT_TICK
from sim.mission import build_fleet, run_mission
from sim.world import World
from world.map_format import load_map

MAP = Path(__file__).resolve().parent.parent / "colony" / "world" / "maps" / "aftershock.json"


class Isolated:
    """One robot's private fleet memory.

    Every write lands in this robot's own store, so nothing it writes is
    readable by any other robot. `log_event` and `events` are forwarded to a
    shared log purely so the §4.7 metrics can still be computed from one event
    stream — no agent ever reads events, so this leaks no coordination.
    """

    def __init__(self, shared: FakeFleetMem):
        self._own = FakeFleetMem()
        self._shared = shared

    def log_event(self, *args, **kwargs):
        return self._shared.log_event(*args, **kwargs)

    def events(self, *args, **kwargs):
        return self._shared.events(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._own, name)


def _replay_adapter() -> BedrockAdapter:
    """Explicitly replay: no network, no spend, deterministic."""
    return BedrockAdapter(mode=REPLAY)


def run_isolated(world_map, *, seed: int, max_ticks: int | None = None):
    """Arm C: baseline behaviour, but cross-agent reads are impossible."""
    world = World(world_map, seed=seed)
    world.shared_vision = False
    mission_id = uuid.uuid4()
    shared = FakeFleetMem()
    embedder = _replay_adapter()

    agents_by_id = build_fleet(
        world, shared, mission_id, coordinated=False, seed=seed, embedder=embedder
    )
    # The ablation itself: swap each agent onto a private store.
    for agent in agents_by_id.values():
        agent.mem = Isolated(shared)
        if isinstance(agent, Scout):
            # Scouts re-register themselves into their own store so their own
            # writes are self-consistent.
            agent.mem.register_robot(
                agent.robot_id,
                world.robots[agent.robot_id].role,
                (world.robots[agent.robot_id].x, world.robots[agent.robot_id].y),
                world.robots[agent.robot_id].battery,
            )

    agents = list(agents_by_id.values())
    limit = max_ticks or world_map.mission_length_ticks
    coverage_at_500 = 0.0
    for _ in range(limit):
        frame = world.step({a.robot_id: a.step(world) for a in agents})
        for event in frame.events:
            shared.log_event(mission_id, event["actor"], event["verb"], event["detail"])
        if world.tick == COVERAGE_AT_TICK:
            coverage_at_500 = world.coverage()
        if world.finished:
            break
    if world.tick < COVERAGE_AT_TICK:
        coverage_at_500 = world.coverage()

    return metrics_mod.compute(
        shared.events(mission_id),
        victims_total=len(world.victims),
        coverage_at_500=coverage_at_500,
        ticks=world.tick,
        horizon=world_map.mission_length_ticks,
    )


def ci95(values: list[float]) -> tuple[float, float]:
    """Mean and 95% half-width. Normal approximation; n=20 is adequate."""
    if len(values) < 2:
        return (values[0] if values else 0.0), 0.0
    mean = statistics.fmean(values)
    half = 1.96 * statistics.stdev(values) / (len(values) ** 0.5)
    return mean, half


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    world_map = load_map(MAP)
    arms: dict[str, list[float]] = {"A_coordinated": [], "B_baseline": [], "C_isolated": []}
    ticks: dict[str, list[float]] = {k: [] for k in arms}

    for seed in range(n):
        a = run_mission(world_map, FakeFleetMem(), coordinated=True, seed=seed,
                        embedder=_replay_adapter()).metrics
        b = run_mission(world_map, FakeFleetMem(), coordinated=False, seed=seed,
                        embedder=_replay_adapter()).metrics
        c = run_isolated(world_map, seed=seed)
        for key, m in (("A_coordinated", a), ("B_baseline", b), ("C_isolated", c)):
            arms[key].append(m.rescue_rate)
            ticks[key].append(m.ticks)
        print(f"seed {seed:2d}  A={a.rescue_rate:.3f}  B={b.rescue_rate:.3f}  "
              f"C={c.rescue_rate:.3f}", flush=True)

    print(f"\n=== X1 ablation: rescue rate, mean +/- 95pct CI, n={n} ===")
    out = {}
    for key, vals in arms.items():
        mean, half = ci95(vals)
        spread = max(vals) - min(vals)
        out[key] = {"mean": mean, "ci95": half, "n": len(vals),
                    "lo": mean - half, "hi": mean + half,
                    "distinct_values": len(set(vals)), "spread": spread}
        print(f"{key:16s} {mean:.3f} +/- {half:.3f}   [{mean-half:.3f}, {mean+half:.3f}]"
              f"   distinct={len(set(vals))} spread={spread:.3f}")

    # Seed sensitivity: if every seed produces the same number, "20 seeds" is
    # 1 seed run 20 times and the CI is degenerate. That is a finding, not a pass.
    degenerate = [k for k, v in arms.items() if len(set(v)) == 1]
    if degenerate:
        print(f"\nWARNING seed has no effect in arms: {degenerate} "
              f"— CIs are zero-width and n does not buy confidence")

    def overlap(p: str, q: str) -> bool:
        return not (out[p]["lo"] > out[q]["hi"] or out[q]["lo"] > out[p]["hi"])

    print("\nCI overlap A vs B:", overlap("A_coordinated", "B_baseline"))
    print("CI overlap A vs C:", overlap("A_coordinated", "C_isolated"))
    print("PASS (A vs C separate):", not overlap("A_coordinated", "C_isolated"))

    (Path(__file__).parent / "x1-raw.json").write_text(
        json.dumps({"summary": out, "rescue_rate": arms, "ticks": ticks}, indent=2)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
