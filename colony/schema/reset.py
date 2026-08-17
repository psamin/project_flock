"""Empty the mission tables, so the next run starts from a known state.

Every mission leaves rows behind — tasks, observations, events, plans, and a
row in `mission_memories` if it finished. That is the point of the schema, and
it also means a cluster that has been demoed against a dozen times answers the
commander console with a dozen missions' worth of history.

    uv run python -m schema.reset                  # everything, with a prompt
    uv run python -m schema.reset --keep-memories  # forget the runs, keep the lessons
    uv run python -m schema.reset --yes            # no prompt, for make targets

`--keep-memories` is the interesting one. Wiping the working and episodic
tables while keeping `mission_memories` is exactly the state the demo wants: no
stale missions cluttering the console, but the fleet still remembers the map.

This is destructive and the cluster is shared, so it prints what it is about to
delete and asks first unless told not to.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg

from fleetmem.client import resolve_dsn

# Working, episodic and provenance memory — everything scoped to a mission.
# Order does not matter: the schema has no foreign keys, dependencies between
# tasks ride in a UUID[] rather than a constraint.
MISSION_TABLES = [
    "observations",
    "plans",
    "events",
    "tasks",
    "victims",
    "hazards",
    "robots",
]
# Semantic memory: what survives a mission on purpose.
MEMORY_TABLE = "mission_memories"


def counts(conn, tables: list[str]) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def reset(dsn: str, keep_memories: bool = False, assume_yes: bool = False) -> int:
    tables = list(MISSION_TABLES) + ([] if keep_memories else [MEMORY_TABLE])
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        before = counts(conn, tables + [MEMORY_TABLE])
        total = sum(before[t] for t in tables)
        host = conn.info.host

        print(f"cluster: {host}")
        for t in tables:
            print(f"  {before[t]:>7}  {t}")
        if keep_memories:
            print(f"  {before[MEMORY_TABLE]:>7}  {MEMORY_TABLE}  (keeping)")

        if total == 0:
            print("\nalready empty; nothing to do")
            return 0

        if not assume_yes:
            print(f"\nThis deletes {total} rows from {host}.")
            if input("Type 'yes' to continue: ").strip().lower() != "yes":
                print("aborted")
                return 1

        for t in tables:
            conn.execute(f"DELETE FROM {t}")
        after = counts(conn, [MEMORY_TABLE])
        print(f"\ndeleted {total} rows; {after[MEMORY_TABLE]} mission memories remain")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-memories",
        action="store_true",
        help="empty the mission tables but keep semantic memory",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = parser.parse_args()

    dsn = resolve_dsn(os.environ.get("COLONY_DSN"))
    return reset(dsn, keep_memories=args.keep_memories, assume_yes=args.yes)


if __name__ == "__main__":
    sys.exit(main())
