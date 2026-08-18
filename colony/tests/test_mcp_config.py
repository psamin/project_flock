"""The Managed MCP Server config (§6.2 required tool #1, §3.5, §6.3).

This file used to assert that the snippet names `commander` in `CRDB_SQL_USER`,
on the belief that the console would therefore connect as a SELECT-only role.
Calling the endpoint for real disproved it: MCP connects as `managed-mcp`, its
own service identity, and that key was never read. The assertion is inverted
below rather than deleted — a test that once pinned a false claim is the right
place to stop it coming back.

The runtime client is tested in `test_mcp_client.py`; this file covers only the
editor-facing snippet `infra/mcp.py` prints.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[2] / "infra"
sys.path.insert(0, str(INFRA))

from mcp import CONSOLE_ROLE, MANAGED_ENDPOINT, SERVER_NAME, config


def test_the_snippet_points_at_the_managed_endpoint():
    """§6.2 names it: the hosted endpoint, not a server we wrote. Using our own
    would not be the required tool."""
    entry = config("abc-123")["mcpServers"][SERVER_NAME]
    assert entry["url"].startswith(MANAGED_ENDPOINT)
    assert "cluster=abc-123" in entry["url"]


def test_the_snippet_does_not_claim_to_set_the_sql_user():
    """The inverted assertion.

    `CRDB_SQL_USER` is not read by the managed endpoint — it connects as
    `managed-mcp` regardless — so emitting it advertised an access-control
    property this path does not have. `commander` still exists and still holds
    SELECT and nothing else; it governs `console/reader.py`, which is a
    different path, and `test_credentials.py` is where that is asserted.
    """
    entry = config("abc-123")["mcpServers"][SERVER_NAME]
    assert "env" not in entry, "the snippet must not imply it sets the SQL user"
    assert CONSOLE_ROLE == "commander"


def test_the_cluster_id_is_in_the_url_for_humans_not_for_the_server():
    """Documents the trap rather than the fix.

    The server ignores `?cluster=` and fails with "cluster_id not provided"
    unless the id also arrives as a tool argument. The parameter stays because
    it tells a person which cluster an entry points at; the runtime client
    injects the argument (`mcp_client.MCPClient.call`).
    """
    from console.mcp_client import MCPClient

    entry = config("abc-123")["mcpServers"][SERVER_NAME]
    assert "cluster=abc-123" in entry["url"]
    client = MCPClient(cluster_id="abc-123")
    assert client.cluster_id == "abc-123"


def test_writes_are_off_explicitly():
    """The default is already read-only (§6.3). Saying so anyway is what stops
    it being flipped by someone who did not know it mattered."""
    assert config("abc-123")["mcpServers"][SERVER_NAME]["readOnly"] is True


def test_the_snippet_carries_no_secret():
    """This file is committed. A password in it would be a credential leak
    dressed as configuration."""
    blob = json.dumps(config("abc-123")).lower()
    for secret in ("password", "sslrootcert", "secret", "token", "postgresql://"):
        assert secret not in blob, f"the snippet leaks {secret!r}"


def test_a_missing_cluster_id_is_a_visible_placeholder(monkeypatch):
    """Rather than a plausible-looking wrong value: a config that looks finished
    and is not is worse than one that says what it is waiting for.

    `CRDB_CLUSTER_ID` is cleared explicitly. `config(None)` falls back to the
    environment before it falls back to the placeholder, so a developer with the
    variable exported — which `colony/.env` now does — was testing the opposite
    branch and passing for the wrong reason.
    """
    monkeypatch.delenv("CRDB_CLUSTER_ID", raising=False)
    entry = config(None)["mcpServers"][SERVER_NAME]
    assert "<cluster-id>" in entry["url"]


def test_the_environment_supplies_the_cluster_id_when_it_is_set(monkeypatch):
    """The branch the test above deliberately avoids. Asserted rather than left
    implicit, because it is the one that runs in practice."""
    monkeypatch.setenv("CRDB_CLUSTER_ID", "from-the-environment")
    entry = config(None)["mcpServers"][SERVER_NAME]
    assert "cluster=from-the-environment" in entry["url"]


def test_the_snippet_is_valid_json_for_a_client_to_paste():
    json.loads(json.dumps(config("abc-123")))


@pytest.mark.parametrize("field", ["type", "url", "readOnly", "description"])
def test_the_entry_has_what_a_client_needs(field):
    assert field in config("abc-123")["mcpServers"][SERVER_NAME]
