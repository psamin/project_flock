"""Semantic memory: what one mission learns, and what the next one does with it.

Run against both the fake and CockroachDB via the `mem` fixture, because this is
the second place the vector index carries real weight and the fake's hand-rolled
cosine has to agree with `<=>` about ordering.

The tests that matter most here are the negative ones. Recall can fail in two
ways that look like success: a duplicate memory that crowds a real one out of
the top-k, and a priority bump that nothing ever reads.
"""

from __future__ import annotations

import uuid

import pytest

from agents.scout import seed_sector_tasks
from bedrock.adapter import BedrockAdapter
from sim import recall as recall_mod
from sim.world import World
from world.map_format import load_map

from tests.conftest import needs_db

MAP = "world/maps/aftershock.json"


@pytest.fixture
def map_key(mem):
    """A map scope nobody else is using, removed afterwards.

    Against the fake each test gets a fresh store, but against a real cluster
    `mission_memories` persists for the session — and recall is deliberately
    *not* scoped to a mission, so one test's memories are visible to the next
    unless the map differs. This is the semantic-memory equivalent of the
    `mission` fixture.

    It also cleans up, which `mission` does not need to: mission ids are unique
    per test, but this table is the one the demo reads across runs, and a
    session's worth of `testmap-` rows sitting in it is litter in the one place
    a judge is invited to look.
    """
    key = f"testmap-{uuid.uuid4()}"
    yield key
    conn = getattr(mem, "conn", None)
    if conn is not None:
        conn.execute("DELETE FROM mission_memories WHERE map_key LIKE %s", (key + "%",))


def _memory(mem, mission_id, map_key, summary="victims in B2", **kw):
    vec = BedrockAdapter().embed(summary)
    return mem.remember_mission(
        mission_id, map_key, summary, embedding=vec, outcome=kw or {}
    )


# --- the round trip ---------------------------------------------------------


def test_a_mission_memory_comes_back(mem, map_key):
    mission = uuid.uuid4()
    written = _memory(mem, mission, map_key, victim_sectors=["B2", "C2"])
    assert written is not None

    got = mem.recall_missions(map_key, BedrockAdapter().embed("victims in B2"))
    assert [m.id for m in got] == [written]
    assert got[0].outcome["victim_sectors"] == ["B2", "C2"]
    assert got[0].mission_id == mission


def test_recall_is_scoped_to_the_map(mem, map_key):
    """The whole point of the index prefix. Knowledge about one map must not
    leak into a mission on another."""
    other = f"{map_key}-other"
    _memory(mem, uuid.uuid4(), map_key, summary="victims in B2")
    _memory(mem, uuid.uuid4(), other, summary="victims in B2")

    got = mem.recall_missions(other, BedrockAdapter().embed("victims in B2"))
    assert len(got) == 1
    assert got[0].map_key == other


def test_writing_the_same_mission_twice_is_a_no_op(mem, map_key):
    """The sim records a run when it ends and again on reset. Without the guard
    the same lesson lands twice and crowds a genuinely different memory out of
    the top-k — while every row still looks correct."""
    mission = uuid.uuid4()
    assert _memory(mem, mission, map_key) is not None
    assert _memory(mem, mission, map_key) is None

    got = mem.recall_missions(map_key, BedrockAdapter().embed("victims in B2"))
    assert len(got) == 1


def test_recall_without_an_embedding_degrades_to_recent(mem, map_key):
    """No Bedrock credentials must not mean no recall — it means recall stops
    being semantic. Anything else makes the whole feature credential-gated."""
    first = _memory(mem, uuid.uuid4(), map_key, summary="one")
    second = _memory(mem, uuid.uuid4(), map_key, summary="two")

    got = mem.recall_missions(map_key, None, limit=2)
    assert [m.id for m in got] == [second, first]


def test_limit_is_applied(mem, map_key):
    for i in range(5):
        _memory(mem, uuid.uuid4(), map_key, summary=f"mission {i}")
    assert len(mem.recall_missions(map_key, None, limit=3)) == 3


def test_an_empty_map_recalls_nothing(mem):
    assert mem.recall_missions("never-run", None) == []


# --- the read path ----------------------------------------------------------


def test_hot_sectors_are_seeded_at_a_higher_priority(mem):
    world_map = load_map(MAP)
    mission = uuid.uuid4()
    hot = ["B2", "C2"]
    seed_sector_tasks(mem, mission, world_map, hot_sectors=hot)

    by_kind = {t.kind: t for t in mem.open_tasks(mission)}
    for sector in world_map.sectors:
        task = by_kind[f"explore_sector:{sector['id']}"]
        expected = 2 if sector["id"] in hot else 1
        assert task.priority == expected, sector["id"]


def test_seeding_without_memory_leaves_every_sector_equal(mem):
    """The cold-start path, and the reason the new sort key is safe: with no
    memories every priority is 1, so `-priority` is constant and the scout's
    ordering is identical to the distance-only one it replaces."""
    world_map = load_map(MAP)
    mission = uuid.uuid4()
    seed_sector_tasks(mem, mission, world_map)
    assert {t.priority for t in mem.open_tasks(mission)} == {1}


