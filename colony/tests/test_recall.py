"""Semantic memory: tactics learned from one mission, applied in the next.

Run against both the fake and CockroachDB via the `mem` fixture, because this is
the second place the vector index carries real weight and the fake's hand-rolled
cosine has to agree with `<=>` about ordering.

The tests that matter most here are the ones about what a lesson may *contain*.
A lesson naming a coordinate is not knowledge, it is a fact about one map — it
transfers to no other disaster, and a fleet recalling victim positions is a
fleet handed the answer. That failure is invisible from the outside: the rows
look fine either way.
"""

from __future__ import annotations

import uuid

import pytest

from bedrock.adapter import BedrockAdapter
from sim import recall as recall_mod
from sim.world import World
from world.map_format import load_map

from tests.conftest import needs_db

MAP = "world/maps/aftershock.json"


@pytest.fixture
def clean(mem):
    """Semantic memory is deliberately global — no mission and no map scope — so
    against a real cluster one test's lessons are visible to every other. This
    is the equivalent of the `mission` fixture for a table that has no mission.
    """
    conn = getattr(mem, "conn", None)
    if conn is not None:
        conn.execute("DELETE FROM mission_memories")
    yield
    if conn is not None:
        conn.execute("DELETE FROM mission_memories")


def _lesson(mem, situation, lesson="stage the medic early", mission=None):
    return mem.remember_lesson(
        mission or uuid.uuid4(),
        situation,
        lesson,
        embedding=BedrockAdapter().embed(situation),
        evidence={"run": "test"},
    )


# --- the round trip ---------------------------------------------------------


def test_a_lesson_comes_back(mem, clean):
    mission = uuid.uuid4()
    written = _lesson(mem, "victim behind rubble", "stage the medic", mission)

    got = mem.recall_lessons(BedrockAdapter().embed("victim behind rubble"))
    assert [m.id for m in got] == [written]
    assert got[0].lesson == "stage the medic"
    assert got[0].mission_id == mission


def test_recall_crosses_missions_and_maps(mem, clean):
    """The point of the redesign. A tactic learned on one map is meant to apply
    on the next, so there is deliberately no scope argument to pass."""
    a = _lesson(mem, "fire near a located victim", "prioritise by hazard")
    b = _lesson(mem, "rubble blocking the only route", "clear before dispatch")

    got = mem.recall_lessons(None, limit=5)
    assert {m.id for m in got} == {a, b}


def test_recall_without_an_embedding_degrades_to_recent(mem, clean):
    """No Bedrock credentials must not mean no recall — it means recall stops
    being semantic. Anything else makes the whole feature credential-gated."""
    first = _lesson(mem, "one")
    second = _lesson(mem, "two")
    got = mem.recall_lessons(None, limit=2)
    assert [m.id for m in got] == [second, first]


def test_limit_is_applied(mem, clean):
    for i in range(5):
        _lesson(mem, f"situation {i}")
    assert len(mem.recall_lessons(None, limit=3)) == 3


def test_an_empty_memory_recalls_nothing(mem, clean):
    assert mem.recall_lessons(None) == []


def test_retrieval_is_counted(mem, clean):
    """A lesson nothing ever retrieves is dead weight, and the console's claim
    that the fleet *leans on* a tactic rests on this number."""
    written = _lesson(mem, "victim behind rubble")
    assert mem.recall_lessons(None)[0].times_recalled == 0

    mem.mark_recalled([written])
    mem.mark_recalled([written])
    assert mem.recall_lessons(None)[0].times_recalled == 2


def test_marking_nothing_is_harmless(mem, clean):
    mem.mark_recalled([])  # a decision that recalled nothing still logs


# --- what a lesson may contain ----------------------------------------------


def test_the_run_digest_names_no_places(mem, clean):
    """The digest is the model's only view of the run, so anything place-shaped
    in it comes back as a place-shaped lesson. Not offering the temptation is
    stronger than forbidding it in the prompt — which we also do."""
    world_map = load_map(MAP)
    world = World(world_map, seed=world_map.seed)
    mission = uuid.uuid4()
    vec = BedrockAdapter().embed("victim under rubble")
    mem.report_observation(mission, "s1", "victim", (12, 10), embedding=vec)
    mem.log_event(mission, "l1", "debris_cleared", {"x": 12, "y": 9})

    digest = recall_mod.run_digest(mem, mission, world, {"ticks": 300})

    for banned in ("(12,10)", "12,10", "B2", "sector"):
        assert banned not in digest, f"{banned!r} leaked into the digest:\n{digest}"


def test_the_lessons_prompt_forbids_coordinates():
    """The instruction is load-bearing, so it is asserted rather than trusted."""
    from bedrock.adapter import _lessons_prompt

    prompt = _lessons_prompt("rescued 5 of 8", limit=3)
    assert "coordinates" in prompt
    assert "sector" in prompt
    assert "DIFFERENT maps" in prompt


def test_malformed_lessons_are_dropped_not_stored():
    """A malformed lesson is worse than no lesson: written once, then retrieved
    into every similar situation forever."""
    from bedrock.adapter import _parse_lessons

    assert _parse_lessons("not json at all", 3) == []
    assert _parse_lessons('{"lessons": [{"situation": "x"}]}', 3) == []
    assert _parse_lessons('{"lessons": [{"lesson": "y"}]}', 3) == []
    assert _parse_lessons('{"lessons": []}', 3) == []
    good = _parse_lessons('{"lessons": [{"situation": "a", "lesson": "b"}]}', 3)
    assert good == [{"situation": "a", "lesson": "b"}]


