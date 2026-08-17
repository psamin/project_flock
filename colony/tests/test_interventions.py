"""Operator interventions — breaking the world on purpose (issue #22).

Four things are worth testing here and they fail in different ways:

    validation      a bad request is refused before anything mutates, because a
                    half-applied intervention leaves a world no seed reproduces
    no stranding    fire that would seal off a victim is refused outright; the
                    operator is allowed to make the mission harder and not to
                    make it unwinnable
    transport       the row reaches the mission, and reaches it once, whether it
                    travels by changefeed or by poll
    the fleet feels it  a disruption releases held work and produces a
                    `world_changed` decision, which is the whole point

The API tests use the fake, so none of this needs a cluster. The changefeed's
own behaviour against a live one is covered in `test_changefeed.py`.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from agents.planning import (
    PLAN_CALLS_PER_MINUTE,
    RESERVED_CALLS_PER_MINUTE,
    Planner,
)
from fleetmem.changefeed import _parse_hazard
from fleetmem.fake import FakeFleetMem
from fleetmem.types import INTERVENTION_PREFIX
from sim import interventions as iv
from sim.world import World
from world.map_format import DEBRIS, EMPTY, FIRE, RUBBLE_HEAVY, parse_map


def _map(width=12, height=12, **extra):
    data = {
        "width": width,
        "height": height,
        "tile_size": 32,
        "layers": {
            "ground": [["open"] * width for _ in range(height)],
            "objects": [[EMPTY] * width for _ in range(height)],
        },
        "zones": [],
        "spawn_points": {"lifter": [{"x": 0, "y": 0}]},
        "victims": [],
        "escalations": [],
        "mission_length_ticks": 1200,
    }
    data.update(extra)
    return data


@pytest.fixture
def world():
    return World(parse_map(_map()), seed=1)


# --- the blast shape ---------------------------------------------------------


def test_a_radius_zero_diamond_is_one_tile():
    assert iv.diamond(4, 4, 0) == [(4, 4)]


def test_a_diamond_is_manhattan_not_square():
    tiles = set(iv.diamond(5, 5, 1))
    assert tiles == {(5, 5), (4, 5), (6, 5), (5, 4), (5, 6)}
    assert (4, 4) not in tiles  # the corner a square would have included


# --- validation --------------------------------------------------------------


def test_an_unknown_kind_is_refused(world):
    with pytest.raises(iv.InterventionError) as exc:
        iv.plan(world, "earthquake", 5, 5)
    assert "earthquake" in exc.value.reason
    assert sorted(iv.KINDS) == exc.value.detail["known"]


def test_an_off_map_origin_is_refused(world):
    with pytest.raises(iv.InterventionError):
        iv.plan(world, iv.COLLAPSE, 99, 5)
    with pytest.raises(iv.InterventionError):
        iv.plan(world, iv.COLLAPSE, 5, -1)


def test_an_oversized_radius_is_refused(world):
    with pytest.raises(iv.InterventionError):
        iv.plan(world, iv.COLLAPSE, 5, 5, radius=iv.MAX_RADIUS + 1)


def test_a_negative_radius_is_refused(world):
    with pytest.raises(iv.InterventionError):
        iv.plan(world, iv.COLLAPSE, 5, 5, radius=-1)


def test_an_all_wall_target_is_refused():
    """Writing an object onto a wall changes nothing and would make
    `tiles_changed` claim something moved."""
    data = _map()
    for y in range(12):
        for x in range(12):
            data["layers"]["ground"][y][x] = "wall"
    data["layers"]["ground"][0][0] = "open"  # somewhere for the lifter to stand
    world = World(parse_map(data), seed=1)
    with pytest.raises(iv.InterventionError) as exc:
        iv.plan(world, iv.COLLAPSE, 6, 6, radius=1)
    assert "wall" in exc.value.reason


def test_walls_are_dropped_from_the_tile_list(world):
    world.ground[5][6] = "wall"
    planned = iv.plan(world, iv.COLLAPSE, 5, 5, radius=1)
    assert (6, 5) not in planned.tiles
    assert (5, 5) in planned.tiles


# --- the no-stranding guarantee ---------------------------------------------


def _victim_map(**extra):
    """A victim in a dead-end corridor, reachable only through (5,5)."""
    data = _map(width=12, height=12, **extra)
    for y in range(12):
        for x in range(12):
            data["layers"]["ground"][y][x] = "wall"
    for tile in [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (5, 0)]:
        data["layers"]["ground"][tile[1]][tile[0]] = "open"
    for tile in [(5, 1), (5, 2), (5, 3), (5, 4), (5, 5), (5, 6)]:
        data["layers"]["ground"][tile[1]][tile[0]] = "open"
    data["victims"] = [{"id": "v1", "x": 5, "y": 6, "vitals_deadline": 600}]
    return data


def test_clearable_disruptions_never_strand(world_factory=None):
    """Debris and rubble are work, not walls: a lifter opens them in 3 and 6
    ticks, so a route through them is slow rather than absent."""
    world = World(parse_map(_victim_map()), seed=1)
    for kind in (iv.COLLAPSE, iv.RUBBLE):
        planned = iv.plan(world, kind, 5, 3, radius=0)
        assert iv.would_strand(world, planned) == []


def test_fire_that_seals_the_only_corridor_is_refused():
    world = World(parse_map(_victim_map()), seed=1)
    with pytest.raises(iv.InterventionError) as exc:
        iv.plan(world, iv.BLAZE, 5, 3, radius=0)
    assert exc.value.detail["stranded"] == ["v1"]
    assert "v1" in exc.value.reason


def test_fire_elsewhere_is_allowed():
    world = World(parse_map(_victim_map()), seed=1)
    planned = iv.plan(world, iv.BLAZE, 0, 0, radius=0)
    assert planned.kind == iv.BLAZE


def test_a_stabilized_victim_cannot_be_stranded():
    """Only victims still needing rescue constrain the operator."""
    world = World(parse_map(_victim_map()), seed=1)
    world.victims["v1"].state = "stabilized"
    assert iv.plan(world, iv.BLAZE, 5, 3, radius=0).kind == iv.BLAZE


def test_reachability_ignores_the_scout_that_cannot_rescue():
    """A victim only a flier can reach is stranded as far as the mission goes."""
    data = _victim_map()
    data["spawn_points"] = {"scout": [{"x": 0, "y": 0}], "lifter": [{"x": 0, "y": 0}]}
    world = World(parse_map(data), seed=1)
    with pytest.raises(iv.InterventionError):
        iv.plan(world, iv.BLAZE, 5, 3, radius=0)


# --- applying it -------------------------------------------------------------


def test_applying_writes_the_object_and_reports_the_tiles(world):
    planned = iv.plan(world, iv.COLLAPSE, 5, 5, radius=1)
    applied = world.apply_intervention(planned)
    assert world.objects[5][5] == DEBRIS
    assert set(applied.tiles) == set(planned.tiles)


def test_rubble_and_fire_write_their_own_objects(world):
    world.apply_intervention(iv.plan(world, iv.RUBBLE, 2, 2, radius=0))
    world.apply_intervention(iv.plan(world, iv.BLAZE, 8, 8, radius=0))
    assert world.objects[2][2] == RUBBLE_HEAVY
    assert world.objects[8][8] == FIRE


def test_a_tile_already_carrying_the_object_is_not_reported_changed(world):
    world.objects[5][5] = DEBRIS
    applied = world.apply_intervention(iv.plan(world, iv.COLLAPSE, 5, 5, radius=0))
    assert applied.tiles == []


def test_a_tile_under_a_robot_is_skipped(world):
    """Issue #22's first non-goal: an operator disrupts the world, never a
    robot. Dropping fire onto L1 is destroying a robot by another name."""
    world.robots["l1"].x, world.robots["l1"].y = 5, 5
    applied = world.apply_intervention(iv.plan(world, iv.BLAZE, 5, 5, radius=0))
    assert applied.tiles == []
    assert world.objects[5][5] != FIRE


def test_applying_emits_an_event_naming_the_cause(world):
    world.apply_intervention(
        iv.plan(world, iv.COLLAPSE, 5, 5, radius=1, caused_by="commander")
    )
    event = next(e for e in world.events if e["verb"] == "intervention")
    assert event["actor"] == "commander"
    assert event["detail"]["kind"] == iv.COLLAPSE
    assert event["detail"]["tiles"] == 5


def test_tiles_changed_reaches_the_renderer(world):
    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 5, 5, radius=0))
    frame = world.step({})
    assert [5, 5] in [[t["x"], t["y"]] for t in frame.tiles_changed]


# --- the counter agents watch ------------------------------------------------


def test_an_intervention_counts_as_a_disruption(world):
    assert world.disruptions_felt == 0
    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 5, 5, radius=0))
    assert world.disruptions_felt == 1
    assert world.escalations_fired == 0  # it was not the map's doing


def test_a_no_op_intervention_still_counts(world):
    """The operator acted, so the fleet is entitled to re-decide. Gating the
    counter on tiles moving would make a fire dropped on burning ground a
    silent no-op the UI still claimed had happened."""
    world.objects[5][5] = DEBRIS
    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 5, 5, radius=0))
    assert world.disruptions_felt == 1


def test_escalations_and_interventions_both_count():
    data = _map(
        escalations=[
            {"tick": 1, "kind": "aftershock", "block_tiles": [{"x": 3, "y": 3}]}
        ]
    )
    world = World(parse_map(data), seed=1)
    world.step({})
    assert (world.escalations_fired, world.disruptions_felt) == (1, 1)
    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 8, 8, radius=0))
    assert (world.escalations_fired, world.disruptions_felt) == (1, 2)


# --- the row, and rebuilding it ---------------------------------------------


def test_an_intervention_round_trips_through_its_area(world):
    planned = iv.plan(world, iv.RUBBLE, 4, 6, radius=2, caused_by="commander")
    rebuilt = iv.from_area(planned.kind, planned.area() | {"caused_by": "commander"})
    assert rebuilt == planned


def test_the_row_carries_the_intervention_prefix():
    mem = FakeFleetMem()
    mission_id = uuid.uuid4()
    mem.record_intervention(mission_id, iv.BLAZE, {"origin": [1, 1], "tiles": []})
    hazard = mem.active_hazards(mission_id, interventions_only=True)[0]
    assert hazard.kind == f"{INTERVENTION_PREFIX}fire"
    assert hazard.intervention_kind == "fire"
    assert hazard.is_intervention


def test_hazards_are_scoped_to_their_mission():
    mem = FakeFleetMem()
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    mem.record_intervention(theirs, iv.BLAZE, {"origin": [1, 1], "tiles": []})
    assert mem.active_hazards(mine, interventions_only=True) == []


# --- the changefeed parser ---------------------------------------------------


def _feed_row(**over):
    import json

    after = {
        "id": str(uuid.uuid4()),
        "mission_id": str(uuid.uuid4()),
        "kind": f"{INTERVENTION_PREFIX}fire",
        "area": {"origin": [3, 4], "radius": 1, "tiles": [[3, 4]]},
        "severity": 4,
        "active": True,
    }
    after.update(over)
    return {"table": "hazards", "value": json.dumps({"after": after})}


def test_a_resolved_timestamp_row_is_not_a_hazard():
    assert _parse_hazard({"table": None, "value": None}) is None


def test_a_delete_is_not_a_hazard():
    import json

    assert (
        _parse_hazard({"table": "hazards", "value": json.dumps({"after": None})})
        is None
    )


def test_a_jsonb_area_arriving_as_text_is_parsed():
    import json

    row = _feed_row()
    payload = json.loads(row["value"])
    payload["after"]["area"] = json.dumps(payload["after"]["area"])
    row["value"] = json.dumps(payload)
    assert _parse_hazard(row).area["origin"] == [3, 4]


def test_a_non_intervention_hazard_is_not_one():
    change = _parse_hazard(_feed_row(kind="fire"))
    assert not change.is_intervention
    assert change.intervention_kind == ""


# --- the listener ------------------------------------------------------------


def test_the_listener_polls_when_there_is_nothing_to_stream():
    mem = FakeFleetMem()
    watch = iv.InterventionWatch(mem, uuid.uuid4()).start()
    assert watch.transport == "poll"


def test_the_listener_returns_what_was_written():
    mem, mission_id = FakeFleetMem(), uuid.uuid4()
    watch = iv.InterventionWatch(mem, mission_id).start()
    mem.record_intervention(
        mission_id, iv.COLLAPSE, {"origin": [2, 3], "radius": 0, "tiles": [[2, 3]]}
    )
    found = watch.pending()
    assert [i.kind for i in found] == [iv.COLLAPSE]
    assert found[0].origin == (2, 3)


def test_the_listener_delivers_each_intervention_once():
    """Required on both transports and for different reasons: a changefeed
    carries every write to a row, and a poll re-reads by design."""
    mem, mission_id = FakeFleetMem(), uuid.uuid4()
    watch = iv.InterventionWatch(mem, mission_id).start()
    mem.record_intervention(
        mission_id, iv.COLLAPSE, {"origin": [2, 3], "radius": 0, "tiles": [[2, 3]]}
    )
    assert len(watch.pending()) == 1
    assert watch.pending() == []


def test_a_malformed_row_costs_one_intervention_not_the_mission():
    mem, mission_id = FakeFleetMem(), uuid.uuid4()
    watch = iv.InterventionWatch(mem, mission_id).start()
    mem.record_intervention(mission_id, iv.COLLAPSE, {"radius": 0})  # no origin
    assert watch.pending() == []
    mem.record_intervention(
        mission_id, iv.BLAZE, {"origin": [1, 1], "radius": 0, "tiles": [[1, 1]]}
    )
    assert [i.kind for i in watch.pending()] == [iv.BLAZE]


def test_the_listener_ignores_hazards_that_are_not_interventions():
    mem, mission_id = FakeFleetMem(), uuid.uuid4()
    watch = iv.InterventionWatch(mem, mission_id).start()
    mem._hazards[uuid.uuid4()] = {
        "id": uuid.uuid4(),
        "mission_id": mission_id,
        "kind": "fire",  # a plain map hazard, no prefix
        "area": {},
        "severity": 1,
        "active": True,
    }
    assert watch.pending() == []


# --- the reserved planning budget -------------------------------------------


class _Robot:
    id = "l1"
    role = "lifter"
    kits = 0


class _Digest:
    text = "a belief"
    ids: list = []


def _planner():
    """A planner in replay with no cassette: `plan()` declines, which is enough
    to exercise the budget without touching AWS."""
    return Planner()


def test_reserved_calls_draw_on_their_own_budget():
    planner, robot = _planner(), _Robot()
    for tick in range(PLAN_CALLS_PER_MINUTE):
        planner._record_call(robot.id, tick)
    assert not planner._within_cap(robot.id, PLAN_CALLS_PER_MINUTE)
    # Ordinary budget spent; the disruption budget is untouched.
    assert planner._within_cap(robot.id, PLAN_CALLS_PER_MINUTE, reserved=True)


def test_the_reserved_budget_is_itself_bounded():
    """An operator holding the button down must not mint unbounded calls."""
    planner, robot = _planner(), _Robot()
    for tick in range(RESERVED_CALLS_PER_MINUTE):
        planner._record_call(robot.id, tick, reserved=True)
    assert not planner._within_cap(robot.id, RESERVED_CALLS_PER_MINUTE, reserved=True)


def test_spending_the_reserve_leaves_ordinary_planning_alone():
    planner, robot = _planner(), _Robot()
    for tick in range(RESERVED_CALLS_PER_MINUTE):
        planner._record_call(robot.id, tick, reserved=True)
    assert planner._within_cap(robot.id, RESERVED_CALLS_PER_MINUTE)


def test_both_budgets_age_out_after_a_minute():
    from agents.planning import TICKS_PER_MINUTE

    planner, robot = _planner(), _Robot()
    for tick in range(RESERVED_CALLS_PER_MINUTE):
        planner._record_call(robot.id, tick, reserved=True)
    assert planner._within_cap(robot.id, TICKS_PER_MINUTE + 1, reserved=True)


# --- the API seam ------------------------------------------------------------


def _fresh_server(monkeypatch):
    """A reloaded server module on the fake, so each test gets its own mission.

    Deliberately not entered as a context manager, for the reason spelled out in
    `test_console_api.py`: the lifespan starts the 4 Hz tick loop on the client's
    thread, and a test that also calls `tick_once()` is two threads driving one
    mission.
    """
    import importlib

    monkeypatch.setenv("COLONY_MEMORY", "fake")
    from sim import server as server_module

    importlib.reload(server_module)
    return TestClient(server_module.app), server_module


@pytest.fixture
def server(monkeypatch):
    """A fresh server, with the mission's world put back afterwards.

    `_fresh_server` reloads `sim.server`, and the reloaded module — mission and
    all — outlives this test in `sys.modules`. Two tests below swap
    `mission.world` for a purpose-built fixture, and without the restore the
    next file to import the module gets a mission whose `agents` name robots its
    world has never heard of (`KeyError: 's1'` in test_routes.py).
    """
    client, module = _fresh_server(monkeypatch)
    original = module.mission.world
    yield client, module
    module.mission.world = original


def test_the_catalog_lists_what_an_operator_may_do(server):
    client, _ = server
    body = client.get("/api/interventions").json()
    assert {k["id"] for k in body["kinds"]} == set(iv.KINDS)
    assert body["max_radius"] == iv.MAX_RADIUS
    assert body["applied"] == []


def test_an_intervention_is_accepted_and_written(server):
    client, module = server
    reply = client.post(
        "/api/intervene", json={"kind": iv.COLLAPSE, "x": 6, "y": 6, "radius": 1}
    )
    assert reply.status_code == 200
    body = reply.json()
    assert body["accepted"] and body["kind"] == iv.COLLAPSE

    written = module.mission.mem.active_hazards(
        module.mission.mission_id, interventions_only=True
    )
    assert [h.intervention_kind for h in written] == [iv.COLLAPSE]


def test_the_route_writes_a_row_and_does_not_touch_the_world(server):
    """The claim issue #22 rests on: an operator has no path to a running
    mission that does not go through fleet memory. If the handler mutated the
    world directly, this would pass anyway and the claim would be false — so
    the assertion is that the world is *unchanged* until a tick runs."""
    client, module = server
    before = module.mission.world.objects[6][6]

    client.post("/api/intervene", json={"kind": iv.RUBBLE, "x": 6, "y": 6, "radius": 0})
    assert module.mission.world.objects[6][6] == before, "the handler mutated the world"

    module.mission.tick_once()
    assert module.mission.world.objects[6][6] == RUBBLE_HEAVY


def test_an_intervention_is_applied_exactly_once(server):
    client, module = server
    client.post(
        "/api/intervene", json={"kind": iv.COLLAPSE, "x": 6, "y": 6, "radius": 0}
    )
    module.mission.tick_once()
    felt = module.mission.world.disruptions_felt
    module.mission.tick_once()
    assert module.mission.world.disruptions_felt == felt


def test_an_unknown_kind_is_a_400(server):
    client, _ = server
    reply = client.post("/api/intervene", json={"kind": "meteor", "x": 5, "y": 5})
    assert reply.status_code == 400
    assert "meteor" in reply.json()["detail"]["reason"]


def test_an_off_map_target_is_a_400(server):
    client, _ = server
    reply = client.post("/api/intervene", json={"kind": iv.COLLAPSE, "x": 999, "y": 5})
    assert reply.status_code == 400


def test_a_non_integer_coordinate_is_a_400(server):
    client, _ = server
    reply = client.post(
        "/api/intervene", json={"kind": iv.COLLAPSE, "x": "left", "y": 5}
    )
    assert reply.status_code == 400


def test_the_catalog_reports_what_has_landed(server):
    client, module = server
    client.post(
        "/api/intervene", json={"kind": iv.COLLAPSE, "x": 6, "y": 6, "radius": 0}
    )
    module.mission.tick_once()
    applied = client.get("/api/interventions").json()["applied"]
    assert len(applied) == 1 and applied[0]["kind"] == iv.COLLAPSE


def test_stranding_a_victim_is_a_409_not_a_400(server):
    """A well-formed request the world refuses. Distinguished from a bad request
    because the operator cannot fix it by correcting the payload — and letting
    it through would quietly kill a run they were only poking at.

    The mission's world is swapped for the single-corridor fixture so the
    refusal is caused by *this fire* closing *that route*. Walling the demo map
    down to make a choke point strands every other victim too, and the test then
    passes without the fire doing anything.
    """
    client, module = server
    module.mission.world = World(parse_map(_victim_map()), seed=1)

    reply = client.post(
        "/api/intervene", json={"kind": iv.BLAZE, "x": 5, "y": 3, "radius": 0}
    )
    assert reply.status_code == 409
    assert reply.json()["detail"]["stranded"] == ["v1"]


def test_the_same_tile_accepts_a_clearable_disruption(server):
    """The refusal is about permanence, not about the tile: debris on the very
    corridor fire was refused for is work, and work is allowed."""
    client, module = server
    module.mission.world = World(parse_map(_victim_map()), seed=1)

    reply = client.post(
        "/api/intervene", json={"kind": iv.RUBBLE, "x": 5, "y": 3, "radius": 0}
    )
    assert reply.status_code == 200


# --- the fleet feels it ------------------------------------------------------


def _fleet_world(width=20, height=20, **extra):
    data = _map(width=width, height=height, **extra)
    data["spawn_points"] = {"medic": [{"x": 5, "y": 5}], "scout": [{"x": 5, "y": 5}]}
    return World(parse_map(data), seed=0)


def _run(world, agents, ticks):
    for _ in range(ticks):
        world.step({a.robot_id: a.step(world) for a in agents})


def test_an_intervention_puts_held_work_back_in_the_pool(mem, mission):
    """The same contract FR-7 gives an aftershock, for a disruption a person
    caused: the robot is not told what changed, it re-decides, and it does not
    keep walking towards a route that may no longer exist."""
    from agents.worker import Worker

    world = _fleet_world()
    mem.register_victim(mission, (15, 15), reported_by="s1")
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    _run(world, [medic], 3)
    held = medic.task
    assert held is not None, "nothing was held when the intervention landed"

    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 10, 10, radius=1))
    medic.step(world)  # the tick that feels it

    released = [
        e
        for e in mem.events(mission)
        if e["verb"] == "task_released" and e["detail"].get("reason") == "intervention"
    ]
    assert released, "the medic held work through an operator intervention"
    assert released[0]["detail"]["task"] == str(held.id)


def test_feeling_an_intervention_is_logged_as_world_changed(mem, mission):
    """`world_changed` rather than `aftershock`, so the console can tell a
    disruption a person caused from one the map did — and both from routine
    planning."""
    from agents.worker import Worker
    from fleetmem.types import WORLD_CHANGED

    world = _fleet_world()
    mem.register_victim(mission, (15, 15), reported_by="s1")
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    _run(world, [medic], 3)
    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 10, 10, radius=1))
    medic.step(world)

    triggers = [p.trigger for p in mem.plans_for(mission, "m1")]
    assert WORLD_CHANGED in triggers


def test_an_aftershock_is_still_logged_as_an_aftershock(mem, mission):
    """Regression: both disruptions now share one counter, and the agent tells
    them apart by comparing two counts. Get that backwards and every scripted
    aftershock starts reporting itself as somebody's doing."""
    from agents.worker import Worker
    from fleetmem.types import AFTERSHOCK

    world = _fleet_world(
        escalations=[
            {"tick": 3, "kind": "aftershock", "block_tiles": [{"x": 9, "y": 9}]}
        ]
    )
    mem.register_victim(mission, (15, 15), reported_by="s1")
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    _run(world, [medic], 4)
    assert AFTERSHOCK in [p.trigger for p in mem.plans_for(mission, "m1")]


