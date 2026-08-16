"""Running a mission, in either coordination mode (§3.3, §4.7).

One entry point for "run the fleet on this map" so the coordinated and baseline
runs cannot drift apart by accident. Everything that differs between them is
named here, in one place:

    coordinated   shared beliefs, transactional claiming, sector claims
    baseline      private world models, greedy self-selection, no sector claims

Nothing else changes — same map, same seed, same robots, same behaviours — so
the delta between the two runs is attributable to the coordination layer rather
than to a pile of mode flags scattered through the agents.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agents.planning import Planner
from agents.scout import Scout, seed_sector_tasks, split_sectors
from agents.worker import Worker
from bedrock.adapter import BedrockAdapter
from sim import metrics as metrics_mod
from sim.metrics import COVERAGE_AT_TICK, Comparison, Metrics
from sim.world import World
from world.map_format import WorldMap


@dataclass
class MissionRun:
    world: World
    mem: Any
    mission_id: UUID
    metrics: Metrics


def build_fleet(
    world: World,
    mem: Any,
    mission_id: UUID,
    *,
    coordinated: bool = True,
    seed: int | None = None,
    embedder: Any = None,
    planner: Any = None,
) -> dict[str, Any]:
    """Register the robots, seed the sector tasks, and return agents by robot id.

    One definition of what a fleet *is*, because there are two callers — the
    batch runner below and the live server — and they had drifted: the server
    seeded sector tasks unconditionally and hard-coded coordinated behaviour, so
    the ON/OFF toggle it now offers would have switched the fog of war without
    switching the fleet underneath it. A demo that claims to compare two modes
    has to actually run two.
    """
    embedder = embedder or BedrockAdapter()
    # One planner for the mission: the §3.5 rate cap is per robot, but the
    # thread pool and the cassette are not. Both modes get one, so a difference
    # between the runs is never "the baseline had no planner".
    planner = planner or Planner(adapter=embedder)

    if coordinated:
        # Baseline explores on private frontier bias only (§3.3), so it gets no
        # sector tasks to claim.
        seed_sector_tasks(mem, mission_id, world.map)

    scouts = [r for r in world.robots.values() if r.role == "scout"]
    shares = split_sectors(world.map.sectors, max(1, len(scouts)))
    agents: dict[str, Any] = {
        robot.id: Scout(
            robot_id=robot.id,
            mission_id=mission_id,
            mem=mem,
            embedder=embedder,
            seed=(seed or 0) + i,
            sectors=shares[i] if coordinated else (),
            planner=planner,
        )
        for i, robot in enumerate(scouts)
    }
    agents.update(
        {
            robot.id: Worker(
                robot_id=robot.id,
                role=robot.role,
                mission_id=mission_id,
                mem=mem,
                coordinated=coordinated,
                planner=planner,
            )
            for robot in world.robots.values()
            if robot.role in ("lifter", "medic")
        }
    )
    for robot in world.robots.values():
        mem.register_robot(robot.id, robot.role, (robot.x, robot.y), robot.battery)
    return agents


def run_mission(
    world_map: WorldMap,
    mem: Any,
    *,
    coordinated: bool = True,
    seed: int | None = None,
    max_ticks: int | None = None,
    embedder: Any = None,
    planner: Any = None,
) -> MissionRun:
    """Run one mission to completion and return it with its §4.7 metrics."""
    world = World(world_map, seed=seed)
    world.shared_vision = coordinated
    mission_id = uuid.uuid4()
    agents = list(
        build_fleet(
            world,
            mem,
            mission_id,
            coordinated=coordinated,
            seed=seed,
            embedder=embedder,
            planner=planner,
        ).values()
    )

    limit = max_ticks or world_map.mission_length_ticks
    coverage_at_500 = 0.0
    for _ in range(limit):
        frame = world.step({a.robot_id: a.step(world) for a in agents})
        mem.log_events(
            mission_id, [(e["actor"], e["verb"], e["detail"]) for e in frame.events]
        )
        if world.tick == COVERAGE_AT_TICK:
            coverage_at_500 = world.coverage()
        if world.finished:
            break

    # A mission that ends before tick 500 has finished exploring as far as it is
    # going to, so its final coverage *is* Coverage@500.
    if world.tick < COVERAGE_AT_TICK:
        coverage_at_500 = world.coverage()

    return MissionRun(
        world=world,
        mem=mem,
        mission_id=mission_id,
        metrics=metrics_mod.compute(
            mem.events(mission_id),
            victims_total=len(world.victims),
            coverage_at_500=coverage_at_500,
            ticks=world.tick,
            horizon=world_map.mission_length_ticks,
            # From belief rows, not simulator state: how many victims the fleet
            # itself knows about. In baseline this is the number that stays low.
            victims_located=len(mem.get_beliefs(mission_id, kind="victim")),
        ),
    )


def compare_modes(
    world_map: WorldMap,
    make_memory,
    *,
    seed: int | None = None,
    max_ticks: int | None = None,
) -> Comparison:
    """Run the same mission with coordination on and off (§4.7).

    `make_memory` is a factory rather than an instance: the two runs must not
    share fleet memory, or the baseline inherits the coordinated run's beliefs
    and stops being a baseline.
    """
    coordinated = run_mission(
        world_map, make_memory(), coordinated=True, seed=seed, max_ticks=max_ticks
    )
    baseline = run_mission(
        world_map, make_memory(), coordinated=False, seed=seed, max_ticks=max_ticks
    )
    return Comparison(coordinated=coordinated.metrics, baseline=baseline.metrics)
