"""Golden tables: one per type, seeded identically on both engines.

Values are emitted as **SQL literal expressions**, not Python objects, so both
engines receive byte-identical input text and any divergence we observe is the
engine's rendering, never the driver's adaptation.
"""

import random
import uuid

RNG_SEED = 20260801
ROWS_PER_TABLE = 200
NULL_EVERY = 13

# type key -> DDL type for the `v` column
SQL_TYPES = {
    "int8": "int8",
    "text": "text",
    "bool": "bool",
    "uuid": "uuid",
    "date": "date",
    "timestamptz": "timestamptz",
    "numeric": "numeric",
    "float8": "float8",
    "bytea": "bytea",
    "jsonb": "jsonb",
}

# Values that have historically broken cross-engine equality. These lead every
# golden table; the deterministic filler pads out to ROWS_PER_TABLE.
BOUNDARIES: dict[str, list[str]] = {
    "int8": ["-9223372036854775808", "9223372036854775807", "0", "-1", "1"],
    "text": [
        "''",                       # empty string vs NULL
        "'NULL_'",                  # the sentinel itself
        "'|'",                      # delimiter that fixed-width digests must survive
        "'5d41402abc4b2a76b9719d911017c592'",  # md5-looking hex
        "'  leading and trailing  '",
        "'🚁🐓 flock'",              # emoji / 4-byte utf8
        "'ß Straße ñ 日本語'",        # multi-byte, case-folding bait
        "'line1' || chr(10) || 'line2'",
    ],
    "bool": ["true", "false"],
    "uuid": [
        "'00000000-0000-0000-0000-000000000000'::uuid",
        "'ffffffff-ffff-ffff-ffff-ffffffffffff'::uuid",
        "'A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11'::uuid",  # uppercase input
    ],
    "date": ["'0001-01-01'::date", "'1970-01-01'::date", "'9999-12-31'::date", "'2026-08-01'::date"],
    "timestamptz": [
        "'1970-01-01 00:00:00+00'::timestamptz",
        "'2026-08-01 00:00:00+00'::timestamptz",          # midnight
        "'2026-08-01 12:34:56.123456+00'::timestamptz",   # full 6-digit sub-second
        "'2026-08-01 12:34:56.100000+00'::timestamptz",   # trailing zeros in sub-second
        "'2026-08-01 12:34:56.000001+00'::timestamptz",   # leading zeros in sub-second
        "'2026-03-08 07:00:00+00'::timestamptz",          # US DST transition instant
        "'1900-01-01 00:00:00+00'::timestamptz",          # pre-epoch
        "'2026-08-01 12:34:56-05'::timestamptz",          # non-UTC input offset
    ],
    "numeric": [
        "1.50", "1.5",                                     # the classic trailing-zero fight
        "0.0", "-0.0", "0",
        "1e10", "1E-10",
        "12345678901234567890.123456789",
        "-0.000000000001",
    ],
    "float8": [
        "'NaN'::float8", "'Infinity'::float8", "'-Infinity'::float8",
        "'-0.0'::float8", "'0.0'::float8",
        "0.1::float8", "(1.0/3.0)::float8",
        "1e308::float8", "5e-324::float8",                # max normal, min subnormal
    ],
    "bytea": [
        "decode('', 'hex')",                               # empty vs NULL
        "decode('00', 'hex')",
        "decode('deadbeef', 'hex')",
        "decode('ff00ff00', 'hex')",
    ],
    "jsonb": [
        """'{"b":1,"a":2}'::jsonb""",                       # key order normalization
        """'{"a":2,"b":1}'::jsonb""",
        """'{ "a" :  2 , "b" : 1 }'::jsonb""",              # whitespace normalization
        """'{"n":1.0}'::jsonb""",                           # number rendering
        """'{"n":1}'::jsonb""",
        """'{"n":1.10}'::jsonb""",
        """'[1,2,{"k":null}]'::jsonb""",
        """'{"u":"🚁 日本語"}'::jsonb""",
        "'null'::jsonb",                                    # JSON null vs SQL NULL
    ],
}


def _filler(type_key: str, rng: random.Random) -> str:
    if type_key == "int8":
        return str(rng.randrange(-(2**63), 2**63))
    if type_key == "text":
        return "'" + "".join(rng.choice("abcXYZ019 -_.") for _ in range(rng.randrange(1, 24))) + "'"
    if type_key == "bool":
        return rng.choice(["true", "false"])
    if type_key == "uuid":
        return f"'{uuid.UUID(int=rng.getrandbits(128))}'::uuid"
    if type_key == "date":
        return f"'{2000 + rng.randrange(30)}-{rng.randrange(1, 13):02d}-{rng.randrange(1, 29):02d}'::date"
    if type_key == "timestamptz":
        return (
            f"'{1970 + rng.randrange(80)}-{rng.randrange(1, 13):02d}-{rng.randrange(1, 29):02d}"
            f" {rng.randrange(24):02d}:{rng.randrange(60):02d}:{rng.randrange(60):02d}"
            f".{rng.randrange(10**6):06d}+00'::timestamptz"
        )
    if type_key == "numeric":
        return f"{rng.randrange(-(10**12), 10**12)}.{rng.randrange(10**6):06d}"
    if type_key == "float8":
        return f"{rng.uniform(-1e6, 1e6)!r}::float8"
    if type_key == "bytea":
        return "decode('" + "".join(rng.choice("0123456789abcdef") for _ in range(rng.randrange(0, 16) * 2)) + "', 'hex')"
    if type_key == "jsonb":
        return f"""'{{"i":{rng.randrange(1000)},"s":"v{rng.randrange(1000)}"}}'::jsonb"""
    raise ValueError(type_key)


def golden_values(type_key: str) -> list[str]:
    """SQL expressions for `v`, one per row id. Deterministic."""
    rng = random.Random(RNG_SEED + len(type_key))
    values = list(BOUNDARIES[type_key])
    while len(values) < ROWS_PER_TABLE:
        values.append("NULL" if len(values) % NULL_EVERY == 0 else _filler(type_key, rng))
    return values


def table_name(type_key: str) -> str:
    return f"t_{type_key}"


def seed_golden(conn, type_key: str, engine_offset: int) -> None:
    """Create and fill one golden table; engine_offset varies insertion order."""
    table, sql_type = table_name(type_key), SQL_TYPES[type_key]
    values = golden_values(type_key)
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"CREATE TABLE {table} (id int8 PRIMARY KEY, v {sql_type})")

    rows = list(enumerate(values))
    random.Random(RNG_SEED + engine_offset).shuffle(rows)
    for i, expr in rows:
        conn.execute(f"INSERT INTO {table} (id, v) VALUES ({i}, {expr})")


def seed_all(conns) -> None:
    for offset, (engine, conn) in enumerate(sorted(conns.items())):
        for type_key in SQL_TYPES:
            seed_golden(conn, type_key, offset)