def test_a_scout_re_sweeps_after_an_intervention(mem, mission):
    from agents.scout import Scout

    world = _fleet_world(width=12, height=12)
    scout = Scout(robot_id="s1", mission_id=mission, mem=mem)

    _run(world, [scout], 5)
    seen_before = len(scout.explored)
    assert seen_before > 0

    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 6, 6, radius=1))
    scout.step(world)

    assert len(scout.explored) < seen_before, "the scout kept its stale map"


def test_released_work_goes_back_through_the_pool(mem, mission):
    """The recovery half. A robot that feels a disruption surrenders its claim
    and takes the work again the same way anyone else would — through
    `claim_task`, against the pool.

    It very often re-wins it, and that is correct rather than a missed release:
    the route changed, the robot re-decided, and this was still the best thing
    it could do. What must never happen is the claim being *retained* across the
    disruption, so the assertion is on the order of the two events rather than
    on who ended up holding it.
    """
    from agents.worker import Worker

    world = _fleet_world()
    mem.register_victim(mission, (15, 15), reported_by="s1")
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    _run(world, [medic], 3)
    held = medic.task
    assert held is not None

    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 10, 10, radius=1))
    medic.step(world)

    verbs = [
        e["verb"]
        for e in mem.events(mission)
        if e["detail"].get("task") == str(held.id)
        and e["verb"] in ("task_claimed", "task_released")
    ]
    assert verbs[-2:] == ["task_released", "task_claimed"], (
        f"the claim was not surrendered and retaken through the pool: {verbs}"
    )


