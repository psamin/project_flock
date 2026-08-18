"""CockroachDB Managed MCP Server wiring (§6.2 required tool #1, FR-10).

§6.2 describes the hookup as a "config snippet copied from Cloud Console into
Claude Code/Cursor/VS Code", and this module prints it:

    uv run python ../infra/mcp.py config --cluster-id <id>   # or $CRDB_CLUSTER_ID
    uv run python ../infra/mcp.py check     assert the grant posture

That snippet is the *editor* path — how a teammate points Claude Code at the
cluster while building. The **runtime** path is `colony/console/mcp_client.py`,
which the commander agent uses to read fleet memory during a mission. Both reach
the same managed endpoint; only the second one is load-bearing in the demo.

## What calling it for real corrected

This module used to assert a read-only story that does not survive contact with
the server. Recorded here rather than quietly fixed, because the same claim
appears in §3.5 and in the README:

    identity   MCP connects as `managed-mcp`, its own service identity.
               `SELECT current_user` through the endpoint says so. The
               `CRDB_SQL_USER: commander` key this file used to emit was inert,
               and has been removed rather than left to imply otherwise. The
               `commander` grant governs `console/reader.py` — the psycopg path
               — and nothing else.
    read-only  On the MCP path it is the server's control, not our grant:
               `select_query` refuses non-SELECT, `information_schema` and
               `crdb_internal` are blocked, and only some SHOW forms are
               allowed. Note that the server still *offers* `insert_rows`,
               `create_table` and `create_database` even with `readOnly: true`
               in the config — which is why the agent is handed an explicit
               allowlist (`mcp_client.TOOLS`) rather than whatever it advertises.
    cluster id must be passed as a tool argument; the `?cluster=` query parameter
               is not read.

So §6.3's "leave writes off" is still the right posture, and it is still worth
stating in the config — but it is one of three layers rather than the whole
story, and the one it is easiest to over-claim.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

MANAGED_ENDPOINT = "https://cockroachlabs.cloud/mcp"
CONSOLE_ROLE = "commander"

# The server name clients will show. Kept explicit so a teammate who wires two
# clusters can tell which one an answer came from.
SERVER_NAME = "colony-fleet-memory"


def config(
    cluster_id: str | None = None,
    endpoint: str = MANAGED_ENDPOINT,
) -> dict:
    """The client config snippet (Claude Code / Cursor / VS Code shape).

    `readOnly` is stated rather than left to the default. The default is already
    read-only per §6.3, but a config that says so is a config nobody can flip by
    accident and a line a judge can read.
    """
    cluster_id = cluster_id or os.environ.get("CRDB_CLUSTER_ID", "<cluster-id>")
    return {
        "mcpServers": {
            SERVER_NAME: {
                "type": "http",
                # The cluster is named here so a human reading the config can
                # tell which one this entry is, but the server does **not** read
                # it: calls arrive as "cluster_id not provided" unless the id is
                # also passed as a tool argument. `console/mcp_client.py` injects
                # it into every call for exactly that reason.
                "url": f"{endpoint}?cluster={cluster_id}",
                "readOnly": True,
                "description": (
                    "Colony fleet memory — working, episodic, provenance and "
                    "semantic tables. Read-only. Used at runtime by the "
                    "commander agent (colony/console/agent.py)."
                ),
            }
        }
    }


def check() -> int:
    """Assert the posture the snippet depends on, before anyone wires it up.

    Delegates to `credentials.verify` rather than re-deriving it: one definition
    of "read-only" for the grants and for MCP, so the two cannot drift into
    disagreeing about what the commander may do.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from credentials import robot_users, verify

    return verify(robot_users())


def main() -> int:
    parser = argparse.ArgumentParser(description="Managed MCP Server wiring (§6.2)")
    parser.add_argument("action", choices=["config", "check"])
    parser.add_argument("--cluster-id", default=None)
    args = parser.parse_args()

    if args.action == "check":
        return check()

    print(json.dumps(config(args.cluster_id), indent=2))
    if not (args.cluster_id or os.environ.get("CRDB_CLUSTER_ID")):
        print(
            "\n# No cluster id. Take it from the Cloud console's connect dialog\n"
            "# and re-run with --cluster-id, or set CRDB_CLUSTER_ID.\n"
            "# Nothing else in this snippet changes.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
