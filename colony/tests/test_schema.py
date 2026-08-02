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


def test_schema_is_idempotent(db):
    """`make dev` re-runs this on every start; it must not fail on a live db."""
    from tests.conftest import SCHEMA

    db.conn.execute(SCHEMA.read_text())


@pytest.mark.parametrize("table", TABLES)
def test_every_table_is_queryable(db, table):
    db.conn.execute(f"SELECT count(*) FROM {table}")