def test_lessons_are_capped():
    """A run that produced eight insights produced none."""
    from bedrock.adapter import _parse_lessons

    payload = (
        '{"lessons": ['
        + ",".join(f'{{"situation": "s{i}", "lesson": "l{i}"}}' for i in range(9))
        + "]}"
    )
    assert len(_parse_lessons(payload, 3)) == 3


# --- the situation a robot searches with ------------------------------------


class _Robot:
    role = "medic"
    battery = 180
    kits = 2


def test_the_situation_describes_conditions_not_places(mem, clean):
    """It is the query vector, so it has to land near the `situation` half of
    stored lessons — and it must not smuggle coordinates in either."""
    situation = recall_mod.situation_of(_Robot(), mem.get_beliefs(uuid.uuid4()), [])
    assert "medic" in situation
    assert "kits" in situation
    assert "(" not in situation


def test_the_same_predicament_produces_the_same_query(mem, clean):
    """Determinism, and the reason one recorded embedding serves every rerun:
    the situation text is a cassette key like any other prompt."""
    beliefs = mem.get_beliefs(uuid.uuid4())
    assert recall_mod.situation_of(_Robot(), beliefs, []) == recall_mod.situation_of(
        _Robot(), beliefs, []
    )


def test_prompt_lines_pair_the_condition_with_the_advice(mem, clean):
    _lesson(mem, "a victim is behind heavy rubble", "stage the medic adjacent")
    lines = recall_mod.as_prompt_lines(mem.recall_lessons(None))
    assert lines == ["when a victim is behind heavy rubble — stage the medic adjacent"]


def test_tactics_only_reach_the_prompt_when_there_are_some():
    """A fleet that has learned nothing must produce the exact prompt it always
    did, or every cassette entry recorded before memory existed stops matching.
    """
    from bedrock.adapter import _plan_prompt

    without = _plan_prompt("card", "beliefs", [])
    assert "earlier missions learned" not in without
    with_tactics = _plan_prompt("card", "beliefs", [], ["when x — do y"])
    assert "earlier missions learned" in with_tactics


# --- the guards -------------------------------------------------------------


def test_the_baseline_never_reads_semantic_memory(mem, clean):
    """A baseline run is a control condition. If it read what coordinated runs
    learned, the ON/OFF comparison would be measuring its own history."""
    from agents.worker import Worker

    _lesson(mem, "anything at all")
    worker = Worker(
        robot_id="l1",
        role="lifter",
        mission_id=uuid.uuid4(),
        mem=mem,
        coordinated=False,
        embedder=BedrockAdapter(),
    )
    assert worker._recall(_Robot(), []) == []


def test_a_failed_recall_costs_the_decision_its_memory_not_the_robot(mem, clean):
    """Retrieval sits on the path between a plan boundary and an action. A
    throttled model must cost this decision its memory, not stall the fleet."""
    from agents.worker import Worker

    class _Exploding:
        def embed(self, text):
            raise RuntimeError("bedrock is having a day")

    worker = Worker(
        robot_id="l1",
        role="lifter",
        mission_id=uuid.uuid4(),
        mem=mem,
        coordinated=True,
        embedder=_Exploding(),
    )
    assert worker._recall(_Robot(), []) == []


def test_a_robot_with_no_embedder_is_still_a_complete_robot(mem, clean):
    """The rules floor (§5.4). Memory improves choices; it does not enable
    them."""
    from agents.worker import Worker

    worker = Worker(robot_id="l1", role="lifter", mission_id=uuid.uuid4(), mem=mem)
    assert worker.planner is None
    assert worker.embedder is None
    assert worker._recall(_Robot(), []) == []


# --- provenance -------------------------------------------------------------


def test_recalled_tactics_are_recorded_separately_from_beliefs(mem, clean):
    """`based_on` resolves against `observations` and `recalled_from` against
    `mission_memories`. Merged into one column an id resolves to nothing at all
    — which is how a decision trace turns back into a plausible story."""
    mission = uuid.uuid4()
    tactic = _lesson(mem, "victim behind rubble")
    belief = mem.report_observation(
        mission, "s1", "victim", (5, 5), embedding=BedrockAdapter().embed("victim")
    )

    mem.log_plan(
        mission,
        "l1",
        trigger="idle",
        chosen={"action": "claim_task", "source": "bedrock"},
        rationale="because",
        based_on=[belief],
        recalled_from=[tactic],
    )
    plan = mem.plans_for(mission, "l1")[0]
    assert plan.based_on == [belief]
    assert plan.recalled_from == [tactic]


# --- the index actually being used ------------------------------------------


@needs_db
def test_semantic_recall_uses_the_vector_index(db):
    """Asserts the plan, not the results, because every way this breaks returns
    perfectly plausible rows.

    It has already caught one: `WHERE embedding IS NOT NULL` — which reads as
    hygiene — moves the plan from `vector search` to a FULL SCAN, because only
    filters matching a prefix column keep a vector index engaged and that one
    matches nothing. The query below is the one `recall_lessons` actually runs;
    a test that exercised a tidier query would pass while the SDK full-scanned.
    """
    vec = "[" + ",".join(["0.01"] * 512) + "]"
    db.conn.execute("DELETE FROM mission_memories")
    for i in range(10):
        db.conn.execute(
            "INSERT INTO mission_memories (mission_id, situation, lesson, embedding)"
            " VALUES (%s, %s, 'l', %s)",
            (uuid.uuid4(), f"s{i}", vec),
        )
    try:
        plan = "\n".join(
            r["info"]
            for r in db.conn.execute(
                "EXPLAIN SELECT id FROM mission_memories"
                " ORDER BY embedding <=> %s LIMIT 3",
                (vec,),
            ).fetchall()
        )
        assert "vector search" in plan, plan
        assert "mm_situation_idx" in plan, plan
    finally:
        db.conn.execute("DELETE FROM mission_memories")
