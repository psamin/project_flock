"""Connections to both engines. CockroachDB speaks pgwire, so psycopg drives both."""

import psycopg

PG_DSN = "postgresql://flock:flock@localhost:5432/flock"
CRDB_DSN = "postgresql://root@localhost:26257/defaultdb?sslmode=disable"

ENGINES = ("pg", "crdb")


def connect(engine: str) -> psycopg.Connection:
    """Open a connection with the session settings the canonical recipe depends on.

    Both are load-bearing (hard rule 5), and both are honoured by both engines:
      - TIME ZONE UTC          timestamptz canonicalizes via ::text, which renders
                               in the session zone.
      - extra_float_digits 0   the engines only agree on float8 text at 0; the
                               float8 canonical cast restores the lost precision
                               with a residual term (see checksum.py).
    """
    dsn = PG_DSN if engine == "pg" else CRDB_DSN
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET TIME ZONE 'UTC'")
    conn.execute("SET extra_float_digits = 0")
    return conn


def connect_all() -> dict[str, psycopg.Connection]:
    return {engine: connect(engine) for engine in ENGINES}
