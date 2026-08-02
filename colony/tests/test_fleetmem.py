"""fleetmem behaviour, run against BOTH the fake and CockroachDB (see conftest).

The coordination mechanics in §4.4 live or die here: single-winner claiming,
dependency unblocking, and the reconcile gate that stops two scouts turning one
victim into two.
"""

import hashlib
import math
import uuid

import pytest

from bedrock.adapter import EMBED_DIMS, BedrockAdapter
from fleetmem.types import BLOCKED, CLAIMED, DONE, OPEN


# --- reconcile gate (§4.2 step 3) -------------------------------------------


def test_two_robots_seeing_the_same_thing_produce_one_belief(mem, mission):
    """The gate's whole job. Without it, S1 and S2 spotting one victim creates
    two victims and the fleet double-dispatches."""
    embedder = BedrockAdapter()
    vec = embedder.embed("victim under rubble at 14,9")

    first = mem.report_observation(mission, "s1", "victim", (14, 9), embedding=vec)
    second = mem.report_observation(mission, "s2", "victim", (15, 9), embedding=vec)

    assert first == second, "the same victim was recorded twice"
    beliefs = mem.get_beliefs(mission, kind="victim")
    assert len(beliefs) == 1
    assert beliefs[0].sightings == 2, "a merge should bump the sighting count"


def test_confidence_rises_with_corroboration(mem, mission):
    vec = BedrockAdapter().embed("hazard: fire spreading")
    mem.report_observation(mission, "s1", "hazard", (5, 5), embedding=vec, confidence=0.5)
    before = mem.get_beliefs(mission, kind="hazard")[0].confidence
    mem.report_observation(mission, "s2", "hazard", (5, 5), embedding=vec, confidence=0.5)
    after = mem.get_beliefs(mission, kind="hazard")[0].confidence
    assert after > before


def test_distant_sightings_stay_separate(mem, mission):
    """Same description, far apart — two different victims, not one merged."""
    vec = BedrockAdapter().embed("victim under rubble")
    a = mem.report_observation(mission, "s1", "victim", (2, 2), embedding=vec)
    b = mem.report_observation(mission, "s2", "victim", (30, 25), embedding=vec)
    assert a != b
    assert len(mem.get_beliefs(mission, kind="victim")) == 2


def test_different_kinds_never_merge(mem, mission):
    vec = BedrockAdapter().embed("something at 10,10")
    a = mem.report_observation(mission, "s1", "victim", (10, 10), embedding=vec)
    b = mem.report_observation(mission, "s1", "hazard", (10, 10), embedding=vec)
    assert a != b


def test_gate_still_works_without_embeddings(mem, mission):
    """Bedrock unavailable must degrade, not break — the gate falls back to
    position and kind."""
    a = mem.report_observation(mission, "s1", "victim", (7, 7))
    b = mem.report_observation(mission, "s2", "victim", (8, 7))
    assert a == b


