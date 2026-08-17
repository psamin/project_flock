"""The read-only execution path (FR-10, §3.5, §6.3).

§3.5 lists "commander MCP access read-only" as a judge-visible posture, and
§6.3 notes the MCP server is read-only by default with write tools opt-in —
"leave writes off, it *is* our access-control story". A story that rests on a
default nobody checked is not much of a story, so the console refuses writes
twice, at two different layers:

    grant   `commander` holds SELECT and nothing else, across all eight tables
            (`infra/credentials.py`). This is the one that actually stops a
            write, and it stops it even if every line below is wrong.
    reader  the statement is checked before it is sent.

The second layer looks redundant next to the first, and is not. The grant lives
on the cluster, so it is absent on a laptop running `make dev`, absent on a
fresh Cloud cluster before anyone runs `credentials.py apply`, and absent in
every test that connects as root. The check below travels with the code. Two
layers also means the demo can honestly say the console cannot write without
that claim depending on which cluster it happens to be pointed at.
"""

from __future__ import annotations

import os
import re
from typing import Any

import psycopg
from fleetmem.client import resolve_dsn
from psycopg.rows import dict_row

# `COLONY_CONSOLE_DSN` is how the console connects as `commander` rather than as
# the application. Separate from `COLONY_DSN` on purpose: the sim writes and the
# console does not, so they are different identities against the same cluster.
CONSOLE_DSN_ENV = "COLONY_CONSOLE_DSN"

# Statements a read-only console may issue. `WITH` is here because two of the
# five questions are CTEs; it is only safe alongside the writing-keyword scan
# below, since `WITH ... AS (...) DELETE` is also a WITH statement.
READ_ONLY_PREFIXES = ("select", "with", "explain", "show")

# Scanned for anywhere in the statement, not just at the front. A CTE hides the
# verb in the middle: `WITH doomed AS (SELECT id FROM tasks) DELETE FROM tasks`
# starts with an allowed prefix and is not a read.
WRITING_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "upsert",
    "truncate",
    "drop",
    "alter",
    "create",
    "grant",
    "revoke",
    "commit",
    "rollback",
    "set",
    "import",
    "restore",
    "backup",
)

_COMMENT = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.DOTALL)
_WORD = re.compile(r"[a-z_]+")


class NotReadOnly(ValueError):
    """A statement the console refuses to send."""


def assert_read_only(sql: str) -> None:
    """Raise unless `sql` is a single read.

    Comments are stripped first: `SELECT 1 --\\nDELETE FROM tasks` is two
    statements wearing one comment, and a keyword scan over the raw text would
    find `delete` inside what it believed was a comment either way. Stripping
    makes both halves visible to the same rule.
    """
    stripped = _COMMENT.sub(" ", sql).strip().rstrip(";").strip()
    if not stripped:
        raise NotReadOnly("empty statement")
    # One statement only. A trailing semicolon is fine; a second statement after
    # it is the oldest trick there is.
    if ";" in stripped:
        raise NotReadOnly("multiple statements; the console sends one read")

    lowered = stripped.lower()
    if not lowered.startswith(READ_ONLY_PREFIXES):
        raise NotReadOnly(
            f"{lowered.split()[0]!r} is not a read; "
            f"the console may only {', '.join(READ_ONLY_PREFIXES).upper()}"
        )
    for word in _WORD.findall(lowered):
        if word in WRITING_KEYWORDS:
            raise NotReadOnly(f"{word!r} writes; the commander console is read-only")


class ReadOnlyReader:
    """A connection that can only read.

    Opened as `commander` when `COLONY_CONSOLE_DSN` says so. Without it the
    console falls back to the application's own connection string, which is what
    makes the demo work on a laptop — and exactly why `assert_read_only` is not
    optional: on that path the grant is not doing anything.
    """

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or resolve_dsn(os.environ.get(CONSOLE_DSN_ENV))
        self.conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        # Belt and braces: even if a statement slipped past the check above, the
        # session itself refuses to write. Left non-fatal because a role without
        # permission to set it is not a reason to lose the console.
        try:
            self.conn.execute("SET default_transaction_read_only = on")
        except psycopg.Error:  # pragma: no cover - cluster-dependent
            pass

    def read(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        assert_read_only(sql)
        return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()
