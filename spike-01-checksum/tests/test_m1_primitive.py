"""M1 exit tests: the hash primitive on t_smoke (int8 + text, no canonicalization yet)."""

from harness.checksum import range_checksum
from harness.seed import SMOKE_ROWS, smoke_rows

LO, HI = 0, SMOKE_ROWS


def test_checksums_equal_across_engines(smoke):
    pg = range_checksum(smoke["pg"], "t_smoke", "id", LO, HI)
    crdb = range_checksum(smoke["crdb"], "t_smoke", "id", LO, HI)
    assert pg == crdb, f"pg={pg} crdb={crdb}"
    assert pg[1] == SMOKE_ROWS


def test_subranges_equal_across_engines(smoke):
    for lo, hi in [(0, 1), (0, 250), (250, 500), (999, 1000)]:
        pg = range_checksum(smoke["pg"], "t_smoke", "id", lo, hi)
        crdb = range_checksum(smoke["crdb"], "t_smoke", "id", lo, hi)
        assert pg == crdb, f"range [{lo},{hi}): pg={pg} crdb={crdb}"


def test_insertion_order_does_not_matter(conns):
    """Same rows, reversed insertion order, same table, same engine -> same checksum."""
    rows = smoke_rows()
    checksums = []
    for order in (rows, list(reversed(rows))):
        conn = conns["pg"]
        conn.execute("DROP TABLE IF EXISTS t_order")
        conn.execute("CREATE TABLE t_order (id int8 PRIMARY KEY, v text)")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO t_order (id, v) VALUES (%s, %s)", order)
        checksums.append(range_checksum(conn, "t_order", "id", LO, HI))
    assert checksums[0] == checksums[1], checksums


def test_null_is_distinct_from_the_literal_string(conns):
    """A NULL and the text 'NULL_' must not hash the same — the whole point of
    the 5-char sentinel."""
    for engine, conn in conns.items():
        for table, value in [("t_null_a", None), ("t_null_b", "NULL_")]:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(f"CREATE TABLE {table} (id int8 PRIMARY KEY, v text)")
            conn.execute(f"INSERT INTO {table} (id, v) VALUES (1, %s)", (value,))
        a = range_checksum(conn, "t_null_a", "id", 0, 2)
        b = range_checksum(conn, "t_null_b", "id", 0, 2)
        assert a != b, f"{engine}: NULL and 'NULL_' collided: {a}"


def test_empty_range_convention(smoke):
    for engine, conn in smoke.items():
        assert range_checksum(conn, "t_smoke", "id", 10_000, 20_000) == (None, 0), engine
