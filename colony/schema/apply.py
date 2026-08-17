"""Apply a schema file over a DSN, without the `cockroach` CLI.

The Makefile's `schema` target runs `./cockroach sql` *inside* the docker
container, so local dev never needs the CLI on the host. Cloud has no container
to shell into, and requiring a 100MB CLI download for one `-f` invocation is a
bad trade when psycopg is already a dependency (§3.2).

    uv run python -m schema.apply "$COLONY_DSN"
    uv run python -m schema.apply "$COLONY_DSN" schema/v1_1.sql

Creates the DSN's database if it is missing — Cloud hands you `defaultdb`, and
CREATE DATABASE cannot run from inside the database it is creating.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg

# `-- comment` to end of line. Stripped before splitting so a semicolon inside a
# comment cannot end a statement.
_COMMENT = re.compile(r"--[^\n]*")


def statements(sql: str) -> list[str]:
    """Split a schema file into individual statements.

    Sent as one string, psycopg batches the file into a single implicit
    transaction — and CockroachDB rejects some statements outright in that form.
    `ALTER TABLE ... SET (schema_locked = ...)`, which Cloud requires around any
    migration, "can only be set/reset on its own without other parameters in a
    single-statement implicit transaction". Applying statement by statement also
    means a failure names the statement that failed instead of the whole file.

    The split is on semicolons after comments are stripped. That is sound for
    this schema and would not be for one containing a semicolon inside a string
    literal — there is none, and a dollar-quoted body would need a real parser.
    """
    return [s.strip() for s in _COMMENT.sub("", sql).split(";") if s.strip()]


def _database_of(dsn: str) -> str:
    return urlparse(dsn).path.lstrip("/") or "defaultdb"


def _rebased_on(dsn: str, database: str) -> str:
    return urlunparse(urlparse(dsn)._replace(path=f"/{database}"))


def ensure_database(dsn: str) -> str:
    """Create the DSN's database if absent. Returns its name."""
    database = _database_of(dsn)
    if database == "defaultdb":
        return database
    with psycopg.connect(_rebased_on(dsn, "defaultdb"), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE IF NOT EXISTS "{database}"')
    return database


def apply(dsn: str, sql_path: Path) -> None:
    database = ensure_database(dsn)
    stmts = statements(sql_path.read_text())
    with psycopg.connect(dsn, autocommit=True) as conn:
        for stmt in stmts:
            try:
                conn.execute(stmt)
            except psycopg.Error as exc:
                # Name the statement. A migration file that fails as one opaque
                # blob is the same as a migration file with no error message.
                head = " ".join(stmt.split())[:120]
                raise RuntimeError(f"failed on: {head}\n  {exc}") from exc
    print(f"applied {sql_path} to {database} ({len(stmts)} statements)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python -m schema.apply <dsn> [sql_file]")
    schema_file = Path(sys.argv[2] if len(sys.argv) > 2 else "schema/v1_1.sql")
    if not schema_file.exists():
        sys.exit(f"no such schema file: {schema_file} (run from the colony/ directory)")
    apply(sys.argv[1], schema_file)