def test_a_released_task_is_claimable_by_a_robot_that_did_not_hold_it(mem, mission):
    """The same task, offered to someone else while it is in the pool: an
    intervention creates ordinary open work, not a special case."""
    from agents.worker import Worker

    world = _fleet_world()
    mem.register_victim(mission, (15, 15), reported_by="s1")
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)

    _run(world, [medic], 3)
    held = medic.task
    assert held is not None

    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 10, 10, radius=1))
    medic._note_escalation(world)  # the release, without the re-claim that follows

    assert held.id in {t.id for t in mem.open_tasks(mission)}
    assert mem.claim_task(held.id, "m2") is True


# --- the replan path, with a planner attached --------------------------------
#
# Every agent test above runs with `planner=None`, which is a complete robot and
# the path the demo takes with no AWS credentials. It is also the path that
# returns before touching the model — so a whole branch of the disruption replan
# went unexercised, and shipped a `TypeError` that silently killed the tick loop
# the first time an intervention landed on a real cluster. These run it.


class _StubPlanner:
    """A planner that always answers, recording how it was called."""

    def __init__(self, plan=None):
        from bedrock.adapter import Plan

        self.plan_result = (
            plan
            if plan is not None
            else Plan(action="explore", rationale="the corridor is gone; going around")
        )
        self.calls: list[dict] = []

    def plan(self, robot, tick, digest, open_tasks, tactics=(), reserved=False):
        self.calls.append(
            {
                "robot": robot.id,
                "tick": tick,
                "reserved": reserved,
                "tasks": len(open_tasks),
            }
        )
        return self.plan_result


