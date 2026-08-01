"""M0 exit test: both engines reachable, seeded identically, count(*) matches."""

from harness.seed import SMOKE_ROWS


def test_utc_pinned(conns):
    for engine, conn in conns.items():
        tz = conn.execute("SHOW TIME ZONE").fetchone()[0]
        assert tz == "UTC", f"{engine} session timezone is {tz!r}, not UTC"


def test_row_counts_match(smoke):
    counts = {
        engine: conn.execute("SELECT count(*) FROM t_smoke").fetchone()[0]
        for engine, conn in smoke.items()
    }
    assert counts["pg"] == counts["crdb"] == SMOKE_ROWS, counts


def test_insertion_orders_actually_differ(smoke):
    """Guard on the guard: if both engines got the same order, M1's
    order-independence test proves nothing."""
    from harness.seed import insertion_order, smoke_rows

    rows = smoke_rows()
    assert insertion_order(rows, "pg") != insertion_order(rows, "crdb")
