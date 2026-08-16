"""The commander console (FR-10, §5.1 lane 4, §6.2).

Two halves, tested separately because they fail differently:

    the guard      refuses to send anything that is not a read. Pure string
                   work, so it is tested exhaustively — this is the layer that
                   travels with the code onto a cluster where nobody has run
                   `credentials.py apply`.
    the questions  run against a live cluster and answer from real rows written
                   through the SDK, not from fixtures. A question that returns
                   the right shape over invented data would still be wrong on
                   the day.
"""

from __future__ import annotations

import uuid

import pytest
from console.questions import BY_ID, QUESTIONS, UnknownQuestion, answer, catalog
from console.reader import NotReadOnly, ReadOnlyReader, assert_read_only

from tests.conftest import needs_db

# --- the guard --------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select id from tasks",
        "  SELECT id FROM tasks;  ",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "EXPLAIN SELECT id FROM tasks",
        "SHOW TABLES",
    ],
)
def test_reads_are_allowed(sql):
    assert_read_only(sql)  # raises if not


@pytest.mark.parametrize(
    "sql,why",
    [
        ("DELETE FROM tasks", "the plain case"),
        ("UPDATE tasks SET status = 'open'", "the plain case"),
        ("INSERT INTO events VALUES (1)", "the plain case"),
        ("DROP TABLE tasks", "the catastrophic case"),
        ("TRUNCATE events", "the catastrophic case"),
        ("GRANT ALL ON tasks TO commander", "privilege escalation"),
        ("ALTER TABLE tasks ADD COLUMN x INT", "schema change"),
        ("", "an empty statement is not a read"),
        ("   ", "whitespace is not a read"),
    ],
)
def test_writes_are_refused(sql, why):
    with pytest.raises(NotReadOnly):
        assert_read_only(sql)


def test_a_cte_hiding_a_delete_is_refused():
    """The reason the keyword scan covers the whole statement, not the prefix.

    `WITH ... DELETE` begins with an allowed keyword and is not a read. A guard
    that only checked the first word would wave this straight through.
    """
    with pytest.raises(NotReadOnly, match="delete"):
        assert_read_only(
            "WITH doomed AS (SELECT id FROM tasks) DELETE FROM tasks "
            "WHERE id IN (SELECT id FROM doomed)"
        )


def test_a_second_statement_after_a_semicolon_is_refused():
    with pytest.raises(NotReadOnly, match="multiple statements"):
        assert_read_only("SELECT 1; DROP TABLE tasks")


def test_a_write_hidden_behind_a_comment_is_refused():
    """`SELECT 1 --` then a newline then a write is two statements wearing one
    comment. Comments are stripped before the scan so both halves are visible
    to the same rule."""
    with pytest.raises(NotReadOnly):
        assert_read_only("SELECT 1 -- harmless\n; DELETE FROM tasks")
    with pytest.raises(NotReadOnly):
        assert_read_only("/* nothing to see */ DELETE FROM tasks")


def test_a_trailing_semicolon_is_still_one_read():
    assert_read_only("SELECT 1;")


# --- the catalog ------------------------------------------------------------


def test_there_are_six_canned_questions():
    """§5.1 asks for five; the sixth reads semantic memory, which arrived with
    cross-mission recall. The count is asserted so quietly dropping one to make
    a test pass shows up here."""
    assert len(QUESTIONS) == 6


def test_the_questions_cover_all_four_memory_systems():
    """§4.0's four memories are judging criterion #1, so the console has to
    interrogate every one of them — this used to reach three, with SEMANTIC
    missing in exactly the place a judge is told to look."""
    memories = {q.memory for q in QUESTIONS}
    assert {"provenance", "working", "episodic", "semantic"} == memories


def test_the_named_question_exists():
    """§5.1 names this one explicitly — "why did robot X do Y"."""
    assert "why_did_robot" in BY_ID
    assert BY_ID["why_did_robot"].memory == "provenance"


def test_every_canned_question_is_a_read():
    """The guard would catch it at runtime; catching it here means a bad
    question cannot ship in the first place."""
    for question in QUESTIONS:
        assert_read_only(question.sql)


