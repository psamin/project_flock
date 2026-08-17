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
    "report_observation",
    "get_beliefs",
    "claim_task",
    "complete_task",
    "heartbeat",
    "log_plan",
    "log_event",
    # the batched form: a tick's events in one round trip, which is what the
    # sim loop uses. log_event() is the single-row convenience on top of it.
    "log_events",
    # lease mechanics (§4.4) — recovery lives here, so the fake must match
    "renew_leases",
    "release_task",
    # supporting methods the orchestrator and tests rely on
    "create_task",
    "open_tasks",
    "find_similar",
    "register_robot",
    "register_victim",
    "stale_robots",
    "events",
    "plans_for",
    # semantic memory (§4.0): the only search in the SDK that crosses missions
    "remember_lesson",
    "recall_lessons",
    "mark_recalled",
    # operator interventions (issue #22). On the SDK surface rather than beside
    # the server because writing the row *is* the operator's entire channel —
    # the fleet learns about a disruption the same way it learns everything,
    # and the fake has to offer that path too or the feature needs a cluster.
    "record_intervention",
    "active_hazards",
    "close",
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
        name
        for name, _ in inspect.getmembers(CockroachFleetMem, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public <= set(SDK_METHODS), (
        f"undeclared public API: {public - set(SDK_METHODS)}"
    )
