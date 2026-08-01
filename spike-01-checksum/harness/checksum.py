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

# Numeric canonicalization. `v::text` diverges because CockroachDB renders values
# carrying an exponent in scientific notation ('1E+10') where Postgres always
# renders plain ('10000000000'). to_char with an FM format agrees on both engines
# AND normalizes display scale (1.50 -> 1.5, which is what we want: numerically
# equal values must not raise a false divergence).
#
# But to_char is a *lossy* renderer, and lossy means collisions, and a collision
# is a silent false pass. Two guards, both measured (see RESULTS.md):
#   - CRDB's to_char raises "invalid operation" above 20 integer digits, so the
#     guard must also keep to_char from ever being reached on such a value.
#   - Values with more fractional digits than the format render identically
#     (0.000…1 and 0.000…2 both become '0.') on BOTH engines — a real false pass.
# Outside the guarded range we fall back to `::text`, which is lossless on each
# engine but may render differently across them. That direction of error is safe:
# a spurious divergence gets investigated, a spurious match does not.
NUMERIC_INT_DIGITS = 20
NUMERIC_FRAC_DIGITS = 30
NUMERIC_FORMAT = "FM" + "9" * NUMERIC_INT_DIGITS + "." + "9" * NUMERIC_FRAC_DIGITS
NUMERIC_CANONICAL = (
    "case when abs({col}) < 1e20 and {col} = trunc({col}, 30)"
    f" then to_char({{col}}, '{NUMERIC_FORMAT}')"
    " else {col}::text end"
)

# float8 is the hardest of the ten, and the fight is over digits, not notation.
# At the default extra_float_digits=1 the engines disagree twice over: on when to
# switch to exponent form (1000000.0 -> '1000000' on PG, '1e+06' on CRDB) and on
# the shortest-round-trip digits themselves (2.4058303385923568e+16 on PG vs
# 2.405830338592357e+16 on CRDB — the same double, spelled differently).
# Hypothesis found both; M2's curated values missed both.
#
# Both engines honour extra_float_digits, and at 0 their renderings agree — but 0
# means 15 significant digits, and a double carries up to 17. Two distinct
# doubles can then render identically, which is a collision, which is a false
# pass. Trading false alarms for false passes is the wrong direction for a
# verifier, so 15-digit text alone is not enough.
#
# The residual restores exactness. `({col}::text)::float8` is the 15-digit value
# parsed back; subtracting it from the original is exact (Sterbenz — the operands
# are within a factor of two), and IEEE arithmetic is deterministic, so both
# engines compute a bit-identical residual whose own 15-digit rendering exposes
# the digits the first term dropped. Two doubles that agree on the first term but
# differ at all must differ in the second.
#
# Requires extra_float_digits=0 on BOTH sessions — a load-bearing session
# setting, not an incidental default (see db.py and RESULTS.md). Note the spike
# brief assumed this knob was PG-only; CockroachDB honours it too.
#
# -0.0 and 0.0 canonicalize identically. They are equal under IEEE comparison,
# and reporting that as data corruption would be a false alarm.
#
# The magnitude branch exists because rounding to 15 digits can round *up*: near
# DBL_MAX, `{col}::text` yields 1.79769313486232e+308, which is larger than
# DBL_MAX, so parsing it back raises "out of range" and takes down the whole
# range check. Dividing by four first is exact (a power of two, and the branch
# only applies to values far from underflow), keeps the round-trip in range, and
# stays injective — so the extreme band is checked as precisely as the rest
# rather than being quietly excused.
_residual = "({v})::text || '|' || (({v}) - ((({v})::text)::float8))::text"
FLOAT8_CANONICAL = (
    "case when abs({col}) > 1e307::float8"
    f" then {_residual.format(v='{col} / 4')}"
    f" else {_residual.format(v='{col}')} end"
)

# data_type (as reported by information_schema, verified identical on both
# engines) -> SQL expression template. Only types whose naive `::text` cast was
# *measured* to diverge appear here; the other eight were proven equal as-is and
# are documented in RESULTS.md rather than wrapped in ceremony.
CANONICAL_CASTS: dict[str, str] = {
    "numeric": NUMERIC_CANONICAL,
    "double precision": FLOAT8_CANONICAL,
}


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