def _unit(seed: str) -> list[float]:
    """Deterministic unit vector, no numpy."""
    digest = hashlib.sha512(seed.encode()).digest()
    raw = [(digest[i % len(digest)] / 255.0) - 0.5 for i in range(EMBED_DIMS)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


def _blend(base: list[float], other: list[float], weight: float) -> list[float]:
    mixed = [a + weight * b for a, b in zip(base, other)]
    norm = math.sqrt(sum(v * v for v in mixed)) or 1.0
    return [v / norm for v in mixed]


# Far from (14,9) so they cannot legitimately merge with it, and far from each
# other so they stay distinct beliefs rather than collapsing into one.
DECOY_POSITIONS = [(0, 0), (35, 0), (0, 29), (35, 29), (35, 15)]


def test_merge_survives_nearer_but_ineligible_beliefs(mem, mission):
    """Regression: the candidate filter must be part of the nearest-neighbour
    query, not applied to its results.

    Two scouts describe one victim slightly differently, so the second sighting's
    embedding is close to the first but not identical. Meanwhile five same-kind
    beliefs elsewhere on the map sit *nearer* in embedding space. Filtering
    position after a top-k means the five decoys fill the result set, the real
    duplicate never surfaces, and one victim becomes two — the double-dispatch
    the gate exists to prevent.

    Verified to fail against the pre-fix implementation (2 beliefs at (14,9))
    and pass after it (1 belief, 2 sightings). Earlier gate tests all ran on
    near-empty missions and could not have caught this.
    """
    stored = _unit("victim under rubble at 14,9")
    second_sighting = _blend(stored, _unit("scout-2 phrasing"), 0.05)

    mem.report_observation(mission, "s1", "victim", (14, 9), embedding=stored)
    for i, pos in enumerate(DECOY_POSITIONS):
        mem.report_observation(
            mission, "s2", "victim", pos,
            embedding=_blend(second_sighting, _unit(f"decoy{i}"), 0.004),
        )

    again = mem.report_observation(
        mission, "s3", "victim", (14, 9), embedding=second_sighting
    )

    at_target = [b for b in mem.get_beliefs(mission, kind="victim") if b.pos == (14, 9)]
    assert len(at_target) == 1, f"duplicate victim created at (14,9): {at_target}"
    assert at_target[0].id == again
    assert at_target[0].sightings == 2


def test_unknown_dependency_ids_are_rejected(mem, mission):
    """Regression: unchecked, the client silently opened such a task while the
    fake raised KeyError. Either way a medic could be dispatched to a victim
    still behind rubble."""
    import uuid as _uuid

    with pytest.raises(ValueError, match="unknown task"):
        mem.create_task(mission, "deliver_kit", depends_on=[_uuid.uuid4()])


def test_beliefs_can_be_scoped_to_an_area(mem, mission):
    mem.report_observation(mission, "s1", "victim", (2, 2))
    mem.report_observation(mission, "s1", "victim", (35, 28))
    near = mem.get_beliefs(mission, area=(0, 0, 10, 10))
    assert len(near) == 1
    assert near[0].pos == (2, 2)


def test_missions_are_isolated(mem):
    """Two missions in one database must not see each other's beliefs."""
    a, b = uuid.uuid4(), uuid.uuid4()
    mem.report_observation(a, "s1", "victim", (5, 5))
    assert mem.get_beliefs(b) == []


# --- claiming and dependencies (§4.4) ---------------------------------------


def test_only_one_robot_can_claim_a_task(mem, mission):
    task = mem.create_task(mission, "clear_debris", (14, 8))
    assert mem.claim_task(task, "l1") is True
    assert mem.claim_task(task, "l2") is False, "two robots claimed the same task"


def test_a_task_with_dependencies_starts_blocked(mem, mission):
    clear = mem.create_task(mission, "clear_debris", (14, 8))
    deliver = mem.create_task(mission, "deliver_kit", (14, 9), depends_on=[clear])

    open_ids = {t.id for t in mem.open_tasks(mission)}
    assert clear in open_ids
    assert deliver not in open_ids, "a blocked task must not be claimable"


def test_completing_the_dependency_unblocks_the_dependent(mem, mission):
    """The handoff (§4.2 step 6): the lifter finishing is what makes the medic's
    task claimable, with no human in the loop."""
    clear = mem.create_task(mission, "clear_debris", (14, 8))
    deliver = mem.create_task(mission, "deliver_kit", (14, 9), depends_on=[clear])

    mem.claim_task(clear, "l1")
    unblocked = mem.complete_task(clear, "l1")

    assert unblocked == [deliver]
    assert deliver in {t.id for t in mem.open_tasks(mission)}
    assert mem.claim_task(deliver, "m1") is True


def test_a_task_waits_for_every_dependency(mem, mission):
    """§3.3's hardest victim sits behind two debris walls, so its delivery task
    depends on two clears. One finishing must not open it."""
    first = mem.create_task(mission, "clear_debris", (3, 27))
    second = mem.create_task(mission, "clear_debris", (4, 27))
    deliver = mem.create_task(mission, "deliver_kit", (3, 27), depends_on=[first, second])

    mem.claim_task(first, "l1")
    assert mem.complete_task(first, "l1") == [], "opened before all dependencies were done"

    mem.claim_task(second, "l1")
    assert mem.complete_task(second, "l1") == [deliver]


def test_only_the_claimer_can_complete(mem, mission):
    task = mem.create_task(mission, "clear_debris", (1, 1))
    mem.claim_task(task, "l1")
    assert mem.complete_task(task, "l2") == []


def test_completing_twice_does_not_re_unblock(mem, mission):
    """At-least-once delivery is a fact of life; the second call must be a no-op
    rather than re-firing the handoff."""
    clear = mem.create_task(mission, "clear_debris", (1, 1))
    deliver = mem.create_task(mission, "deliver_kit", (1, 2), depends_on=[clear])
    mem.claim_task(clear, "l1")

    assert mem.complete_task(clear, "l1") == [deliver]
    assert mem.complete_task(clear, "l1") == []


def test_open_tasks_are_priority_ordered(mem, mission):
    mem.create_task(mission, "explore", priority=1)
    urgent = mem.create_task(mission, "deliver_kit", priority=9)
    assert mem.open_tasks(mission)[0].id == urgent


# --- liveness and log --------------------------------------------------------


def test_heartbeat_keeps_a_robot_fresh(mem, mission):
    mem.register_robot("s1", "scout", (2, 2), battery=120)
    mem.heartbeat("s1", pos=(3, 2), battery=119, status="exploring")
    assert "s1" not in mem.stale_robots(seconds=10)


def test_a_silent_robot_goes_stale(mem, mission):
    """>10s stale is what triggers claim release and reassignment (§4.4)."""
    mem.register_robot("l1", "lifter", (2, 4), battery=300)
    assert "l1" in mem.stale_robots(seconds=0)


def test_events_are_appended_in_order(mem, mission):
    mem.log_event(mission, "s1", "victim_found", {"pos": [14, 9]})
    mem.log_event(mission, "l1", "task_claimed", {"kind": "clear_debris"})
    verbs = [e["verb"] for e in mem.events(mission)]
    assert verbs == ["victim_found", "task_claimed"]


# --- provenance memory: plans (FR-17, §4.0) ---------------------------------


def test_a_plan_records_the_memories_that_drove_it(mem, mission):
    """The provenance leg of the four-memory thesis. Storing `based_on` is what
    turns "the robot went to the office" into an answer to "why?" that a judge
    can trace back to specific rows."""
    a = mem.report_observation(mission, "s1", "victim", (14, 9))
    b = mem.report_observation(mission, "s1", "hazard", (20, 9))

    plan_id = mem.log_plan(
        mission, "l1", trigger="task_done",
        chosen={"action": "claim_task", "task_id": "t1"},
        rationale="closest reachable victim, route avoids the fire",
        based_on=[a, b],
    )

    plans = mem.plans_for(mission, "l1")
    assert len(plans) == 1
    assert plans[0].id == plan_id
    assert plans[0].trigger == "task_done"
    assert plans[0].chosen["action"] == "claim_task"
    assert set(plans[0].based_on) == {a, b}


def test_plans_are_scoped_by_robot_and_mission(mem, mission):
    mem.log_plan(mission, "s1", "idle", {"action": "explore"}, "sweeping A1")
    mem.log_plan(mission, "l1", "idle", {"action": "explore"}, "staging")

    assert len(mem.plans_for(mission)) == 2
    assert len(mem.plans_for(mission, "s1")) == 1
    assert mem.plans_for(uuid.uuid4()) == []


def test_a_plan_with_no_sources_is_still_recorded(mem, mission):
    """Rule-based fallback plans have no digest behind them; they must still be
    traceable, or the decision log has holes exactly when Bedrock was down."""
    mem.log_plan(mission, "s1", "idle", {"action": "explore"}, "no tasks open")
    assert mem.plans_for(mission)[0].based_on == []


# --- lease semantics shared by both implementations -------------------------


def test_open_tasks_includes_work_abandoned_by_a_dead_robot(mem, mission):
    """Recovery has no separate step: to the allocator, a dead lease is just
    availability (§4.4)."""
    task = mem.create_task(mission, "clear_debris", (1, 1))
    mem.claim_task(task, "l1", lease_seconds=-1)

    assert task in {t.id for t in mem.open_tasks(mission)}


def test_open_tasks_excludes_live_work(mem, mission):
    task = mem.create_task(mission, "clear_debris", (1, 1))
    mem.claim_task(task, "l1")
    assert task not in {t.id for t in mem.open_tasks(mission)}


# --- the rescue chain the gate creates (§4.2 step 3) ------------------------


def test_a_new_victim_creates_the_clear_then_deliver_chain(mem, mission):
    """The handoff exists in the data, not in any message between robots: the
    medic's task is gated on the lifter's by depends_on."""
    victim, tasks = mem.register_victim(
        mission, (14, 9), reported_by="s1", blocked_by=[(14, 8)]
    )

    assert victim is not None
    assert len(tasks) == 2

    open_now = {t.kind for t in mem.open_tasks(mission)}
    assert open_now == {"clear_debris"}, "deliver_kit should be blocked, not open"


def test_completing_the_clear_unblocks_the_delivery(mem, mission):
    _, (clear, deliver) = mem.register_victim(
        mission, (14, 9), reported_by="s1", blocked_by=[(14, 8)]
    )

    mem.claim_task(clear, "l1")
    assert mem.complete_task(clear, "l1") == [deliver]
    assert mem.claim_task(deliver, "m1") is True


def test_a_victim_behind_two_walls_waits_for_both(mem, mission):
    """§3.3's hardest victim: scout -> lifter -> lifter -> medic."""
    _, tasks = mem.register_victim(
        mission, (3, 27), reported_by="s1", blocked_by=[(4, 27), (5, 27)]
    )
    first, second, deliver = tasks

    mem.claim_task(first, "l1")
    assert mem.complete_task(first, "l1") == [], "opened before both clears were done"
    mem.claim_task(second, "l1")
    assert mem.complete_task(second, "l1") == [deliver]


def test_a_reachable_victim_needs_no_clearing(mem, mission):
    _, tasks = mem.register_victim(mission, (12, 10), reported_by="s1")
    assert len(tasks) == 1
    assert [t.kind for t in mem.open_tasks(mission)] == ["deliver_kit"]


def test_registering_the_same_victim_twice_does_not_double_dispatch(mem, mission):
    """Two scouts sighting one victim must not create two rescue chains — the
    same duplication the reconcile gate prevents for beliefs."""
    first_id, first_tasks = mem.register_victim(
        mission, (14, 9), reported_by="s1", blocked_by=[(14, 8)]
    )
    second_id, second_tasks = mem.register_victim(
        mission, (14, 9), reported_by="s2", blocked_by=[(14, 8)]
    )

    assert first_id == second_id
    assert set(second_tasks) == set(first_tasks), "a second chain was created"
    assert len(mem.open_tasks(mission)) == 1, "the fleet was dispatched twice"