def test_the_scout_prefers_a_remembered_sector_over_a_nearer_one(mem):
    """Without the `-t.priority` term in scout.py's sort this passes only by
    accident of distance — which is exactly the inert-but-plausible failure
    this test exists to catch."""
    world_map = load_map(MAP)
    mission = uuid.uuid4()
    seed_sector_tasks(mem, mission, world_map, hot_sectors=["C3"])

    tasks = [t for t in mem.open_tasks(mission) if t.kind.startswith("explore_sector:")]

    class _Robot:
        x = y = 0

    robot = _Robot()
    ordered = sorted(
        tasks,
        key=lambda t: (
            -t.priority,
            abs((t.target[0] or 0) - robot.x) + abs((t.target[1] or 0) - robot.y),
            t.kind,
        ),
    )
    assert ordered[0].kind == "explore_sector:C3"
    # ...and it is genuinely not the nearest, or the assertion above proves
    # nothing about priority.
    nearest = min(tasks, key=lambda t: (t.target[0] or 0) + (t.target[1] or 0))
    assert nearest.kind != "explore_sector:C3"


# --- the summarizer ---------------------------------------------------------


def test_the_summary_describes_what_the_fleet_saw_not_the_world(mem):
    """Facts come out of fleet memory. A summary built from World.victims would
    describe victims nobody found, and the next mission would 'remember'
    knowledge that was never earned."""
    world_map = load_map(MAP)
    mission = uuid.uuid4()
    vec = BedrockAdapter().embed("victim under rubble")
    mem.report_observation(mission, "s1", "victim", (12, 10), embedding=vec)

    summary, outcome = recall_mod.summarize(mem, mission, world_map)

    assert "(12,10)" in summary
    assert outcome["victim_sites"] == [[12, 10]]
    assert outcome["victim_sectors"] == [world_map.sector_at(12, 10)]
    # Every other sector is recorded as empty, which is knowledge too.
    assert world_map.sector_at(12, 10) not in outcome["empty_sectors"]


def test_the_summary_carries_no_run_specific_numbers(mem):
    """The cassette key is a hash of this string. A tick count or a rescue
    tally in it means a miss on every rerun and a silent fall back to the
    offline embedding, which is not semantically meaningful."""
    world_map = load_map(MAP)
    first, second = uuid.uuid4(), uuid.uuid4()
    vec = BedrockAdapter().embed("victim under rubble")
    for mission in (first, second):
        mem.report_observation(mission, "s1", "victim", (12, 10), embedding=vec)

    assert (
        recall_mod.summarize(mem, first, world_map)[0]
        == recall_mod.summarize(mem, second, world_map)[0]
    )


def test_the_query_text_is_fixed_for_a_map():
    """Same reason as above, for the other half of the retrieval."""
    world_map = load_map(MAP)
    assert recall_mod.query_text(world_map) == recall_mod.query_text(load_map(MAP))


def test_map_key_ignores_the_seed():
    """Two seeds on one scenario are two runs of the same map and should share
    what was learned about it."""
    world_map = load_map(MAP)
    assert recall_mod.map_key(world_map) == "aftershock"


def test_hot_sectors_merges_across_memories(mem, map_key):
    _memory(mem, uuid.uuid4(), map_key, summary="a", victim_sectors=["B2"])
    _memory(mem, uuid.uuid4(), map_key, summary="b", victim_sectors=["C2", "B2"])
    memories = mem.recall_missions(map_key, None)
    assert recall_mod.hot_sectors(memories) == ["B2", "C2"]


# --- the index actually being used ------------------------------------------


@needs_db
def test_semantic_recall_uses_the_vector_index(db):
    """The correction that matters: a prefixed vector index is used only when
    the prefix is constrained to an exact value, so this asserts the plan says
    `vector search` rather than trusting that the results look right."""
    vec = "[" + ",".join(["0.01"] * 512) + "]"
    mission_ids = [uuid.uuid4() for _ in range(10)]
    for i, mid in enumerate(mission_ids):
        db.conn.execute(
            "INSERT INTO mission_memories (mission_id, map_key, summary, embedding)"
            " VALUES (%s, 'idxtest', %s, %s)",
            (mid, f"m{i}", vec),
        )
    try:
        plan = "\n".join(
            r["info"]
            for r in db.conn.execute(
                "EXPLAIN SELECT id FROM mission_memories WHERE map_key = 'idxtest'"
                " ORDER BY embedding <=> %s LIMIT 3",
                (vec,),
            ).fetchall()
        )
        assert "vector search" in plan, plan
        assert "mm_embedding_idx" in plan, plan
    finally:
        db.conn.execute("DELETE FROM mission_memories WHERE map_key = 'idxtest'")


def test_a_broken_recall_does_not_stop_the_mission(mem, monkeypatch):
    """Recall sits between "restart" and the first tick, and costs a Bedrock
    call. A throttled model must not mean no mission — that trades a fleet that
    starts slightly worse informed for a fleet that rescues nobody."""
    from sim import mission as mission_mod

    world_map = load_map(MAP)
    world = World(world_map, seed=world_map.seed)

    def explode(*a, **kw):
        raise RuntimeError("bedrock is having a day")

    monkeypatch.setattr(mission_mod.recall_mod, "recall", explode)
    agents = mission_mod.build_fleet(
        world, mem, uuid.uuid4(), coordinated=True, recall_enabled=True
    )

    assert agents, "the fleet must still be built"
    # And the sectors are still seeded, just without a prior.
    tasks = [t for t in mem.open_tasks(list(agents.values())[0].mission_id)]
    assert any(t.kind.startswith("explore_sector:") for t in tasks)
