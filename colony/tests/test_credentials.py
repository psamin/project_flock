"""Per-robot credentials and the read-only commander (PRD §3.5, §6.2, §6.3).

§3.5 lists this posture as judge-visible, and §6.2 makes "the MCP console is
read-only" the access-control story the writeup tells. A story is only worth
telling if the grants actually enforce it, so these assert the grant set rather
than the intention.

The structural tests run anywhere. The live ones connect as the roles and try
the things that must be refused, and skip when no cluster is up.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CREDENTIALS = ROOT / "infra" / "credentials.py"
COMPOSE = ROOT / "infra" / "docker-compose.3node.yml"
# The console's own identity (§6.2). No password: the 3-node rig runs
# --insecure, which is also why it is a rehearsal rig and not a deployment.
COMMANDER_DSN = "postgresql://commander@localhost:26257/colony?sslmode=disable"


def _load():
    spec = importlib.util.spec_from_file_location("credentials", CREDENTIALS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["credentials"] = module
    spec.loader.exec_module(module)
    return module


creds = _load()


def _as(user: str, statement: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "-p",
            "colony3",
            "exec",
            "-T",
            "crdb-1",
            "./cockroach",
            "sql",
            "--insecure",
            "-d",
            "colony",
            "--user",
            user,
            "-e",
            statement,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _roles_exist() -> bool:
    result = _as("commander", "SELECT 1")
    return result.returncode == 0


needs_roles = pytest.mark.skipif(
    not _roles_exist(),
    reason="no cluster with roles applied (make cluster-3 && credentials.py apply)",
)


# --- the grant set ------------------------------------------------------------


def test_every_robot_in_the_stat_block_gets_its_own_user():
    """§3.3 fields 2 scouts, a lifter and a medic. One shared login would make
    the event log unattributable and "per-robot credentials" a fiction."""
    assert creds.robot_users() == ["s1", "s2", "l1", "m1"]


def test_no_robot_is_ever_granted_delete_or_drop():
    """The mission log is append-only (§4.5) and replay depends on that. A robot
    that can delete can erase the evidence of what it did."""
    statements = creds.grant_statements(creds.robot_users())
    for robot in creds.robot_users():
        robot_grants = [s for s in statements if s.endswith(f"TO {robot}")]
        assert robot_grants, f"{robot} got no grants at all"
        for statement in robot_grants:
            assert "DELETE" not in statement, statement
            assert "ALL" not in statement, statement


def test_a_robot_can_write_its_own_observations():
    """Least privilege must not mean no privilege — a robot that cannot INSERT
    cannot report, and the reconcile gate never runs."""
    statements = creds.grant_statements(["s1"])
    assert any("INSERT" in s and "observations" in s for s in statements)


def test_a_robot_can_append_plans_but_not_read_them():
    """Provenance is written by each robot and read by the commander. A robot
    has no reason to read another robot's reasoning, so it does not get to."""
    statements = creds.grant_statements(["s1"])
    plans = [s for s in statements if ".plans " in s and s.endswith("TO s1")]
    assert plans, "no plans grant at all — log_plan would fail"
    assert all("INSERT" in s and "SELECT" not in s for s in plans), plans


def test_the_commander_gets_select_and_nothing_else():
    """§6.2: the MCP console is read-only. This is the grant that makes it so
    rather than a setting someone could flip."""
    statements = creds.grant_statements([])
    commander = [
        s
        for s in statements
        if s.endswith(f"TO {creds.COMMANDER}") and s.startswith("GRANT")
    ]
    writes = [
        s
        for s in commander
        if any(v in s for v in ("INSERT", "UPDATE", "DELETE", "ALL"))
    ]
    assert not writes, writes


def test_the_commander_can_read_every_table():
    """A console that cannot see `plans` cannot answer "why did L1 stop?", which
    is the provenance question §6.2 sells."""
    statements = creds.grant_statements([])
    readable = {
        s.split("colony.")[1].split()[0]
        for s in statements
        if s.startswith("GRANT SELECT") and s.endswith(f"TO {creds.COMMANDER}")
    }
    assert readable == set(creds.ALL_TABLES)


