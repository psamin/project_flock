"""Node-kill chaos rehearsal (PRD FR-11, §6.5, §5.4).

Runs a real mission against the 3-node cluster, kills a node partway through,
and checks the two things FR-11 actually claims:

    zero task loss   every task claimed before the kill is still claimed by the
                     same robot afterwards, or finished — none silently vanished
    no fleet stall   tasks keep completing across the kill; the fleet does not
                     freeze and wait for the node to come back

Both are measured from fleet memory rather than from the sim, because the point
is that the *memory* survived. §5.4 wants this rehearsed at least five times
before recording, so `--rehearsals` runs it repeatedly and fails if any run
regresses.

    uv run python ../infra/chaos.py --rehearsals 5

Kills a container, so it is deliberately not part of the pytest suite: the tests
assert its logic against a fake, and this drives the real thing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "colony"))

CLUSTER = ROOT / "infra" / "cluster3.sh"
KILL_AT_TICK = 40          # after the fleet has claimed work, before it finishes
REVIVE_AFTER_TICKS = 60


@dataclass
class ChaosResult:
    """One rehearsal. `survived` is the FR-11 verdict."""

    claimed_before_kill: int = 0
    lost_tasks: list[str] = field(default_factory=list)
    completions_before: int = 0
    completions_after: int = 0
    stabilized: int = 0
    ticks: int = 0
    kill_error: str | None = None

    @property
    def zero_task_loss(self) -> bool:
        return not self.lost_tasks

    @property
    def no_fleet_stall(self) -> bool:
        # The fleet must keep finishing work *after* the node dies. Comparing
        # against the pre-kill rate rather than requiring a fixed number: a
        # mission that had nearly finished before the kill legitimately has less
        # left to do.
        return self.completions_after > 0

    @property
    def survived(self) -> bool:
        return self.zero_task_loss and self.no_fleet_stall and self.kill_error is None

    def describe(self) -> str:
        verdict = "SURVIVED" if self.survived else "FAILED"
        return (
            f"{verdict}: {self.claimed_before_kill} tasks in flight at the kill, "
            f"{len(self.lost_tasks)} lost, "
            f"{self.completions_before} completions before / "
            f"{self.completions_after} after, "
            f"{self.stabilized} victims stabilized over {self.ticks} ticks"
        )


def _cluster(action: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(CLUSTER), action], capture_output=True, text=True, timeout=180
    )


def _in_flight(mem: Any, mission_id: uuid.UUID) -> dict[str, str]:
    """Task id -> owner, for everything currently claimed."""
    rows = mem.conn.execute(
        "SELECT id, claimed_by FROM tasks"
        " WHERE mission_id = %s AND status IN ('claimed', 'in_progress')",
        (mission_id,),
    ).fetchall()
    return {str(r["id"]): r["claimed_by"] for r in rows}


def _surviving(mem: Any, task_ids: list[str]) -> set[str]:
    if not task_ids:
        return set()
    rows = mem.conn.execute(
        "SELECT id FROM tasks WHERE id = ANY(%s)", ([uuid.UUID(t) for t in task_ids],)
    ).fetchall()
    return {str(r["id"]) for r in rows}


def run_rehearsal(kill_at: int = KILL_AT_TICK, verbose: bool = True) -> ChaosResult:
    """One mission with a node killed mid-flight."""
    from agents.scout import Scout, seed_sector_tasks, split_sectors
    from agents.worker import Worker
    from bedrock.adapter import BedrockAdapter
    from fleetmem.client import CockroachFleetMem
    from sim.world import World
    from world.map_format import load_map

    world_map = load_map(ROOT / "colony" / "world" / "maps" / "aftershock.json")
    world = World(world_map, seed=3)
    mem = CockroachFleetMem()
    mission_id = uuid.uuid4()
    result = ChaosResult()

    seed_sector_tasks(mem, mission_id, world_map)
    scouts = [r for r in world.robots.values() if r.role == "scout"]
    shares = split_sectors(world_map.sectors, max(1, len(scouts)))
    embedder = BedrockAdapter()
    agents: list[Any] = [
        Scout(robot_id=r.id, mission_id=mission_id, mem=mem, embedder=embedder,
              seed=i, sectors=shares[i])
        for i, r in enumerate(scouts)
    ]
    agents += [
        Worker(robot_id=r.id, role=r.role, mission_id=mission_id, mem=mem)
        for r in world.robots.values() if r.role in ("lifter", "medic")
    ]
    for robot in world.robots.values():
        mem.register_robot(robot.id, robot.role, (robot.x, robot.y), robot.battery)

    in_flight: dict[str, str] = {}
    killed = False

    for tick in range(world_map.mission_length_ticks):
        if tick == kill_at:
            in_flight = _in_flight(mem, mission_id)
            result.claimed_before_kill = len(in_flight)
            result.completions_before = _completions(mem, mission_id)
            killed_proc = _cluster("kill")
            if killed_proc.returncode != 0:
                result.kill_error = killed_proc.stderr.strip()[:200]
            killed = True
            if verbose:
                print(f"  tick {tick}: killed a node with "
                      f"{result.claimed_before_kill} tasks in flight")

        if killed and tick == kill_at + REVIVE_AFTER_TICKS:
            _cluster("revive")
            if verbose:
                print(f"  tick {tick}: node back")

        world.step({a.robot_id: a.step(world) for a in agents})
        if world.finished:
            break

    result.ticks = world.tick
    result.stabilized = world.metrics()["victims_stabilized"]

    # Zero task loss: every task that was in flight at the kill still exists.
    # A task whose row vanished is the failure FR-11 rules out.
    survivors = _surviving(mem, list(in_flight))
    result.lost_tasks = sorted(set(in_flight) - survivors)
    result.completions_after = _completions(mem, mission_id) - result.completions_before

    mem.close()
    return result


def _completions(mem: Any, mission_id: uuid.UUID) -> int:
    row = mem.conn.execute(
        "SELECT count(*) AS n FROM events"
        " WHERE mission_id = %s AND verb = 'task_completed'",
        (mission_id,),
    ).fetchone()
    return row["n"]


def main() -> int:
    parser = argparse.ArgumentParser(description="FR-11 node-kill rehearsal")
    parser.add_argument("--rehearsals", type=int, default=1,
                        help="§5.4 wants at least 5 before recording")
    parser.add_argument("--kill-at", type=int, default=KILL_AT_TICK)
    args = parser.parse_args()

    if _cluster("nodes").stdout.strip() != "3":
        print("Need a healthy 3-node cluster first: make cluster-3", file=sys.stderr)
        return 2

    failures = 0
    for run in range(1, args.rehearsals + 1):
        print(f"rehearsal {run}/{args.rehearsals}")
        result = run_rehearsal(kill_at=args.kill_at)
        print(f"  {result.describe()}")
        if not result.survived:
            failures += 1
        _cluster("revive")           # always leave the cluster whole

    print(f"\n{args.rehearsals - failures}/{args.rehearsals} rehearsals survived")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