def test_a_disruption_replan_reaches_the_planner(mem, mission):
    """Regression: `_replan_after_disruption` called `_claimable(world)` with one
    argument. Nothing caught it because every other test leaves the planner
    unset, and the tick loop's exception died inside an un-awaited asyncio task
    — so the only symptom was a mission that silently stopped ticking."""
    from agents.worker import Worker

    world = _fleet_world()
    mem.register_victim(mission, (15, 15), reported_by="s1")
    planner = _StubPlanner()
    medic = Worker(
        robot_id="m1", role="medic", mission_id=mission, mem=mem, planner=planner
    )

    _run(world, [medic], 3)
    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 10, 10, radius=1))
    medic.step(world)  # must not raise

    assert planner.calls, "the disruption never reached the planner"


def test_a_disruption_replan_draws_on_the_reserved_budget(mem, mission):
    """`reserved=True` is what stops a robot that spent its ordinary calls
    ranking routine work from answering an intervention with a rule."""
    from agents.worker import Worker

    world = _fleet_world()
    mem.register_victim(mission, (15, 15), reported_by="s1")
    planner = _StubPlanner()
    medic = Worker(
        robot_id="m1", role="medic", mission_id=mission, mem=mem, planner=planner
    )

    _run(world, [medic], 3)
    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 10, 10, radius=1))
    medic.step(world)

    assert any(c["reserved"] for c in planner.calls), (
        "the replan spent ordinary planning budget instead of the reserve"
    )


