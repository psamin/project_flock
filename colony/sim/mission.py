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


def run_mission(
    world_map: WorldMap,
    mem: Any,
    *,
    coordinated: bool = True,
    seed: int | None = None,
    max_ticks: int | None = None,
    embedder: Any = None,
) -> MissionRun:
    """Run one mission to completion and return it with its §4.7 metrics."""
    world = World(world_map, seed=seed)
    world.shared_vision = coordinated
    mission_id = uuid.uuid4()
    embedder = embedder or BedrockAdapter()

    if coordinated:
        # Baseline explores on private frontier bias only (§3.3), so it gets no
        # sector tasks to claim.
        seed_sector_tasks(mem, mission_id, world_map)

    scouts = [r for r in world.robots.values() if r.role == "scout"]
    shares = split_sectors(world_map.sectors, max(1, len(scouts)))
    agents: list[Any] = [
        Scout(robot_id=robot.id, mission_id=mission_id, mem=mem, embedder=embedder,
              seed=(seed or 0) + i, sectors=shares[i] if coordinated else ())
        for i, robot in enumerate(scouts)
    ]
    agents += [
        Worker(robot_id=robot.id, role=robot.role, mission_id=mission_id, mem=mem,
               coordinated=coordinated)
        for robot in world.robots.values() if robot.role in ("lifter", "medic")
    ]
    for robot in world.robots.values():
        mem.register_robot(robot.id, robot.role, (robot.x, robot.y), robot.battery)

    limit = max_ticks or world_map.mission_length_ticks
    coverage_at_500 = 0.0
    for _ in range(limit):
        frame = world.step({a.robot_id: a.step(world) for a in agents})
        for event in frame.events:
            mem.log_event(mission_id, event["actor"], event["verb"], event["detail"])
        if world.tick == COVERAGE_AT_TICK:
            coverage_at_500 = world.coverage()
        if world.finished:
            break

    # A mission that ends before tick 500 has finished exploring as far as it is
    # going to, so its final coverage *is* Coverage@500.
    if world.tick < COVERAGE_AT_TICK:
        coverage_at_500 = world.coverage()

    return MissionRun(
        world=world, mem=mem, mission_id=mission_id,
        metrics=metrics_mod.compute(
            mem.events(mission_id),
            victims_total=len(world.victims),
            coverage_at_500=coverage_at_500,
            ticks=world.tick,
            horizon=world_map.mission_length_ticks,
        ),
    )


def compare_modes(world_map: WorldMap, make_memory, *, seed: int | None = None,
                  max_ticks: int | None = None) -> Comparison:
    """Run the same mission with coordination on and off (§4.7).

    `make_memory` is a factory rather than an instance: the two runs must not
    share fleet memory, or the baseline inherits the coordinated run's beliefs
    and stops being a baseline.
    """
    coordinated = run_mission(world_map, make_memory(), coordinated=True,
                              seed=seed, max_ticks=max_ticks)
    baseline = run_mission(world_map, make_memory(), coordinated=False,
                           seed=seed, max_ticks=max_ticks)
    return Comparison(coordinated=coordinated.metrics, baseline=baseline.metrics)
