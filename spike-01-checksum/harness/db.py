"""Connections to both engines. CockroachDB speaks pgwire, so psycopg drives both."""

import psycopg

PG_DSN = "postgresql://flock:flock@localhost:5432/flock"
CRDB_DSN = "postgresql://root@localhost:26257/defaultdb?sslmode=disable"

ENGINES = ("pg", "crdb")


def connect(engine: str) -> psycopg.Connection:
    """Open a connection with the session pinned to UTC (hard rule 5)."""
    dsn = PG_DSN if engine == "pg" else CRDB_DSN
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET TIME ZONE 'UTC'")
    return conn


def connect_all() -> dict[str, psycopg.Connection]:
    return {engine: connect(engine) for engine in ENGINES}
