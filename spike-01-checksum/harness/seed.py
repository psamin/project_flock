"""Deterministic data generator. Seeds BOTH engines with identical rows, in different orders.

Different insertion order per engine is the built-in order-independence check: any
checksum that depends on scan order will fail loudly instead of passing by luck.
"""

import random

from harness.db import ENGINES, connect_all

SMOKE_ROWS = 1000
RNG_SEED = 20260801


def smoke_rows() -> list[tuple[int, str | None]]:
    """1,000 deterministic (id, v) rows. Every 97th value is NULL."""
    rng = random.Random(RNG_SEED)
    rows = []
    for i in range(SMOKE_ROWS):
        v = None if i % 97 == 0 else f"row-{i}-{rng.randrange(10**9)}"
        rows.append((i, v))
    return rows


def insertion_order(rows: list, engine: str) -> list:
    """Same rows, engine-specific order."""
    shuffled = list(rows)
    random.Random(RNG_SEED + ENGINES.index(engine)).shuffle(shuffled)
    return shuffled


def seed_smoke(conn, engine: str) -> None:
    conn.execute("DROP TABLE IF EXISTS t_smoke")
    conn.execute("CREATE TABLE t_smoke (id int8 PRIMARY KEY, v text)")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO t_smoke (id, v) VALUES (%s, %s)",
            insertion_order(smoke_rows(), engine),
        )


def main() -> None:
    conns = connect_all()
    for engine, conn in conns.items():
        seed_smoke(conn, engine)
        count = conn.execute("SELECT count(*) FROM t_smoke").fetchone()[0]
        print(f"{engine}: t_smoke seeded, {count} rows")


if __name__ == "__main__":
    main()
