"""M2 exit test: the golden suite. One table per type, identical rows on both
engines, checksums must match.

This is the PRD's named Phase 0 exit criterion.
"""

import pytest

from harness.checksum import range_checksum
from harness.golden import ROWS_PER_TABLE, SQL_TYPES, table_name


@pytest.mark.parametrize("type_key", sorted(SQL_TYPES))
def test_golden_table_matches_across_engines(golden, type_key):
    table = table_name(type_key)
    pg = range_checksum(golden["pg"], table, "id", 0, ROWS_PER_TABLE)
    crdb = range_checksum(golden["crdb"], table, "id", 0, ROWS_PER_TABLE)
    assert pg == crdb, f"{table}: pg={pg} crdb={crdb}"
    assert pg[1] == ROWS_PER_TABLE


@pytest.mark.parametrize("type_key", sorted(SQL_TYPES))
def test_golden_checksum_is_actually_sensitive(golden, type_key):
    """Guard against a vacuous pass: if the canonical cast flattened every value
    to a constant, the table above would still be green. Deleting one row must
    change the checksum."""
    table = table_name(type_key)
    conn = golden["pg"]
    full = range_checksum(conn, table, "id", 0, ROWS_PER_TABLE)
    partial = range_checksum(conn, table, "id", 0, ROWS_PER_TABLE - 1)
    assert full != partial, f"{table}: checksum is insensitive to row removal"


# --- numeric: the type that actually diverged (see RESULTS.md) ---

OUT_OF_RANGE = [
    "'" + "9" * 25 + "'::numeric",              # 25 int digits: past CRDB's to_char ceiling
    "'0." + "0" * 40 + "1'::numeric",           # 41 frac digits: past the format's scale
]


@pytest.fixture
def oversized(conns):
    """A table holding values outside the canonical range, on both engines."""
    for conn in conns.values():
        conn.execute("DROP TABLE IF EXISTS t_numeric_big")
        conn.execute("CREATE TABLE t_numeric_big (id int8 PRIMARY KEY, v numeric)")
        for i, expr in enumerate(OUT_OF_RANGE):
            conn.execute(f"INSERT INTO t_numeric_big (id, v) VALUES ({i}, {expr})")
    return conns


def test_out_of_range_numeric_does_not_error(oversized):
    """The guard must keep to_char from ever being evaluated on a value CRDB
    cannot render — a raised error would take down the whole range check."""
    for engine, conn in oversized.items():
        checksum, count = range_checksum(conn, "t_numeric_big", "id", 0, 10)
        assert count == len(OUT_OF_RANGE), engine
        assert checksum is not None, engine


def test_out_of_range_numerics_never_silently_collide(oversized):
    """The false-pass guard. to_char alone renders 0.000…1 and 0.000…2 both as
    '0.'; two different values that checksum equal would let a corrupted
    migration pass verification. They must stay distinguishable."""
    for engine, conn in oversized.items():
        conn.execute("DROP TABLE IF EXISTS t_collide_a")
        conn.execute("DROP TABLE IF EXISTS t_collide_b")
        for table, tail in [("t_collide_a", "1"), ("t_collide_b", "2")]:
            conn.execute(f"CREATE TABLE {table} (id int8 PRIMARY KEY, v numeric)")
            conn.execute(
                f"INSERT INTO {table} (id, v) VALUES (0, '0.{'0' * 40}{tail}'::numeric),"
                f" (1, '{'9' * 24}{tail}'::numeric)"
            )
        a = range_checksum(conn, "t_collide_a", "id", 0, 10)
        b = range_checksum(conn, "t_collide_b", "id", 0, 10)
        assert a != b, f"{engine}: distinct out-of-range numerics collided: {a}"


def test_scale_is_normalized_not_compared(golden):
    """1.50 and 1.5 are the same number; a migration that changes only display
    scale must not be reported as data corruption."""
    for engine, conn in golden.items():
        conn.execute("DROP TABLE IF EXISTS t_scale_a")
        conn.execute("DROP TABLE IF EXISTS t_scale_b")
        for table, value in [("t_scale_a", "1.50"), ("t_scale_b", "1.5")]:
            conn.execute(f"CREATE TABLE {table} (id int8 PRIMARY KEY, v numeric)")
            conn.execute(f"INSERT INTO {table} (id, v) VALUES (0, {value})")
        assert range_checksum(conn, "t_scale_a", "id", 0, 10) == range_checksum(
            conn, "t_scale_b", "id", 0, 10
        ), engine


# --- session settings the recipe depends on (hard rule 5) ---


def test_utc_pin_is_load_bearing_for_timestamptz(golden):
    """timestamptz canonicalizes via ::text, which renders in the session zone.
    Proving the pin matters is what makes it a documented dependency rather than
    an incidental default."""
    conn = golden["pg"]
    utc = range_checksum(conn, table_name("timestamptz"), "id", 0, ROWS_PER_TABLE)
    conn.execute("SET TIME ZONE 'America/New_York'")
    try:
        shifted = range_checksum(conn, table_name("timestamptz"), "id", 0, ROWS_PER_TABLE)
    finally:
        conn.execute("SET TIME ZONE 'UTC'")
    assert utc != shifted, "timestamptz checksum ignored the session zone"
