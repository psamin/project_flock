"""`/health` tells you which integrations are actually live.

The sim is designed to degrade rather than crash: no cluster falls back to
in-memory fleet memory, no credentials falls back to rule-based planning. Both
are deliberate (§5.4 puts demo reliability first), and both mean a fully
degraded run looks exactly like a working one from the outside.

So the thing under test here is not the fallback — it is that the fallback is
*visible*. Each case below is a configuration someone could demo by accident.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


def _fresh_server():
    """A reloaded server module, so env vars are read at Mission construction.

    `sim.server` builds its mission at import time, which means the adapter is
    chosen then too — without the reload every test in the file would see
    whichever configuration happened to import it first. Not wrapped in `with
    TestClient(...)` for the reason `test_console_api._fresh_server` documents:
    the lifespan would start the 4 Hz tick loop on the client's thread.
    """
    import importlib

    from sim import server as server_module

    importlib.reload(server_module)
    return TestClient(server_module.app)


@pytest.fixture
def health(monkeypatch):
    """Ask /health under a given configuration, always on the fake memory."""

    def ask(**env: str) -> dict:
        monkeypatch.setenv("COLONY_MEMORY", "fake")
        monkeypatch.delenv("COLONY_BEDROCK_MODE", raising=False)
        monkeypatch.delenv("COLONY_BEDROCK_CASSETTE", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return _fresh_server().get("/health").json()

    return ask


def test_the_default_configuration_reports_no_aws_calls(health):
    """`make sim` on a bare laptop. Every robot runs on rules, and the numbers
    say so rather than leaving it to be inferred from the absence of a log."""
    bedrock = health()["bedrock"]
    assert bedrock == {
        "requested": "replay",
        "mode": "replay",
        "calls": 0,
        "cassette_entries": 0,
    }


def test_a_credential_downgrade_is_visible_as_a_disagreement(health, monkeypatch):
    """The failure this endpoint exists for.

    `adapter_from_env` quietly drops live to replay when no credentials resolve,
    which is right — a missing credential should be a degraded demo, not a
    crashed one. But it means the one configuration where you *think* you are
    calling AWS and are not is otherwise indistinguishable from success.
    Reporting both the requested and the effective mode turns that into
    something you can see.
    """
    monkeypatch.setattr("bedrock.adapter.has_credentials", lambda: False)
    bedrock = health(COLONY_BEDROCK_MODE="live")["bedrock"]
    assert bedrock["requested"] == "live"
    assert bedrock["mode"] == "replay"


def test_live_is_reported_when_credentials_resolve(health, monkeypatch):
    """The inverse, so the test above cannot pass by never reaching live at all."""
    monkeypatch.setattr("bedrock.adapter.has_credentials", lambda: True)
    bedrock = health(COLONY_BEDROCK_MODE="live")["bedrock"]
    assert bedrock["requested"] == bedrock["mode"] == "live"


def test_an_empty_cassette_is_distinguishable_from_a_loaded_one(health, tmp_path):
    """Replay with nothing to replay is the other silent no-op: every
    `knows_plan` misses, so the fleet runs on rules while the mode still reads
    `replay`. The entry count is what separates the two."""
    assert health()["bedrock"]["cassette_entries"] == 0

    cassette = tmp_path / "cassette.json"
    cassette.write_text(json.dumps({"plan:abc": "{}", "embed:def": [0.0, 1.0]}))
    loaded = health(COLONY_BEDROCK_CASSETTE=str(cassette))["bedrock"]
    assert loaded["cassette_entries"] == 2


def test_a_named_cluster_that_is_unreachable_refuses_to_start(monkeypatch):
    """The most expensive failure this project has, and the reason it is not a
    fallback: with COLONY_DSN set and the cluster down, the old behaviour ran
    the whole mission on in-memory memory. Nothing persisted, no vector index,
    no serializable claiming — and the UI looked perfect. A typo in a Cloud DSN
    was indistinguishable from a working demo.

    Naming a cluster has to mean it. The refusal names the cause and the escape
    hatch, because this fires at the worst possible moment.
    """
    monkeypatch.delenv("COLONY_MEMORY", raising=False)
    # Port 1 is reserved and nothing listens there.
    monkeypatch.setenv(
        "COLONY_DSN", "postgresql://root@127.0.0.1:1/colony?sslmode=disable"
    )
    with pytest.raises(RuntimeError, match="COLONY_DSN is set"):
        _fresh_server()


def test_asking_for_the_fake_still_works_with_a_dsn_set(monkeypatch):
    """The escape hatch the refusal points at has to actually exist — a
    developer with a Cloud DSN in their shell must still be able to work on the
    renderer offline."""
    monkeypatch.setenv("COLONY_MEMORY", "fake")
    monkeypatch.setenv(
        "COLONY_DSN", "postgresql://root@127.0.0.1:1/colony?sslmode=disable"
    )
    assert _fresh_server().get("/health").json()["memory"] == "fake"


def test_no_dsn_still_degrades_quietly_to_the_fake(monkeypatch):
    """`make sim` on a bare laptop, which is how lanes 2 and 4 were unblocked.
    Nobody named a cluster, so there is nothing to be wrong about."""
    monkeypatch.delenv("COLONY_MEMORY", raising=False)
    monkeypatch.delenv("COLONY_DSN", raising=False)
    assert _fresh_server().get("/health").json()["memory"] == "fake"


def test_the_two_modes_do_not_collide(health):
    """`mode` at the top level is the coordination mode (§4.7) and `bedrock.mode`
    is the adapter's. They are different vocabularies on the same word, which is
    why the adapter's is nested rather than flattened."""
    body = health()
    assert body["mode"] == "coordinated"
    assert body["bedrock"]["mode"] == "replay"
    assert body["memory"] == "fake"
