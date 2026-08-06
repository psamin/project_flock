"""The Managed MCP Server config (§6.2 required tool #1, §3.5, §6.3).

The endpoint itself needs a Cloud cluster nobody has yet, so what is testable is
the part that would still be wrong once it exists: the snippet must name the
read-only role and must not turn writes on. §6.3's words are "leave writes off —
it *is* our access-control story", and a story nobody asserted is one that gets
edited by accident.
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


def test_the_console_connects_as_the_read_only_role():
    """Not as root. `commander` holds SELECT and nothing else, which is what
    makes §3.5's posture a property rather than a setting."""
    entry = config("abc-123")["mcpServers"][SERVER_NAME]
    assert entry["env"]["CRDB_SQL_USER"] == CONSOLE_ROLE
    assert CONSOLE_ROLE == "commander"


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


def test_a_missing_cluster_id_is_a_visible_placeholder():
    """Rather than a plausible-looking wrong value: the hookup is blocked on the
    Cloud cluster, and the config should say so instead of looking finished."""
    entry = config(None)["mcpServers"][SERVER_NAME]
    assert "<cluster-id>" in entry["url"]


def test_the_snippet_is_valid_json_for_a_client_to_paste():
    json.loads(json.dumps(config("abc-123")))


@pytest.mark.parametrize("field", ["type", "url", "readOnly", "description"])
def test_the_entry_has_what_a_client_needs(field):
    assert field in config("abc-123")["mcpServers"][SERVER_NAME]
