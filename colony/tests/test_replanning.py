"""Reacting to a world that changed underneath the plan (FR-7, FR-17, §3.6).

The aftershock is the demo's turning point, and until now it was something that
happened *to* the fleet rather than something the fleet responded to: robots
kept walking towards corridors that had just been re-blocked, and scouts that
had finished sweeping never looked again, so the victim the aftershock reveals
was found by luck or not at all.

Three things are asserted here, and they are the three a judge can see:
holding work through an aftershock, patrolling ground that has gone stale, and
every decision leaving a provenance trail behind it.
"""

from agents.scout import Scout, seed_sector_tasks
from agents.worker import Worker
from sim.world import World
from world.map_format import EMPTY, parse_map


def _sectors(width, height, parts=2):
    """A simple sector grid, so seeded `explore_sector` tasks exist to claim."""
    w, h = width // parts, height // parts
    return [
        {"id": f"S{i}{j}", "x": i * w, "y": j * h, "width": w, "height": h}
        for i in range(parts)
        for j in range(parts)
    ]


def _world(width=20, height=20, spawn=None, victims=(), escalations=(), sectors=None):
    data = {
        "width": width,
        "height": height,
        "tile_size": 32,
        "layers": {
            "ground": [["open"] * width for _ in range(height)],
            "objects": [[EMPTY] * width for _ in range(height)],
        },
        "zones": [],
        "spawn_points": spawn or {},
        "victims": list(victims),
        "escalations": list(escalations),
    }
    if sectors is not None:
        data["sectors"] = sectors
    return World(parse_map(data), seed=0)


def _run(world, agents, ticks):
    for _ in range(ticks):
        world.step({a.robot_id: a.step(world) for a in agents})


AFTERSHOCK_AT = 3


def _aftershock(tick=AFTERSHOCK_AT, **extra):
    return [{"tick": tick, "kind": "aftershock", **extra}]


# --- FR-7: an aftershock invalidates plans made before it --------------------


def test_an_aftershock_puts_held_work_back_in_the_pool(mem, mission):
    """FR-7: "the aftershock invalidates affected path/claim state (release =
    status→open, lease cleared) and triggers replans".

    The robot is not told what changed — it re-decides against the world it can
    now observe. Holding on is the risky option: the route this task depended on
    may not exist any more, and nobody else can tell from the outside.
    """
    world = _world(
        spawn={"medic": [{"x": 5, "y": 5}]},
        victims=[{"id": "v1", "x": 15, "y": 15, "vitals_deadline": 700}],
        escalations=_aftershock(),
    )
    mem.register_victim(mission, (15, 15), reported_by="s1")
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    _run(world, [medic], AFTERSHOCK_AT)
    assert medic.task is not None, "nothing was held when the aftershock fired"

    medic.step(world)  # the tick that feels it

    released = [
        e
        for e in mem.events(mission)
        if e["verb"] == "task_released" and e["detail"].get("reason") == "aftershock"
    ]
    assert released, "held work survived an aftershock unquestioned"
    # Re-taking the same task a moment later is a perfectly good replan — the
    # test is that the decision was made again, not that the robot changed its
    # mind. What must never happen is the claim simply riding through.


def test_the_replan_is_logged_with_its_trigger(mem, mission):
    """FR-17 with the trigger vocabulary actually used. `aftershock` existed in
    the schema from day one and nothing had ever written one, so the commander
    console could not distinguish "chose this" from "was forced off that"."""
    world = _world(
        spawn={"medic": [{"x": 5, "y": 5}]},
        victims=[{"id": "v1", "x": 15, "y": 15, "vitals_deadline": 700}],
        escalations=_aftershock(),
    )
    mem.register_victim(mission, (15, 15), reported_by="s1")
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    _run(world, [medic], AFTERSHOCK_AT + 1)

    triggers = [p.trigger for p in mem.plans_for(mission, "m1")]
    assert "aftershock" in triggers


def test_a_robot_takes_work_again_after_the_shock(mem, mission):
    """Releasing is only half of a replan. A fleet that dropped everything and
    stayed dropped would be worse than one that never noticed."""
    world = _world(
        spawn={"medic": [{"x": 5, "y": 5}]},
        victims=[{"id": "v1", "x": 8, "y": 5, "vitals_deadline": 700}],
        escalations=_aftershock(),
    )
    mem.register_victim(mission, (8, 5), reported_by="s1")
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    _run(world, [medic], 30)

    assert world.victims["v1"].state == "stabilized"


