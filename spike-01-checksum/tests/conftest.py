import pytest

from harness.db import ENGINES, connect
from harness.golden import seed_all
from harness.seed import seed_smoke


@pytest.fixture(scope="session")
def conns():
    conns = {engine: connect(engine) for engine in ENGINES}
    yield conns
    for conn in conns.values():
        conn.close()


@pytest.fixture(scope="session")
def smoke(conns):
    """t_smoke seeded on both engines, identical rows in different insertion orders."""
    for engine, conn in conns.items():
        seed_smoke(conn, engine)
    return conns


@pytest.fixture(scope="session")
def golden(conns):
    """One golden table per type, seeded identically on both engines."""
    seed_all(conns)
    return conns