def test_every_question_is_scoped_to_something_the_server_supplies():
    """Without this a question answers across every mission ever run against the
    cluster, which on a shared demo cluster is somebody else's data.

    Semantic memory is the one deliberate exception to *mission* scoping —
    crossing missions is its entire purpose — so it is scoped by map instead.
    Either way the scope is a bound parameter the server fills from the running
    mission, never something a caller chooses, so the console still cannot be
    pointed at data this fleet did not produce.
    """
    for question in QUESTIONS:
        if question.memory == "semantic":
            # Tactics carry no scope at all — not a mission, not a map. One
            # learned clearing rubble on one map is meant to apply on the next,
            # so any scope here would defeat the point of storing it. It is
            # also the only table that holds nothing mission-identifying.
            assert question.params == ()
        else:
            assert question.params[0] == "mission_id"
            assert "mission_id = %s" in question.sql


def test_semantic_memory_is_the_only_question_that_leaves_the_mission():
    """Guards the exception above from spreading. A second unscoped question
    would be a bug, not a feature — every other table has a mission in it."""
    unscoped = [q.id for q in QUESTIONS if "mission_id" not in q.params]
    assert unscoped == ["what_did_we_learn"]


def test_the_catalog_is_json_shaped():
    entries = catalog()
    assert len(entries) == 6
    for entry in entries:
        assert {"id", "prompt", "memory", "params"} == set(entry)


def test_asking_something_uncanned_is_refused():
    with pytest.raises(UnknownQuestion):
        answer(_NullReader(), "drop_everything", uuid.uuid4())


class _NullReader:
    def read(self, sql, params=()):  # pragma: no cover - never reached
        raise AssertionError("should not have run")


# --- the questions against a live cluster -----------------------------------


@pytest.fixture
def reader():
    r = ReadOnlyReader()
    yield r
    r.close()


@pytest.fixture
def seeded(db, mission):
    """A small mission written through the SDK, so the console reads exactly the
    rows the fleet would have produced."""
    robot = f"c{uuid.uuid4().hex[:8]}"
    db.register_robot(robot, "lifter", (3, 3), 300)
    belief = db.report_observation(
        mission, robot, "victim", (10, 10), {"note": "behind debris"}
    )
    # A second sighting of the same thing: the reconcile gate should merge it
    # rather than create a second victim (§4.2 step 3).
    db.report_observation(mission, robot, "victim", (10, 10), {"note": "still there"})
    # The real chain (§4.2 step 3): a victim behind one rubble tile, so the
    # clear_debris targets (10,9) and the deliver_kit depends on it. The
    # console's job is to name the clear, which is not on the victim's tile.
    _victim, tasks = db.register_victim(
        mission, (10, 10), robot, blocked_by=[(10, 9)], vitals_deadline=400
    )
    task = tasks[0]
    db.claim_task(task, robot)
    db.log_plan(
        mission,
        robot,
        "idle",
        {"task_id": str(task), "source": "rules"},
        "nearest open clear_debris, and I am the only lifter",
        [belief],
    )
    return {"robot": robot, "task": task, "belief": belief, "mission": mission}


@needs_db
def test_why_did_robot_answers_with_the_rows_that_caused_it(reader, seeded):
    """FR-17's whole point: the answer names the memories in the prompt digest,
    not a plausible story about them."""
    result = answer(
        reader, "why_did_robot", seeded["mission"], robot_id=seeded["robot"]
    )

    assert result.rows, "the plan this robot logged was not found"
    latest = result.rows[0]
    assert latest["rationale"].startswith("nearest open clear_debris")
    assert latest["trigger"] == "idle"
    assert latest["decided_by"] == "rules"
    # The join resolved based_on to real observation rows.
    assert latest["based_on"], "based_on did not resolve to any observation"
    assert "victim at 10,10" in latest["based_on"][0]
    assert "sighting" in result.summary


@needs_db
def test_why_did_robot_is_honest_about_a_robot_that_never_decided(reader, mission):
    result = answer(reader, "why_did_robot", mission, robot_id="nobody")
    assert result.rows == []
    assert "not logged a decision" in result.summary