# --- live: the database actually refuses ---------------------------------------


@needs_roles
def test_the_commander_cannot_write():
    result = _as(
        "commander",
        "INSERT INTO events (mission_id, actor, verb)"
        " VALUES (gen_random_uuid(), 'x', 'y')",
    )
    assert result.returncode != 0, "the read-only console wrote to the mission log"
    assert "42501" in result.stdout + result.stderr


@needs_roles
def test_the_commander_can_read():
    assert _as("commander", "SELECT count(*) FROM tasks").returncode == 0


@needs_roles
def test_a_robot_cannot_delete():
    """WHERE 1=0 so a missing grant is the only thing this can prove — it
    removes nothing even if the privilege were wrongly present."""
    result = _as("s1", "DELETE FROM events WHERE 1=0")
    assert result.returncode != 0, "a robot can delete from the append-only log"


@needs_roles
def test_a_robot_cannot_read_another_robots_reasoning():
    result = _as("s1", "SELECT count(*) FROM plans")
    assert result.returncode != 0, "a robot read the provenance table"


@needs_roles
def test_a_robot_can_still_report():
    result = _as(
        "s1",
        "INSERT INTO observations (mission_id, robot_id, kind)"
        " VALUES (gen_random_uuid(), 's1', 'victim') RETURNING id",
    )
    assert result.returncode == 0, result.stderr


@needs_roles
def test_verify_reports_the_posture_holds():
    """The script teammates run before recording; it must agree with the tests."""
    assert creds.verify(creds.robot_users()) == 0


# --- the console, as the identity it actually claims -------------------------


@needs_roles
def test_the_console_answers_every_question_as_the_read_only_role():
    """FR-10 end to end, through the grant rather than around it.

    Every other console test connects as root, so the `commander` grant is not
    the thing permitting the read — the code is. This runs the canned set as
    `commander`, which is the only way to find out that the console needs a
    table nobody granted it. `plans` is the one that would bite: robots hold
    INSERT-without-SELECT on it, so a role modelled on a robot could write
    provenance and never read it back.
    """
    import uuid

    from console.questions import QUESTIONS, answer
    from console.reader import ReadOnlyReader
    from fleetmem.client import CockroachFleetMem

    mem = CockroachFleetMem()
    mission = uuid.uuid4()
    robot = f"probe{uuid.uuid4().hex[:6]}"
    try:
        mem.register_robot(robot, "lifter", (3, 3), 300)
        belief = mem.report_observation(mission, robot, "victim", (10, 10), {})
        _v, tasks = mem.register_victim(
            mission, (10, 10), robot, blocked_by=[(10, 9)], vitals_deadline=400
        )
        mem.claim_task(tasks[0], robot)
        mem.log_plan(mission, robot, "idle", {"source": "rules"}, "nearest", [belief])
    finally:
        mem.close()

    reader = ReadOnlyReader(dsn=COMMANDER_DSN)
    try:
        for question in QUESTIONS:
            kwargs = {}
            if "robot_id" in question.params:
                kwargs["robot_id"] = robot
            if "x" in question.params:
                kwargs.update(x=10, y=10, radius=5)
            # The assertion is that it does not raise: a missing grant surfaces
            # as SQLSTATE 42501 here, not as an empty result.
            answer(reader, question.id, mission, **kwargs)
    finally:
        reader.close()


@needs_roles
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO events (mission_id, actor, verb) VALUES (gen_random_uuid(), 'x', 'y')",
        "UPDATE tasks SET status = 'open' WHERE 1=0",
        "DELETE FROM observations WHERE 1=0",
    ],
)
def test_the_commander_cannot_write_even_with_the_in_code_guard_bypassed(statement):
    """The guard in `console/reader.py` refuses these before they are sent. This
    sends them anyway, on a raw connection, to check the layer underneath.

    That distinction is the whole point of having two: the guard travels with
    the code and protects a laptop with no roles applied; the grant protects
    the cluster even if the code is wrong. Only this test exercises the second.
    """
    import psycopg

    conn = psycopg.connect(COMMANDER_DSN, autocommit=True)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
    finally:
        conn.close()
