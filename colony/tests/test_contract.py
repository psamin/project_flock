"""The fake must not drift from the real client.

Lanes 2 and 4 build against FakeFleetMem on day 1 (§5.2). If a signature changes
on one side only, their code compiles against the fake and breaks on the cluster
— exactly the failure the fake exists to prevent.
"""

import inspect

import pytest

from fleetmem.client import CockroachFleetMem
from fleetmem.fake import FakeFleetMem

# The published SDK surface (§5.1 lane 1).
SDK_METHODS = [
    "report_observation", "get_beliefs", "claim_task", "complete_task",
    "heartbeat", "log_event",
    # supporting methods the orchestrator and tests rely on
    "create_task", "open_tasks", "find_similar", "register_robot",
    "stale_robots", "events", "close",
]


@pytest.mark.parametrize("name", SDK_METHODS)
def test_fake_implements_the_method(name):
    assert hasattr(FakeFleetMem, name), f"FakeFleetMem is missing {name}()"
    assert hasattr(CockroachFleetMem, name), f"CockroachFleetMem is missing {name}()"


@pytest.mark.parametrize("name", SDK_METHODS)
def test_signatures_match(name):
    real = inspect.signature(getattr(CockroachFleetMem, name))
    fake = inspect.signature(getattr(FakeFleetMem, name))
    assert real == fake, f"{name}() has drifted:\n  client: {real}\n  fake:   {fake}"


def test_no_undocumented_public_methods_on_the_client():
    """Anything public on the client is something another lane may call, so it
    belongs in the published list above — or it should be private."""
    public = {
        name for name, _ in inspect.getmembers(CockroachFleetMem, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public <= set(SDK_METHODS), f"undeclared public API: {public - set(SDK_METHODS)}"
