"""M3: adversarial values. Hypothesis-generated rows plus curated nasties.

Every case seeds the *same* values into both engines and asserts the checksums
agree. Failures here are canonicalization bugs, not test bugs.
"""

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from harness.checksum import range_checksum

SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def _roundtrip(conns, sql_type: str, values: list, cast: str = "") -> None:
    """Seed `values` into a fresh table on both engines; assert checksums match."""
    for conn in conns.values():
        conn.execute("DROP TABLE IF EXISTS t_adv")
        conn.execute(f"CREATE TABLE t_adv (id int8 PRIMARY KEY, v {sql_type})")
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO t_adv (id, v) VALUES (%s, %s{cast})",
                list(enumerate(values)),
            )
    pg = range_checksum(conns["pg"], "t_adv", "id", 0, len(values) + 1)
    crdb = range_checksum(conns["crdb"], "t_adv", "id", 0, len(values) + 1)
    assert pg == crdb, f"{sql_type} {values!r}: pg={pg} crdb={crdb}"
    assert pg[1] == len(values)


# --- property tests ---


@SETTINGS
@given(st.lists(st.integers(min_value=-(2**63), max_value=2**63 - 1), min_size=1, max_size=30))
def test_int8_property(conns, values):
    _roundtrip(conns, "int8", values)


@SETTINGS
@given(st.lists(st.text(max_size=40), min_size=1, max_size=30))
def test_text_property(conns, values):
    # Postgres rejects NUL inside text; not a cross-engine question.
    _roundtrip(conns, "text", [v.replace("\x00", "") for v in values])


@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.floats(allow_nan=True, allow_infinity=True), min_size=1, max_size=30))
def test_float8_property(conns, values):
    """Turned up to 150 examples: float8 is the type that actually fought back,
    and both of its divergences were found here rather than by curated values."""
    _roundtrip(conns, "float8", values)


def test_float8_precision_is_not_silently_dropped(conns):
    """extra_float_digits=0 renders 15 significant digits; a double carries 17.
    These two values differ only past digit 15, so a canonical cast built on the
    15-digit text alone would collide — a false pass. The residual term is what
    keeps them apart."""
    a, b = 2.4058303385923568e16, 2.4058303385923572e16
    assert a != b
    for engine, conn in conns.items():
        for table, value in [("t_prec_a", a), ("t_prec_b", b)]:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(f"CREATE TABLE {table} (id int8 PRIMARY KEY, v float8)")
            conn.execute(f"INSERT INTO {table} VALUES (0, %s)", (value,))
        # the 15-digit renderings really are identical — the collision is real
        assert (
            conn.execute("SELECT v::text FROM t_prec_a").fetchone()[0]
            == conn.execute("SELECT v::text FROM t_prec_b").fetchone()[0]
        ), f"{engine}: premise broken, values differ within 15 digits"
        assert range_checksum(conn, "t_prec_a", "id", 0, 2) != range_checksum(
            conn, "t_prec_b", "id", 0, 2
        ), f"{engine}: distinct doubles collided"


@SETTINGS
@given(st.lists(st.binary(max_size=40), min_size=1, max_size=30))
def test_bytea_property(conns, values):
    _roundtrip(conns, "bytea", values)


@SETTINGS
@given(
    st.lists(
        st.datetimes(
            min_value=__import__("datetime").datetime(1900, 1, 1),
            max_value=__import__("datetime").datetime(2100, 1, 1),
            timezones=st.just(__import__("datetime").timezone.utc),
        ),
        min_size=1,
        max_size=30,
    )
)
def test_timestamptz_property(conns, values):
    _roundtrip(conns, "timestamptz", values)


@SETTINGS
@given(
    st.lists(
        st.decimals(
            min_value=-(10**18),
            max_value=10**18,
            allow_nan=False,
            allow_infinity=False,
            places=6,
        ),
        min_size=1,
        max_size=30,
    )
)
def test_numeric_property(conns, values):
    _roundtrip(conns, "numeric", values)


# --- curated nasties, one test each ---


@pytest.mark.parametrize(
    "sql_type,exprs",
    [
        ("float8", ["'NaN'::float8", "'Infinity'::float8", "'-Infinity'::float8",
                    "'-0.0'::float8", "'0.0'::float8"]),
        ("numeric", ["1.50", "1.5", "0.0", "-0.0", "0", "1e10", "1E-10"]),
        ("timestamptz", ["'2026-08-01 00:00:00+00'::timestamptz",
                         "'2026-08-01 23:59:59.999999+00'::timestamptz",
                         "'2026-08-01 12:00:00.000001+00'::timestamptz",
                         "'2026-08-01 12:00:00.100000+00'::timestamptz",
                         "'2026-03-08 07:00:00+00'::timestamptz"]),
        ("text", ["''", "'NULL_'", "'|'", "'||'",
                  "'5d41402abc4b2a76b9719d911017c592'",
                  "'🚁🐓'", "'日本語'", "'ß'", "'  '"]),
        ("int8", ["-9223372036854775808", "9223372036854775807", "0", "-1"]),
        ("jsonb", ["""'{"a":1,"b":2}'::jsonb""", """'{"b":2,"a":1}'::jsonb""",
                   "'null'::jsonb", "'[]'::jsonb", "'{}'::jsonb"]),
    ],
)
def test_curated_nasties(conns, sql_type, exprs):
    for conn in conns.values():
        conn.execute("DROP TABLE IF EXISTS t_nasty")
        conn.execute(f"CREATE TABLE t_nasty (id int8 PRIMARY KEY, v {sql_type})")
        for i, expr in enumerate(exprs):
            conn.execute(f"INSERT INTO t_nasty (id, v) VALUES ({i}, {expr})")
        conn.execute(f"INSERT INTO t_nasty (id, v) VALUES ({len(exprs)}, NULL)")
    pg = range_checksum(conns["pg"], "t_nasty", "id", 0, len(exprs) + 5)
    crdb = range_checksum(conns["crdb"], "t_nasty", "id", 0, len(exprs) + 5)
    assert pg == crdb, f"{sql_type}: pg={pg} crdb={crdb}"


def test_delimiter_injection_cannot_forge_a_row(conns):
    """Two columns whose contents, if naively concatenated, would produce the
    same byte string. Fixed-width digests are what stop this from colliding."""
    for conn in conns.values():
        conn.execute("DROP TABLE IF EXISTS t_inject")
        conn.execute("CREATE TABLE t_inject (id int8 PRIMARY KEY, a text, b text)")
        conn.execute("INSERT INTO t_inject VALUES (0, 'ab', 'c'), (1, 'a', 'bc')")
    for engine, conn in conns.items():
        left = range_checksum(conn, "t_inject", "id", 0, 1)
        right = range_checksum(conn, "t_inject", "id", 1, 2)
        assert left != right, f"{engine}: ('ab','c') and ('a','bc') collided"
