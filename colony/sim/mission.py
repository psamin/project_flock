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
from sim import recall as recall_mod
from sim.metrics import COVERAGE_AT_TICK, Comparison, Metrics
from sim.world import World
from world.map_format import WorldMap


@dataclass
class MissionRun:
    world: World
    mem: Any
    mission_id: UUID
    metrics: Metrics


def _log_recall(mem, mission_id, world_map, memories, hot) -> None:
    """Record that memory shaped this mission — as an event and as a plan.

    The plan row is why this is a decision and not a side effect: FR-17 says
    every choice keeps its sources, and "we swept B2 first because two earlier
    missions found victims there" is exactly such a choice.

    The source id goes in `chosen` rather than in `based_on`. `based_on` is a
    UUID[] resolved against `observations` — by Mission.provenance() in Python
    and by the console's WHY_DID_ROBOT join in SQL — so a mission_memories id
    put there is silently dropped in two places and the panel would claim more
    sources than it lists. `chosen` is already the untyped bag carrying
    `source`, it round-trips whole, and it needs no schema change.
    """
    top = memories[0]
    detail = {
        "map": recall_mod.map_key(world_map),
        "memories": len(memories),
        "distance": round(top.distance, 3),
        "sectors": hot,
        "memory_id": str(top.id),
    }
    mem.log_event(mission_id, "fleet", "memory_recalled", detail)
    mem.log_plan(
        mission_id,
        "fleet",
        trigger="idle",
        chosen={"action": "seed_from_memory", "source": "semantic_memory", **detail},
        rationale=(
            f"{len(memories)} earlier mission(s) on {detail['map']} found victims in "
            f"{', '.join(hot) or 'no recorded sector'}; sweeping those first"
        ),
        based_on=(),
    )


def build_fleet(
    world: World,
    mem: Any,
    mission_id: UUID,
    *,
    coordinated: bool = True,
    seed: int | None = None,
    embedder: Any = None,
    planner: Any = None,
    recall_enabled: bool = False,
    recalled: list | None = None,
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
        # Semantic memory enters the mission here and nowhere else. Recall is
        # read *before* the sector tasks exist so what earlier missions learned
        # can be baked into their priorities rather than bumped afterwards.
        #
        # Coordinated only, for the same reason the sector tasks are: a baseline
        # run is a control condition, and letting it read memories a coordinated
        # run wrote would leak across the very comparison compare_modes takes a
        # factory to protect. In baseline there are no sector tasks at all, so
        # the priority ordering is unreachable by construction, not by flag.
        hot: list[str] = []
        if recall_enabled:
            # Never allowed to stop a mission starting. Recall costs one Bedrock
            # embed and one indexed read, and both are on the path between
            # pressing "restart" and the first tick — so a throttled model or a
            # network blip would otherwise mean no mission at all, trading a
            # fleet that rescues nobody for a fleet that starts slightly worse
            # informed. A mission that has forgotten still works; that is the
            # whole point of the rules being the floor.
            try:
                memories = recall_mod.recall(mem, embedder, world.map)
                hot = recall_mod.hot_sectors(memories)
                if recalled is not None:
                    recalled.extend(memories)
                if memories:
                    _log_recall(mem, mission_id, world.map, memories, hot)
            except Exception as exc:  # noqa: BLE001 - a demo must not die here
                print(f"[sim] could not recall earlier missions: {exc!r}")
        seed_sector_tasks(mem, mission_id, world.map, hot_sectors=hot)

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
    remember: bool = False,
    recall_enabled: bool = False,
) -> MissionRun:
    """Run one mission to completion and return it with its §4.7 metrics.

    `remember` and `recall_enabled` default off. Semantic memory calls
    `embed()`, which bumps the adapter's live call counter and — more to the
    point — makes a run's outcome depend on what earlier runs in the same
    process left behind. Both are things the metrics, Bedrock and determinism
    suites need to opt into rather than inherit.
    """
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
            recall_enabled=recall_enabled,
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

    metrics = metrics_mod.compute(
        mem.events(mission_id),
        victims_total=len(world.victims),
        coverage_at_500=coverage_at_500,
        ticks=world.tick,
        horizon=world_map.mission_length_ticks,
        # From belief rows, not simulator state: how many victims the fleet
        # itself knows about. In baseline this is the number that stays low.
        victims_located=len(mem.get_beliefs(mission_id, kind="victim")),
    )

    # Coordinated runs only: a baseline is a control, and a memory it wrote
    # would be read by a later coordinated run — contaminating the comparison
    # from the other direction to the read guard in build_fleet.
    if remember and coordinated:
        recall_mod.write_memory(
            mem, embedder or BedrockAdapter(), mission_id, world_map, metrics.to_json()
        )

    return MissionRun(world=world, mem=mem, mission_id=mission_id, metrics=metrics)


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
