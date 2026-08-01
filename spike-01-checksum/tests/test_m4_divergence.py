"""M4 exit tests: divergence detection and recursion, proven on 100k rows.

The mutation is the PRD's chosen demo quirk — a timestamptz whose sub-second
precision gets truncated on the target, exactly the kind of silent difference a
row-count check would sail straight past.
"""

import pytest

from harness.checksum import range_checksum, split_range
from harness.diverge import corner_divergence

ROWS = 100_000
MUTATED_ID = 61_803


@pytest.fixture(scope="module")
def big(conns):
    """100k rows seeded identically on both engines."""
    for conn in conns.values():
        conn.execute("DROP TABLE IF EXISTS t_big")
        conn.execute("CREATE TABLE t_big (id int8 PRIMARY KEY, ts timestamptz, v text)")
        conn.execute(
            "INSERT INTO t_big (id, ts, v)"
            " SELECT g,"
            "        timestamptz '2026-01-01 00:00:00+00' + (g % 1000) * interval '1 second'"
            "            + (g % 999) * interval '1 microsecond',"
            "        'row-' || g::text"
            f"   FROM generate_series(0, {ROWS - 1}) AS g"
        )
    return conns


def test_identical_data_agrees(big):
    assert range_checksum(big["pg"], "t_big", "id", 0, ROWS) == range_checksum(
        big["crdb"], "t_big", "id", 0, ROWS
    )


@pytest.fixture
def mutated(big):
    """Truncate one row's sub-second precision on the target, then restore."""
    target = big["crdb"]
    original = target.execute(
        "SELECT ts FROM t_big WHERE id = %s", (MUTATED_ID,)
    ).fetchone()[0]
    target.execute("UPDATE t_big SET ts = date_trunc('second', ts) WHERE id = %s", (MUTATED_ID,))
    yield big
    target.execute("UPDATE t_big SET ts = %s WHERE id = %s", (original, MUTATED_ID))


def test_one_truncated_timestamp_breaks_the_full_range_checksum(mutated):
    pg = range_checksum(mutated["pg"], "t_big", "id", 0, ROWS)
    crdb = range_checksum(mutated["crdb"], "t_big", "id", 0, ROWS)
    assert pg != crdb, "a truncated sub-second timestamp went undetected"
    assert pg[1] == crdb[1] == ROWS, "row counts still match — only the data changed"


def test_recursion_corners_the_mutated_row(mutated):
    result = corner_divergence(mutated["pg"], mutated["crdb"], "t_big", "id", 0, ROWS)

    assert len(result.leaves) == 1, f"expected one diverging leaf, got {result.leaves}"
    lo, hi = result.leaves[0]
    assert lo <= MUTATED_ID < hi, f"mutated row {MUTATED_ID} not inside leaf [{lo},{hi})"
    assert hi - lo <= 16, f"leaf range [{lo},{hi}) is wider than the 16-row target"


def test_recursion_cost_is_logarithmic_not_linear(mutated):
    """The whole point: cornering one bad row in 100k must cost O(fanout x depth)
    checks, not a scan."""
    result = corner_divergence(mutated["pg"], mutated["crdb"], "t_big", "id", 0, ROWS)

    assert result.max_depth <= 6, result.max_depth
    assert result.checks <= 8 * result.max_depth + 8, (
        f"{result.checks} checks at depth {result.max_depth} — worse than O(fanout x depth)"
    )
    assert result.checks < ROWS / 1000, f"{result.checks} checks is not cheap enough"
    print(f"\n  cornered 1 row in {ROWS:,} with {result.checks} checksum pairs, "
          f"depth {result.max_depth}, leaf {result.leaves[0]}")


def test_clean_data_costs_exactly_one_check(big):
    """When nothing diverged, the recursion must stop at the root — no descent."""
    result = corner_divergence(big["pg"], big["crdb"], "t_big", "id", 0, ROWS)
    assert result.checks == 1, result.checks
    assert result.leaves == []


# --- split_range, on its own ---


@pytest.mark.parametrize(
    "lo,hi,fanout",
    [(0, 100, 8), (0, 1, 8), (0, 3, 8), (0, 8, 8), (5, 17, 4), (0, 100_000, 8)],
)
def test_split_range_is_a_partition(lo, hi, fanout):
    parts = split_range(lo, hi, fanout)
    assert len(parts) <= fanout
    assert all(a < b for a, b in parts), parts
    assert parts[0][0] == lo and parts[-1][1] == hi
    assert all(parts[i][1] == parts[i + 1][0] for i in range(len(parts) - 1)), parts
    assert sum(b - a for a, b in parts) == hi - lo


def test_split_range_handles_empty():
    assert split_range(5, 5) == []