# --- FR-7 and §4.7: exploration does not "finish" ----------------------------


def test_a_scout_goes_back_over_ground_that_has_gone_stale(mem, mission):
    """The demo-map failure, as a fixture.

    Once every sector was swept the scouts idled for the rest of the mission —
    measured on Aftershock: the victim the tick-300 aftershock reveals was never
    found and died at its deadline, with two fully charged scouts parked. An
    explored tile is a tile somebody looked at once; a disaster site does not
    hold still for that.
    """
    world = _world(
        width=12,
        height=12,
        spawn={"scout": [{"x": 1, "y": 1}]},
        escalations=_aftershock(
            tick=40,
            reveal_victims=[{"id": "v9", "x": 10, "y": 10, "vitals_deadline": 700}],
        ),
    )
    scout = Scout(robot_id="s1", mission_id=mission, mem=mem)

    _run(world, [scout], 120)

    assert world.victims["v9"].state != "unknown", (
        "the revealed victim was never found — the scout stopped looking"
    )


def test_an_aftershock_makes_a_scout_re_sweep(mem, mission):
    """Not merely "keep patrolling": what a scout believes it has seen is now
    out of date everywhere, so the sweep starts again rather than resuming."""
    world = _world(
        width=12,
        height=12,
        spawn={"scout": [{"x": 5, "y": 5}]},
        escalations=_aftershock(tick=6),
    )
    scout = Scout(robot_id="s1", mission_id=mission, mem=mem)

    _run(world, [scout], 5)
    seen_before = len(scout.explored)
    assert seen_before > 0

    _run(world, [scout], 2)  # crosses the escalation

    assert len(scout.explored) < seen_before, "the scout kept its stale map"
    assert "aftershock" in [p.trigger for p in mem.plans_for(mission, "s1")]


# --- FR-17 and §3.6: what the judge sees -------------------------------------


def test_every_scout_decision_leaves_a_trail(mem, mission):
    """Scouts logged no plans at all before this: clicking one in the demo
    opened an empty panel, on the agent that does most of the deciding."""
    world = _world(
        width=12,
        height=12,
        spawn={"scout": [{"x": 1, "y": 1}]},
        sectors=_sectors(12, 12),
    )
    seed_sector_tasks(mem, mission, world.map)
    scout = Scout(robot_id="s1", mission_id=mission, mem=mem)

    _run(world, [scout], 5)

    plans = mem.plans_for(mission, "s1")
    assert plans, "the scout decided which sector to sweep and told nobody why"
    assert plans[0].chosen["action"] == "explore"
    assert plans[0].rationale


def test_a_decision_carries_the_beliefs_behind_it(mem, mission):
    """FR-17's `based_on`. The console joins these ids straight back to
    `observations` — an empty list is a decision with no traceable cause."""
    world = _world(
        spawn={"medic": [{"x": 5, "y": 5}]},
        victims=[{"id": "v1", "x": 7, "y": 5, "vitals_deadline": 700}],
    )
    mem.report_observation(mission, "s1", "victim", (7, 5))
    mem.register_victim(mission, (7, 5), reported_by="s1")
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    medic.step(world)

    plan = mem.plans_for(mission, "m1")[0]
    assert plan.based_on, "the medic cited nothing for going there"


def test_a_working_robot_says_what_it_is_doing(mem, mission):
    """§3.6: the bubble is how a viewer sees a robot thinking. It rides in the
    state frame the renderer already receives, so this costs no new plumbing."""
    world = _world(
        spawn={"medic": [{"x": 5, "y": 5}]},
        victims=[{"id": "v1", "x": 7, "y": 5, "vitals_deadline": 700}],
    )
    mem.register_victim(mission, (7, 5), reported_by="s1")
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    medic.step(world)

    assert world.robots["m1"].bubble, "the medic worked in silence"
    assert world.robots["m1"].to_json()["bubble"]


def test_a_scout_says_which_sector_it_is_sweeping(mem, mission):
    world = _world(
        width=12,
        height=12,
        spawn={"scout": [{"x": 1, "y": 1}]},
        sectors=_sectors(12, 12),
    )
    seed_sector_tasks(mem, mission, world.map)
    scout = Scout(robot_id="s1", mission_id=mission, mem=mem)

    scout.step(world)

    assert "sector" in world.robots["s1"].bubble
