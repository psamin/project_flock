import uuid
from pathlib import Path

import pytest

from fleetmem.client import DEFAULT_DSN, CockroachFleetMem
from fleetmem.fake import FakeFleetMem

SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "v0.sql"


def _db_available() -> bool:
    try:
        CockroachFleetMem().close()
        return True
    except Exception:
        return False


DB_UP = _db_available()
needs_db = pytest.mark.skipif(not DB_UP, reason="no CockroachDB at localhost:26257 (make dev)")


@pytest.fixture(scope="session")
def db():
    if not DB_UP:
        pytest.skip("no CockroachDB (make dev)")
    mem = CockroachFleetMem()
    yield mem
    mem.close()


@pytest.fixture
def fake():
    return FakeFleetMem()


@pytest.fixture(params=["fake", "cockroach"])
def mem(request):
    """Every behavioural test runs against BOTH implementations.

    That is the point of the fake: lanes 2 and 4 build against it today, so it
    has to behave like the real thing, not merely satisfy the same signatures.
    """
    if request.param == "fake":
        yield FakeFleetMem()
        return
    if not DB_UP:
        pytest.skip("no CockroachDB (make dev)")
    mem = CockroachFleetMem()
    yield mem
    mem.close()


@pytest.fixture
def mission():
    """A fresh mission id, so tests sharing one database never see each other."""
    return uuid.uuid4()