def test_a_model_replan_is_recorded_as_a_bedrock_decision(mem, mission):
    """`plans.chosen->>'source'` is the query the README invites a judge to run.
    A replan the model actually made has to show up in it as `bedrock`."""
    from agents.planning import BEDROCK
    from agents.worker import Worker
    from fleetmem.types import WORLD_CHANGED

    world = _fleet_world()
    mem.register_victim(mission, (15, 15), reported_by="s1")
    medic = Worker(
        robot_id="m1",
        role="medic",
        mission_id=mission,
        mem=mem,
        planner=_StubPlanner(),
    )

    _run(world, [medic], 3)
    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 10, 10, radius=1))
    medic.step(world)

    sourced = [
        p
        for p in mem.plans_for(mission, "m1")
        if p.trigger == WORLD_CHANGED and p.chosen.get("source") == BEDROCK
    ]
    assert sourced, "the model's replan was not recorded as a Bedrock decision"
    assert sourced[-1].rationale == "the corridor is gone; going around"


def test_a_planner_that_declines_leaves_the_rules_in_charge(mem, mission):
    """§5.4's floor: a throttle, an empty cassette or missing credentials all
    mean `plan()` returns None, and the mission carries on regardless."""
    from agents.worker import Worker

    world = _fleet_world()
    mem.register_victim(mission, (15, 15), reported_by="s1")
    planner = _StubPlanner()
    planner.plan_result = None
    medic = Worker(
        robot_id="m1", role="medic", mission_id=mission, mem=mem, planner=planner
    )

    _run(world, [medic], 3)
    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 10, 10, radius=1))
    medic.step(world)  # must not raise

    assert planner.calls
    assert medic.task is not None, "the robot stopped working when the model declined"


def test_a_planner_that_raises_does_not_stop_the_mission(mem, mission):
    """A model failure is a decision we do not have, not a dead tick loop."""
    from agents.worker import Worker

    class _Exploding(_StubPlanner):
        """Raises only on the disruption replan.

        Scoped deliberately. The ordinary `_consult` path does not guard against
        a planner that raises either, but that is pre-existing and the real
        `Planner` handles its own failures internally — so widening the blast
        radius of this test would be testing somebody else's bug. What is new
        here is a replan triggered by an operator clicking a button, which is
        the worst moment to discover an unguarded call.
        """

        def plan(self, robot, tick, digest, open_tasks, tactics=(), reserved=False):
            if reserved:
                raise RuntimeError("bedrock is having a day")
            return super().plan(robot, tick, digest, open_tasks, tactics, reserved)

    world = _fleet_world()
    mem.register_victim(mission, (15, 15), reported_by="s1")
    medic = Worker(
        robot_id="m1", role="medic", mission_id=mission, mem=mem, planner=_Exploding()
    )

    _run(world, [medic], 3)
    world.apply_intervention(iv.plan(world, iv.COLLAPSE, 10, 10, radius=1))
    medic.step(world)  # must not raise
