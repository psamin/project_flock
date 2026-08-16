"""The console over HTTP — what the browser and a judge actually touch (FR-10).

`test_console.py` covers the questions and the guard. This covers the seam: the
catalog is reachable, an answer carries its own SQL so the claim "this came out
of fleet memory" is checkable rather than asserted, and asking for something
outside the canned set is refused rather than executed.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import needs_db


def _fresh_server():
    """A reloaded server module, so each test gets its own mission.

    Deliberately **not** wrapped in `with TestClient(...)`. Entering the client
    as a context manager runs the app's lifespan, which starts the 4 Hz tick
    loop on the client's own thread — and a test that then calls `tick_once()`
    itself is two threads driving one mission. That produced an intermittent
    `AttributeError: 'NoneType' has no attribute 'id'` inside `Worker._complete`
    as `self.task` was cleared underneath the other caller. Without the lifespan
    the routes still work and the mission only advances when a test says so.
    """
    import importlib

    from sim import server as server_module

    importlib.reload(server_module)
    return TestClient(server_module.app), server_module


@pytest.fixture
def client(monkeypatch):
    """A server on the fake, so these tests never need a cluster."""
    monkeypatch.setenv("COLONY_MEMORY", "fake")
    return _fresh_server()


@pytest.fixture
def db_client(monkeypatch):
    """A server on CockroachDB — the console's real configuration."""
    monkeypatch.delenv("COLONY_MEMORY", raising=False)
    c, server_module = _fresh_server()
    if server_module.mission.memory_kind != "cockroach":
        pytest.skip("no CockroachDB (make dev)")
    return c, server_module


# --- the catalog ------------------------------------------------------------


def test_the_catalog_lists_the_six_questions(client):
    c, _ = client
    body = c.get("/api/console/questions").json()

    assert len(body["questions"]) == 6
    assert {q["id"] for q in body["questions"]} >= {
        "why_did_robot",
        "unreached_victims",
        "who_holds_what",
        "what_did_we_learn",
    }


def test_the_catalog_says_which_memory_each_question_reads(client):
    """§4.0's four memories are judging criterion #1, so the console labels
    which one it is interrogating rather than leaving it to the writeup."""
    c, _ = client
    body = c.get("/api/console/questions").json()
    assert {q["memory"] for q in body["questions"]} >= {
        "provenance",
        "working",
        "episodic",
        "semantic",
    }


# --- the honest failure -----------------------------------------------------


def test_on_fake_memory_the_console_says_so_instead_of_answering_nothing(client):
    """An empty answer would read as "the fleet never did that". The difference
    between no data and no database has to be visible."""
    c, _ = client
    body = c.get("/api/console/questions").json()
    assert body["available"] is False

    answer = c.post("/api/console/ask", json={"question": "who_holds_what"}).json()
    assert "error" in answer
    assert "fake" in answer["error"]
    assert "rows" not in answer


def test_an_uncanned_question_is_refused(db_client):
    c, _ = db_client
    body = c.post("/api/console/ask", json={"question": "DROP TABLE tasks"}).json()
    assert "error" in body
    assert "canned" in body["error"]


def test_a_missing_question_is_refused(db_client):
    c, _ = db_client
    assert "error" in c.post("/api/console/ask", json={}).json()


# --- answering against a live cluster ---------------------------------------


@needs_db
def test_an_answer_carries_the_sql_that_produced_it(db_client):
    """FR-10 claims the console reads live fleet memory. Returning the query
    next to the rows is what makes that checkable instead of asserted."""
    c, _ = db_client
    body = c.post("/api/console/ask", json={"question": "who_holds_what"}).json()

    assert "error" not in body, body
    assert "SELECT" in body["sql"]
    assert "mission_id = %s" in body["sql"]
    assert body["memory"] == "working"
    assert isinstance(body["rows"], list)


@needs_db
def test_the_console_answers_about_the_running_mission(db_client):
    """Scoped to this mission, not to whatever else is on the cluster."""
    c, server = db_client
    for _ in range(12):
        server.mission.tick_once()

    body = c.post("/api/console/ask", json={"question": "unreached_victims"}).json()
    assert "error" not in body, body
    assert body["question"] == "unreached_victims"
    assert isinstance(body["summary"], str) and body["summary"]


@needs_db
def test_why_did_robot_reaches_a_real_robots_reasoning(db_client):
    """The question §5.1 names, asked the way the demo asks it.

    Of a scout, deliberately. A lifter logs nothing until a scout has found
    somebody to dig out — first plan around tick 40 on this map — so asking the
    alphabetically-first robot tested that a lifter is idle, not that provenance
    works.
    """
    c, server = db_client
    for _ in range(20):
        server.mission.tick_once()
    robot = next(
        r.id for r in server.mission.world.robots.values() if r.role == "scout"
    )

    body = c.post(
        "/api/console/ask", json={"question": "why_did_robot", "robot_id": robot}
    ).json()

    assert "error" not in body, body
    assert body["memory"] == "provenance"
    assert body["rows"], "a robot 20 ticks in had logged no plan"
    assert body["rows"][0]["rationale"]


@needs_db
def test_the_answer_survives_json(db_client):
    """Rows carry timestamps and UUIDs. Either one uncoerced takes the whole
    response down, not just its own field."""
    c, server = db_client
    for _ in range(12):
        server.mission.tick_once()

    for question in ("who_holds_what", "unreached_victims", "aftershock_response"):
        response = c.post("/api/console/ask", json={"question": question})
        assert response.status_code == 200, question
        response.json()  # raises if the body is not JSON


@needs_db
def test_the_console_cannot_be_talked_into_writing(db_client):
    """Parameters are bound, not interpolated — so a caller cannot smuggle SQL
    through one. The row count is the proof: the table is still there."""
    c, server = db_client
    hostile = "s1'; DROP TABLE events; --"

    body = c.post(
        "/api/console/ask", json={"question": "why_did_robot", "robot_id": hostile}
    ).json()

    assert "error" not in body, body
    assert body["rows"] == []  # no such robot, and nothing executed
    # The table it tried to drop still answers.
    assert server.mission.mem.events(server.mission.mission_id) is not None


@needs_db
def test_a_hostile_limit_cannot_change_the_statement(db_client):
    c, _ = db_client
    body = c.post(
        "/api/console/ask",
        json={
            "question": "why_did_robot",
            "robot_id": "s1",
            "limit": "1; DROP TABLE plans",
        },
    ).json()
    # Bound as a value, so the cluster rejects the *argument* rather than
    # running a rewritten statement — and the console reports that as a bad
    # question rather than letting a traceback out.
    assert "error" in body
    assert "refused that" in body["error"]


@needs_db
def test_an_unknown_mission_answers_empty_rather_than_erroring(db_client):
    """A judge clicking through a restarted mission should get "nothing yet",
    not a stack trace."""
    c, server = db_client
    server.mission.mission_id = uuid.uuid4()

    body = c.post("/api/console/ask", json={"question": "who_holds_what"}).json()
    assert "error" not in body, body
    assert body["rows"] == []
    assert "nothing is claimed" in body["summary"]