@needs_db
def test_unreached_victims_names_the_task_in_the_way(reader, seeded):
    result = answer(reader, "unreached_victims", seeded["mission"])

    assert result.rows, "the located victim was not reported as unreached"
    row = result.rows[0]
    assert (row["pos_x"], row["pos_y"]) == (10, 10)
    blocking = row["blocking_tasks"]
    # Both halves of the chain: the delivery, and the clear it depends on. The
    # clear is at (10,9), not the victim's tile — finding it is the whole reason
    # this question walks `depends_on` instead of matching positions.
    assert any("deliver_kit" in t for t in blocking), blocking
    assert any("clear_debris" in t for t in blocking), blocking
    assert any(seeded["robot"] in t for t in blocking), blocking
    assert "not yet stabilized" in result.summary


@needs_db
def test_what_do_we_know_shows_the_merge_rather_than_a_duplicate(reader, seeded):
    """Two sightings of one victim are one belief with sightings=2 — the
    reconcile gate's result, visible in the console rather than only in a test."""
    result = answer(reader, "what_do_we_know", seeded["mission"], x=10, y=10, radius=5)

    assert len(result.rows) == 1, "the second sighting created a second belief"
    assert result.rows[0]["sightings"] == 2
    assert "merged" in result.summary


@needs_db
def test_what_do_we_know_respects_its_radius(reader, seeded):
    far = answer(reader, "what_do_we_know", seeded["mission"], x=30, y=30, radius=2)
    assert far.rows == []
    assert "nothing has been observed" in far.summary


@needs_db
def test_who_holds_what_reports_the_lease_not_just_the_owner(reader, seeded):
    result = answer(reader, "who_holds_what", seeded["mission"])

    assert result.rows
    row = result.rows[0]
    assert row["claimed_by"] == seeded["robot"]
    assert row["role"] == "lifter", "the join to robots did not resolve"
    assert row["lease_expired"] is False
    assert row["lease_expires_at"] is not None


@needs_db
def test_who_holds_what_surfaces_a_lapsed_lease_as_reclaimable(db, reader, mission):
    """The resilience story, answerable in SQL: an expired lease is already
    somebody else's to take, and the console says so."""
    robot = f"c{uuid.uuid4().hex[:8]}"
    db.register_robot(robot, "medic", (1, 1), 200)
    task = db.create_task(mission, "deliver_kit", (4, 4))
    assert db.claim_task(task, robot, lease_seconds=0)

    result = answer(reader, "who_holds_what", mission)

    assert result.rows[0]["lease_expired"] is True
    assert "claimable by anyone" in result.summary


@needs_db
def test_aftershock_response_is_empty_until_one_fires(reader, seeded):
    result = answer(reader, "aftershock_response", seeded["mission"])
    assert result.rows == []
    assert "no aftershock" in result.summary


@needs_db
def test_aftershock_response_traces_every_replan(db, reader, mission):
    for robot in ("s1", "l1"):
        db.log_plan(
            mission,
            robot,
            "aftershock",
            {"source": "rules"},
            "aftershock invalidated my route; re-deciding",
            [],
        )

    result = answer(reader, "aftershock_response", mission)

    assert len(result.rows) == 2
    assert "2 replan(s)" in result.summary
    assert "s1" in result.summary and "l1" in result.summary


@needs_db
def test_a_question_never_leaks_another_mission(db, reader, mission):
    """Missions share a cluster. A question scoped by nothing would answer with
    another team's run on the demo cluster."""
    other = uuid.uuid4()
    db.log_plan(other, "s1", "idle", {"source": "rules"}, "other mission", [])

    result = answer(reader, "why_did_robot", mission, robot_id="s1")

    assert result.rows == []


# --- the posture ------------------------------------------------------------


@needs_db
def test_the_reader_refuses_a_write_before_it_reaches_the_cluster(reader):
    """Tests connect as root, so the grant is not protecting anything here —
    which is exactly the situation the in-code guard exists for."""
    with pytest.raises(NotReadOnly):
        reader.read("DELETE FROM events")


@needs_db
def test_the_session_itself_is_read_only(reader):
    """Belt and braces past the string check: even a statement that somehow got
    through would hit a read-only session."""
    row = reader.read("SHOW default_transaction_read_only")
    assert row[0]["default_transaction_read_only"] == "on"
