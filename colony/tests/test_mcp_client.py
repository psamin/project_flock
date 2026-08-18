"""The runtime MCP client (§6.2 required tool #1).

What matters here is not that the client can talk to the managed endpoint — a
live call proves that, and `test_commander_agent.py` marks those `db`. It is
that the client cannot be talked *into* something: the allowlist holds, the
cluster id is always supplied, an error payload is raised rather than handed to
a model as data, and the token file is not world-readable.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from console import mcp_client
from console.mcp_client import TOOLS, MCPClient, MCPError, _content, _decode


@pytest.fixture
def client():
    return MCPClient(cluster_id="cluster-under-test", database="colony")


# --- the allowlist ---------------------------------------------------------


@pytest.mark.parametrize(
    "tool", ["insert_rows", "create_table", "create_database", "drop_table"]
)
def test_write_tools_are_refused_before_any_request(client, tool, monkeypatch):
    """The managed server exposes three write tools even with readOnly set in
    the config. The allowlist is what makes that irrelevant, and it must refuse
    without touching the network — a refusal that needs a round trip is a
    refusal that fails open when the network does."""

    def explode(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("a refused tool reached the transport")

    monkeypatch.setattr(mcp_client, "_post", explode)
    with pytest.raises(MCPError, match="allowlist"):
        client.call(tool, query="anything")


def test_the_allowlist_holds_only_reads():
    """Named individually rather than checked by prefix: `show_statement` reads
    and `create_table` does not, and both would pass a naive name test."""
    assert set(TOOLS) == {
        "select_query",
        "explain_query",
        "get_table_schema",
        "list_tables",
        "show_running_queries",
        "show_statement",
    }


# --- the argument the server actually requires ------------------------------


def test_every_call_carries_the_cluster_id(client, monkeypatch):
    """The `?cluster=` URL parameter is not read by the server; calls without
    the argument fail with "cluster_id not provided". Injecting it here is what
    keeps every caller from having to know that."""
    seen = {}

    def capture(_url, payload, **_kwargs):
        seen.update(payload["params"]["arguments"])
        return {"result": {"content": [{"type": "text", "text": "{}"}]}}

    monkeypatch.setattr(mcp_client, "_post", capture)
    monkeypatch.setattr(mcp_client, "access_token", lambda: "t")
    client.call("list_tables")
    assert seen["cluster_id"] == "cluster-under-test"
    assert seen["database"] == "colony"


def test_an_explicit_cluster_id_is_not_overwritten(client, monkeypatch):
    seen = {}

    def capture(_url, payload, **_kwargs):
        seen.update(payload["params"]["arguments"])
        return {"result": {"content": [{"type": "text", "text": "{}"}]}}

    monkeypatch.setattr(mcp_client, "_post", capture)
    monkeypatch.setattr(mcp_client, "access_token", lambda: "t")
    client.call("list_tables", cluster_id="somewhere-else")
    assert seen["cluster_id"] == "somewhere-else"


def test_a_client_without_a_cluster_id_refuses_to_exist(monkeypatch):
    """Rather than failing on the first call, which would surface as a confusing
    server error in the middle of an agent loop."""
    monkeypatch.delenv("CRDB_CLUSTER_ID", raising=False)
    with pytest.raises(MCPError, match="cluster id"):
        MCPClient()


# --- error handling ---------------------------------------------------------


def test_a_tool_error_payload_raises_rather_than_returning(client):
    """The server reports a refused query as a *successful* JSON-RPC call
    carrying isError. Returning that text would hand a model an error string
    and let it narrate the error as though it were data."""
    with pytest.raises(MCPError, match="only SELECT"):
        _content({"isError": True, "content": [{"type": "text", "text": "only SELECT allowed"}]})


def test_a_jsonrpc_error_raises(client, monkeypatch):
    monkeypatch.setattr(
        mcp_client, "_post", lambda *a, **k: {"error": {"message": "boom"}}
    )
    monkeypatch.setattr(mcp_client, "access_token", lambda: "t")
    with pytest.raises(MCPError, match="boom"):
        client.call("list_tables")


def test_non_json_content_comes_back_as_text():
    """Not every tool answers with JSON, and a decoder that assumes otherwise
    turns a readable message into a parse error."""
    assert _content({"content": [{"type": "text", "text": "plain words"}]}) == "plain words"


def test_an_event_stream_response_is_decoded():
    """The HTTP transport may answer the same request with SSE instead of JSON.
    A client that handles only one works until the day the server picks the
    other, which is not a thing to discover during a demo."""
    body = 'event: message\ndata: {"result": {"ok": true}}\n\n'
    assert _decode(body, "text/event-stream")["result"]["ok"] is True


def test_an_empty_event_stream_is_an_error_not_a_silent_none():
    with pytest.raises(MCPError):
        _decode("event: ping\n\n", "text/event-stream")


# --- the token on disk ------------------------------------------------------


def test_the_token_file_is_not_world_readable(tmp_path, monkeypatch):
    """It holds a refresh token, which is a long-lived credential. 0600 is set
    at creation rather than by a chmod afterwards, so there is no window in
    which it exists and is readable."""
    path = tmp_path / "mcp-token.json"
    monkeypatch.setattr(mcp_client, "TOKEN_PATH", path)
    mcp_client._write_tokens({"access_token": "a", "refresh_token": "r"})
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}"


def test_a_refresh_without_a_new_refresh_token_keeps_the_old_one(tmp_path, monkeypatch):
    """A refresh response may legitimately omit refresh_token, meaning "keep
    the one you have". Overwriting with the absent value logs the agent out on
    its first successful refresh — headlessly, and hours later."""
    path = tmp_path / "mcp-token.json"
    monkeypatch.setattr(mcp_client, "TOKEN_PATH", path)
    mcp_client._write_tokens({"client_id": "c", "refresh_token": "original"})
    mcp_client._store({"access_token": "new", "expires_in": 3600}, "c")
    assert json.loads(path.read_text())["refresh_token"] == "original"


def test_missing_credentials_report_as_unauthenticated_not_as_a_crash(tmp_path, monkeypatch):
    """The console asks this on every page load and must get a boolean, not an
    exception — an unauthenticated deployment is normal, and falls back to the
    canned questions."""
    monkeypatch.setattr(mcp_client, "TOKEN_PATH", tmp_path / "absent.json")
    assert mcp_client.authenticated() is False
