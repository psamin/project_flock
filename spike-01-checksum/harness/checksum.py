"""In-engine range checksum.

Hard rule 1: the engines compute, the harness compares. Everything below builds a
single SQL string that runs *inside* each database; Python only ever sees the two
numbers it comes back with.

Recipe
------
Per row: canonicalize each column to text, md5 each column to a fixed-width digest,
concatenate the digests, md5 again. Fixed-width digests kill delimiter ambiguity —
no `'a|b'` vs `'a','b'` collision, no escaping. NULL renders as `'NULL_'` (5 chars,
deliberately not 32) so it can never collide with a real digest.

Per range: take the first 16 hex chars of the row hash, parse via
`('x' || hex)::bit(64)::int8`, and sum as NUMERIC (no overflow, order-independent).
The bit-cast is a Postgres idiom — verified to work identically on CockroachDB
(see RESULTS.md); 64 bits rather than the 32 in the original sketch because both
engines accept the wider slice and it costs nothing.
"""

NULL_DIGEST = "NULL_"
HASH_SLICE = 16  # hex chars -> bit(64)

# type name -> SQL expression template. Filled in one type at a time during M2;
# any type absent here falls back to a plain ::text cast, which is a hypothesis,
# not a guarantee.
CANONICAL_CASTS: dict[str, str] = {}


def canonical(col: str, type_name: str | None = None) -> str:
    """SQL expression rendering `col` as engine-independent text."""
    template = CANONICAL_CASTS.get(type_name or "", "{col}::text")
    return template.format(col=f'"{col}"')


def row_hash_expr(columns: list[tuple[str, str]]) -> str:
    """md5 of the concatenated per-column digests."""
    digests = " || ".join(
        f"coalesce(md5({canonical(name, type_name)}), '{NULL_DIGEST}')"
        for name, type_name in columns
    )
    return f"md5({digests})"


def checksum_sql(table: str, pk_col: str, columns: list[tuple[str, str]]) -> str:
    row_hash = row_hash_expr(columns)
    return (
        f"SELECT sum(('x' || substr({row_hash}, 1, {HASH_SLICE}))::bit(64)::int8::numeric),"
        f" count(*) FROM {table} WHERE \"{pk_col}\" >= %s AND \"{pk_col}\" < %s"
    )


def columns_of(conn, table: str) -> list[tuple[str, str]]:
    """(name, type) for every column, ordered by name so both engines agree on
    column order regardless of how the table was declared."""
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_name = %s ORDER BY column_name",
        (table,),
    ).fetchall()
    return [(name, type_name) for name, type_name in rows]


def range_checksum(conn, table: str, pk_col: str, lo, hi) -> tuple[int | None, int]:
    """Checksum of rows where lo <= pk < hi, computed in-engine.

    Identical data => identical value on both engines.
    Returns (checksum, row_count); an empty range returns (None, 0).
    """
    columns = columns_of(conn, table)
    checksum, count = conn.execute(
        checksum_sql(table, pk_col, columns), (lo, hi)
    ).fetchone()
    return (None if checksum is None else int(checksum), count)


def split_range(lo, hi, fanout: int = 8) -> list[tuple]:
    """Split [lo, hi) into up to `fanout` contiguous sub-ranges."""
    width = hi - lo
    if width <= 1:
        return [(lo, hi)] if width == 1 else []
    n = min(fanout, width)
    step, extra = divmod(width, n)
    bounds, cursor = [], lo
    for i in range(n):
        nxt = cursor + step + (1 if i < extra else 0)
        bounds.append((cursor, nxt))
        cursor = nxt
    return bounds
