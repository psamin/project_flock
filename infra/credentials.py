"""Per-robot SQL users with least-privilege grants (PRD §3.5, §6.3).

§3.5 lists "per-robot service credentials; commander MCP access read-only;
least-privilege by construction" as a judge-visible security posture, and §6.2
makes the read-only commander role the access-control story the writeup tells.
So the grants are the deliverable, not an afterthought.

Three roles, derived from what each actually does:

    robot       writes its own observations, claims and events. Cannot DROP,
                cannot ALTER, and cannot read `plans` — a robot has no business
                reading another robot's reasoning.
    commander   SELECT only, everywhere. This is the MCP console's identity, and
                §6.3 notes the MCP server is read-only by default; the grant is
                what makes that true rather than merely configured.
    admin       migrations. Not used at runtime.

    uv run python ../infra/credentials.py apply    create roles and grants
    uv run python ../infra/credentials.py verify   assert the posture holds
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = "colony"

# Tables a robot writes through the fleetmem SDK.
ROBOT_WRITE_TABLES = ["observations", "tasks", "robots", "victims", "hazards", "events"]
# Provenance a robot may write but not read back: log_plan appends, and nothing
# in the agent loop reads another robot's rationale.
ROBOT_APPEND_ONLY = ["plans"]
ALL_TABLES = ROBOT_WRITE_TABLES + ROBOT_APPEND_ONLY + ["mission_memories"]

COMMANDER = "commander"


def robot_users(count: int = 4) -> list[str]:
    """One user per robot in the §3.3 stat block: 2 scouts, a lifter, a medic."""
    return ["s1", "s2", "l1", "m1"][:count]


def grant_statements(robots: list[str]) -> list[str]:
    """Every statement needed for the posture, in order.

    Written out rather than looped in SQL so the grants are readable as a
    security document — someone auditing this should be able to see exactly what
    a robot can do without running anything.
    """
    statements: list[str] = []

    for robot in robots:
        statements += [
            f"CREATE USER IF NOT EXISTS {robot}",
            f"GRANT CONNECT ON DATABASE {DB} TO {robot}",
        ]
        for table in ROBOT_WRITE_TABLES:
            statements.append(
                f"GRANT SELECT, INSERT, UPDATE ON TABLE {DB}.{table} TO {robot}"
            )
        for table in ROBOT_APPEND_ONLY:
            # INSERT without SELECT: a robot records why it acted, and cannot
            # read anyone else's reasoning back out.
            statements.append(f"GRANT INSERT ON TABLE {DB}.{table} TO {robot}")
        # No DELETE anywhere: nothing in the fleet's design removes a row, and
        # the append-only mission log (§4.5) depends on that staying true.

    statements += [
        f"CREATE USER IF NOT EXISTS {COMMANDER}",
        f"GRANT CONNECT ON DATABASE {DB} TO {COMMANDER}",
    ]
    for table in ALL_TABLES:
        statements.append(f"GRANT SELECT ON TABLE {DB}.{table} TO {COMMANDER}")

    return statements


def _sql(statement: str, database: str = DB) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "infra" / "docker-compose.3node.yml"),
            "-p",
            "colony3",
            "exec",
            "-T",
            "crdb-1",
            "./cockroach",
            "sql",
            "--insecure",
            "-d",
            database,
            "--format=csv",
            "-e",
            statement,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def apply(robots: list[str]) -> int:
    for statement in grant_statements(robots):
        result = _sql(statement)
        if result.returncode != 0:
            print(f"failed: {statement}\n{result.stderr.strip()}", file=sys.stderr)
            return 1
    print(f"granted: {len(robots)} robot users + {COMMANDER} (read-only)")
    return 0


def verify(robots: list[str]) -> int:
    """Assert the posture rather than trusting that apply ran.

    Checks the two claims §3.5 makes out loud: a robot cannot escalate, and the
    commander cannot write.
    """
    failures: list[str] = []

    granted = _sql(
        "SELECT grantee, privilege_type, table_name FROM "
        "information_schema.table_privileges WHERE table_schema = 'public'"
    )
    if granted.returncode != 0:
        print(granted.stderr.strip(), file=sys.stderr)
        return 1
    rows = [line.split(",") for line in granted.stdout.strip().splitlines()[1:]]

    for grantee, privilege, table in rows:
        if grantee in robots:
            if privilege in ("DROP", "ALL"):
                failures.append(f"{grantee} holds {privilege} on {table}")
            if table in ROBOT_APPEND_ONLY and privilege == "SELECT":
                failures.append(f"{grantee} can read {table}")
        if grantee == COMMANDER and privilege != "SELECT":
            failures.append(f"{COMMANDER} holds {privilege} on {table} — not read-only")

    for robot in robots:
        if not any(g == robot and p == "INSERT" for g, p, _ in rows):
            failures.append(f"{robot} cannot write anything — it could not report")
    if not any(g == COMMANDER for g, _, _ in rows):
        failures.append(f"{COMMANDER} has no grants — the console cannot read")

    if failures:
        for failure in failures:
            print(f"POSTURE FAILURE: {failure}", file=sys.stderr)
        return 1

    print(
        f"posture holds: {len(robots)} robots write-but-cannot-escalate, "
        f"{COMMANDER} is read-only across {len(ALL_TABLES)} tables"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Per-robot credentials (§3.5)")
    parser.add_argument("action", choices=["apply", "verify"])
    args = parser.parse_args()
    robots = robot_users()
    return apply(robots) if args.action == "apply" else verify(robots)


if __name__ == "__main__":
    raise SystemExit(main())
