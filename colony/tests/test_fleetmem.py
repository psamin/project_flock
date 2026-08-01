"""fleetmem behaviour, run against BOTH the fake and CockroachDB (see conftest).

The coordination mechanics in §4.4 live or die here: single-winner claiming,
dependency unblocking, and the reconcile gate that stops two scouts turning one
victim into two.
"""

import uuid

from bedrock.adapter import BedrockAdapter
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
