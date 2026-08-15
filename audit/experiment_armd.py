"""Arm D — does omniscient pathing explain arm A's rescue rate?

AUDIT B filed B-1 and B-2: agents read ground truth directly, ungated by
observation radius, in *both* coordination modes.

    worker.py:571-580  _passable  -> world.passable(...)      raw terrain
    scout.py:648       _landing   -> world.passable(...)      raw terrain
    worker.py:584-593  _work_is_done -> world.objects / victim_at

T-12 says remove it. Before removing it, measure it — because if arm A's 0.994
depends on omniscient pathing, "removing god-mode" is a regression wearing a
fix's clothes, and the honest move is to say so rather than to ship a worse
fleet with a cleaner import graph.

Arms:

    A  as shipped               coordinated, omniscient pathing
    D  blind pathing            coordinated, but a tile the robot has not seen
                                is assumed walkable until it bumps into it —
                                the standard fog-of-war treatment, and the only
                                one that does not make unexplored ground a wall
    E  blind pathing + blind    D, plus `_work_is_done` reads beliefs rather
       completion checks        than simulator state

Unobserved ground is treated as *passable*, not impassable. Pessimism would make
every unexplored tile a wall and the fleet would never leave home — that would
measure the pessimism, not the god-mode.

Run: PYTHONPATH=. uv run python ../audit/experiment_armd.py [n_seeds]
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from agents.scout import Scout
from agents.worker import Worker
from bedrock.adapter import REPLAY, BedrockAdapter
from fleetmem.fake import FakeFleetMem
from sim.mission import run_mission
from world.map_format import load_map

MAP = Path(__file__).resolve().parent.parent / "colony" / "world" / "maps" / "aftershock.json"

_worker_passable = Worker._passable
_worker_work_done = Worker._work_is_done
_scout_landing = Scout._landing


def blind_worker_passable(self, world, point):
    """Terrain as this robot knows it, not as the simulator knows it."""
    if point not in world.visible_to(self.robot_id):
        return True  # never seen it; assume walkable and find out by walking
    return world.passable(point[0], point[1], flying=False)


def blind_scout_landing(self, world, here, direction):
    from sim.protocol import DIRECTIONS
    from sim.world import ROLES

    dx, dy = DIRECTIONS[direction]
    x, y = here
    seen = world.visible_to(self.robot_id)
    for _ in range(ROLES[world.robots[self.robot_id].role]["speed"]):
        nx, ny = x + dx, y + dy
        known = (nx, ny) in seen
        if known and not world.passable(nx, ny, flying=True):
            break
        if world.occupied(nx, ny, ignore=self.robot_id):
            break
        x, y = nx, ny
    return (x, y)


def blind_work_is_done(self, world, target):
    """Has the fleet *recorded* this victim as handled, rather than has it happened.

    Only the victim branch is blinded. The `clear_debris` branch still reads
    `world.objects`, deliberately: agents write exactly two observation kinds,
    `hazard` and `victim` (grep of `report_observation` call sites in
    `agents/`), so **there is no debris belief in the store to read**. A first
    attempt blinded it against a `kind="debris"` query that always returns
    empty, which made `_work_is_done` true for every clear the instant it was
    claimed — the fleet "finished" every clear without touching the rubble and
    scored 0.111. That measured the stand-in, not the design, and is retracted.

    That gap is the finding: B-2 cannot be fixed by reading beliefs until the
    fleet writes debris observations at all.
    """
    if self.task.kind == "clear_debris":
        return _worker_work_done(self, world, target)
    return any(
        b.pos == target and (b.payload or {}).get("state") in ("stabilized", "lost")
        for b in self.mem.get_beliefs(self.mission_id, kind="victim")
    )


def restore():
    Worker._passable = _worker_passable
    Worker._work_is_done = _worker_work_done
    Scout._landing = _scout_landing


def run_arm(arm: str, seed: int) -> float:
    restore()
    if arm in ("D", "E"):
        Worker._passable = blind_worker_passable
        Scout._landing = blind_scout_landing
    if arm == "E":
        Worker._work_is_done = blind_work_is_done
    try:
        FakeFleetMem.reset_ids()
        run = run_mission(
            load_map(MAP), FakeFleetMem(), coordinated=True, seed=seed,
            embedder=BedrockAdapter(mode=REPLAY),
        )
        return run.metrics.rescue_rate
    finally:
        restore()


def ci95(values):
    if len(values) < 2:
        return (values[0] if values else 0.0), 0.0
    mean = statistics.fmean(values)
    return mean, 1.96 * statistics.stdev(values) / (len(values) ** 0.5)


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    arms = {"A_shipped": [], "D_blind_pathing": [], "E_blind_pathing_and_checks": []}
    for seed in range(n):
        row = {}
        for key, arm in (("A_shipped", "A"), ("D_blind_pathing", "D"),
                         ("E_blind_pathing_and_checks", "E")):
            v = run_arm(arm, seed)
            arms[key].append(v)
            row[arm] = v
        print(f"seed {seed:2d}  A={row['A']:.3f}  D={row['D']:.3f}  E={row['E']:.3f}",
              flush=True)

    print(f"\n=== arm D: rescue rate, mean +/- 95pct CI, n={n} ===")
    out = {}
    for key, vals in arms.items():
        mean, half = ci95(vals)
        out[key] = {"mean": mean, "ci95": half, "lo": mean - half, "hi": mean + half}
        print(f"{key:28s} {mean:.3f} +/- {half:.3f}   [{mean-half:.3f}, {mean+half:.3f}]")

    def overlap(p, q):
        return not (out[p]["lo"] > out[q]["hi"] or out[q]["lo"] > out[p]["hi"])

    print("\nCI overlap A vs D:", overlap("A_shipped", "D_blind_pathing"))
    print("CI overlap A vs E:", overlap("A_shipped", "E_blind_pathing_and_checks"))
    print()
    if not overlap("A_shipped", "D_blind_pathing"):
        print("VERDICT: omniscient pathing is load-bearing. Removing it (T-12) is a")
        print("         capability regression, not just a cleanup. Say so before doing it.")
    else:
        print("VERDICT: omniscient pathing is NOT load-bearing. T-12 is a free cleanup —")
        print("         the fleet performs the same reading only what it has seen.")

    (Path(__file__).parent / "armd-raw.json").write_text(
        json.dumps({"summary": out, "rescue_rate": arms}, indent=2)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
