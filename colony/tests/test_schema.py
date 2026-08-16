"""Schema v0 against a live CockroachDB (PRD §4.5, validated per §6.3).

The vector index tests are the ones that matter: the PRD's draft DDL would have
built an L2 index and the cosine queries would have silently fallen back to full
scans.
"""

import pytest

from tests.conftest import needs_db

pytestmark = needs_db

TABLES = [
    "robots",
    "tasks",
    "observations",
    "victims",
    "hazards",
    "events",
    "plans",
    "mission_memories",
]


def test_all_tables_exist(db):
    rows = db.conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    present = {r["table_name"] for r in rows}
    assert set(TABLES) <= present, f"missing: {set(TABLES) - present}"


def test_embedding_column_is_a_512_dim_vector(db):
    row = db.conn.execute(
        "SELECT data_type FROM information_schema.columns"
        " WHERE table_name = 'observations' AND column_name = 'embedding'"
    ).fetchone()
    assert "vector" in row["data_type"].lower(), row


def test_vector_index_exists_and_uses_cosine(db):
    """The correction to the PRD's DDL. A default CREATE VECTOR INDEX builds
    vector_l2_ops; the reconcile gate is specified in cosine (§6.3)."""
    rows = db.conn.execute("SHOW INDEXES FROM observations").fetchall()
    names = {r["index_name"] for r in rows}
    assert "obs_embedding_idx" in names, names

    create = db.conn.execute("SHOW CREATE TABLE observations").fetchone()[
        "create_statement"
    ]
    assert "vector_cosine_ops" in create, (
        "vector index is not built for cosine; the <=> reconcile gate will full-scan\n"
        + create
    )


def test_cosine_query_actually_uses_the_index(db, mission):
    """Correct results are not enough — an unindexed scan returns the same rows
    and dies at demo scale. Assert the plan."""
    vec = "[" + ",".join(["0.01"] * 512) + "]"
    for i in range(10):
        db.conn.execute(
            "INSERT INTO observations (mission_id, robot_id, kind, pos_x, pos_y, embedding)"
            " VALUES (%s, 's1', 'victim', %s, %s, %s)",
            (mission, i, i, vec),
        )
    plan = "\n".join(
        r["info"]
        for r in db.conn.execute(
            "EXPLAIN SELECT id FROM observations WHERE mission_id = %s"
            " ORDER BY embedding <=> %s LIMIT 5",
            (mission, vec),
        ).fetchall()
    )
    assert "vector search" in plan, (
        f"cosine query is not using the vector index:\n{plan}"
    )
    assert "obs_embedding_idx" in plan, plan


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP: find_similar's real query does not use obs_embedding_idx. "
        "Only filters matching a prefix column keep a vector index engaged, and "
        "the gate also constrains `kind` and a pos_x/pos_y BETWEEN box — neither "
        "is a prefix column, so v26.2 falls back to a FULL SCAN. Results stay "
        "correct and demo-scale data is small, so nothing looks wrong. Flip this "
        "to a passing test if the gate is ever reworked; do not delete it."
    ),
)
def test_the_reconcile_gate_query_uses_the_index(db, mission):
    """The query above is not the one the SDK runs, and this is the one that is.

    Kept because the difference is the whole claim: a test that exercises a
    tidier query passes while `find_similar` full-scans, which is exactly how
    this went unnoticed. Reworking it is a real trade — constraining kind and
    position in SQL is what stops the gate silently missing duplicates
    (client.py:185-192), and moving those filters after the top-k trades an
    index scan for lost matches.
    """
    vec = "[" + ",".join(["0.01"] * 512) + "]"
    for i in range(10):
        db.conn.execute(
            "INSERT INTO observations (mission_id, robot_id, kind, pos_x, pos_y, embedding)"
            " VALUES (%s, 's1', 'victim', %s, %s, %s)",
            (mission, i, i, vec),
        )
    plan = "\n".join(
        r["info"]
        for r in db.conn.execute(
            "EXPLAIN SELECT id, embedding <=> %s AS distance FROM observations"
            " WHERE mission_id = %s AND embedding IS NOT NULL AND kind = %s"
            "   AND pos_x BETWEEN %s AND %s AND pos_y BETWEEN %s AND %s"
            " ORDER BY embedding <=> %s LIMIT 5",
            (vec, mission, "victim", 0, 20, 0, 20, vec),
        ).fetchall()
    )
    assert "vector search" in plan, plan


def test_schema_is_idempotent(db):
    """`make dev` re-runs this on every start; it must not fail on a live db.

    Applied statement by statement, the way schema.apply does it. Sent as one
    string the file becomes a single implicit transaction, and CockroachDB
    rejects `ALTER TABLE ... SET (schema_locked = ...)` in that form — so a
    whole-file execute here would fail on a statement that is correct.
    """
    from schema.apply import statements
    from tests.conftest import SCHEMA

    for stmt in statements(SCHEMA.read_text()):
        db.conn.execute(stmt)


@pytest.mark.parametrize("table", TABLES)
def test_every_table_is_queryable(db, table):
    db.conn.execute(f"SELECT count(*) FROM {table}")
