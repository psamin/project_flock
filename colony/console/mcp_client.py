"""A client for the CockroachDB Cloud Managed MCP Server (§6.2 required tool #1).

Until now the Managed MCP Server was a config snippet this repo could *print*
(`infra/mcp.py`) and a posture it could assert. Nothing in a running mission
went through it. This module is the runtime path: the commander agent
(`console/agent.py`) reads fleet memory by calling MCP tools over HTTP, so the
tool is load-bearing during the demo rather than at development time.

Three things about the managed endpoint are worth stating here, because all
three contradict something the repo believed before it was called for real:

    identity   MCP connects as `managed-mcp`, its own service identity — NOT as
               `commander`. `SELECT current_user` through the server says so.
               The `CRDB_SQL_USER` key in the config snippet is inert. So the
               §3.5 claim "read-only by grant, not by setting" describes the
               psycopg path in `reader.py` and *not* this one, and the two must
               not be described as if one story covered both.

    read-only  On this path it is enforced by the server: `select_query` rejects
               anything that is not a SELECT, and `information_schema` is
               blocked outright. That is a real control, but it is the server's,
               not ours — which is exactly why `assert_read_only` still runs
               over every statement before it leaves this process, and why
               `TOOLS` below is an allowlist rather than whatever the server
               happens to expose. The server also exposes `insert_rows`,
               `create_table` and `create_database`; the agent is never shown
               them.

    cluster id must be passed as a tool *argument*. The `?cluster=<id>` query
               parameter in the published config snippet is not read — calls
               without the argument fail with "cluster_id not provided". This
               client injects it into every call so no caller has to remember.

## Authentication

OAuth 2.1, and the endpoint advertises `authorization_code` and `refresh_token`
only — there is no `client_credentials` grant, so a server-side process cannot
mint a token from a secret. The shape that works is a one-time human login whose
refresh token is then used headlessly:

    uv run python -m console.mcp_client login     # once, opens a browser
    uv run python -m console.mcp_client check     # any time, no browser

The refresh token lives in `~/.colony/mcp-token.json` (0600), deliberately
outside the repository so no .gitignore rule stands between it and a commit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

MANAGED_ENDPOINT = "https://cockroachlabs.cloud/mcp"
REGISTER_URL = f"{MANAGED_ENDPOINT}/oauth/register"
AUTHORIZE_URL = f"{MANAGED_ENDPOINT}/oauth/authorize"
TOKEN_URL = f"{MANAGED_ENDPOINT}/oauth/token"

# `mcp:read` alone. The server offers `mcp:write` and there is no reason for the
# commander to hold it: §6.3 says leave writes off, and a scope never requested
# is a stronger version of that than a scope requested and unused.
SCOPE = "mcp:read"

CLIENT_NAME = "colony-commander"

# Outside the repo on purpose — see the module docstring.
TOKEN_PATH = Path(
    os.environ.get("COLONY_MCP_TOKEN_PATH", Path.home() / ".colony" / "mcp-token.json")
)

# The read-only allowlist the agent is given. The server exposes more than this,
# including three write tools; anything not named here is unreachable from the
# agent loop because it is never put in the tool list Bedrock sees.
TOOLS = (
    "select_query",
    "explain_query",
    "get_table_schema",
    "list_tables",
    "show_running_queries",
    # SHOW is introspection, and the server runs it on a path that accepts
    # nothing else. It is here because two things the console should be able to
    # do need it and cannot be expressed as a SELECT: `SHOW INDEXES FROM
    # observations`, which §2 of docs/setup-testing.md names as the way to check
    # the vector index is real, and `SHOW GRANTS`, without which the
    # hardening-user-privileges skill has nothing to read.
    "show_statement",
)

# Refresh this long before expiry rather than on it, so a mission that starts
# with 40 seconds left on the token does not fail its first question.
_EXPIRY_MARGIN_S = 120


class MCPError(RuntimeError):
    """The managed endpoint refused, or is not authenticated yet."""


class NotAuthenticated(MCPError):
    """No usable token. Run `python -m console.mcp_client login`."""


# --- token storage --------------------------------------------------------


def _read_tokens() -> dict[str, Any]:
    try:
        return json.loads(TOKEN_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_tokens(data: dict[str, Any]) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Written 0600 before anything is in it: creating world-readable and
    # chmod-ing after leaves a window where the refresh token is on disk and
    # readable, which is the whole thing we are trying to avoid.
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)


# --- HTTP -----------------------------------------------------------------


def _post(url: str, payload: dict[str, Any], *, form: bool = False, token: str | None = None) -> dict[str, Any]:
    """POST JSON (or form-encoded, which the token endpoint requires)."""
    if form:
        body = urllib.parse.urlencode(payload).encode()
        content_type = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(payload).encode()
        content_type = "application/json"
    headers = {"Content-Type": content_type, "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return _decode(resp.read().decode(), resp.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as exc:  # pragma: no cover - network-dependent
        detail = exc.read().decode()[:400]
        raise MCPError(f"{url} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network-dependent
        raise MCPError(f"{url} unreachable: {exc.reason}") from exc


def _decode(text: str, content_type: str) -> dict[str, Any]:
    """Parse a response body that may be JSON or a one-event SSE stream.

    The MCP HTTP transport is allowed to answer either way for the same request,
    so a client that only handles JSON works until the day the server decides to
    stream, which is not a failure anybody wants to debug live.
    """
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise MCPError("event stream carried no data frame")
    return json.loads(text) if text.strip() else {}


# --- the one-time login ---------------------------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _CallbackHandler.code = (params.get("code") or [None])[0]
        _CallbackHandler.state = (params.get("state") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font:16px system-ui;padding:3rem'>"
            b"<h2>colony-commander is authorised</h2>"
            b"<p>The commander agent can now read fleet memory through the "
            b"CockroachDB Managed MCP Server. You can close this tab.</p>"
            b"</body></html>"
        )

    def log_message(self, *args: Any) -> None:  # noqa: A002 - silence the server
        pass


def login(port: int = 8765, open_browser: bool = True) -> dict[str, Any]:
    """Register a client, run the PKCE authorization-code flow, store the tokens.

    Interactive by necessity — the endpoint advertises no non-interactive grant.
    Run once; `access_token()` refreshes without a browser from then on.
    """
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    registration = _post(
        REGISTER_URL,
        {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        },
    )
    client_id = registration.get("client_id")
    if not client_id:
        raise MCPError(f"registration returned no client_id: {registration}")

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(24)

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    print(f"authorise colony-commander:\n  {url}\n")
    if open_browser:
        webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    if not _CallbackHandler.code:
        raise MCPError("no authorization code came back within 5 minutes")
    # Checked rather than assumed: without it a forged callback can swap in a
    # code minted for a different session.
    if _CallbackHandler.state != state:
        raise MCPError("state mismatch on the callback; refusing the code")

    tokens = _post(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": _CallbackHandler.code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        form=True,
    )
    stored = _store(tokens, client_id)
    print(f"stored -> {TOKEN_PATH}")
    return stored


def _store(tokens: dict[str, Any], client_id: str) -> dict[str, Any]:
    data = _read_tokens()
    data["client_id"] = client_id
    data["access_token"] = tokens.get("access_token", "")
    # A refresh response may legitimately omit refresh_token, meaning "keep
    # using the one you have". Overwriting with the absent value would log the
    # agent out on its first successful refresh.
    if tokens.get("refresh_token"):
        data["refresh_token"] = tokens["refresh_token"]
    data["expires_at"] = time.time() + float(tokens.get("expires_in", 3600))
    _write_tokens(data)
    return data


def access_token() -> str:
    """A valid bearer token, refreshed headlessly when it has aged out."""
    data = _read_tokens()
    if not data.get("access_token"):
        raise NotAuthenticated(
            "no MCP token. Run: uv run python -m console.mcp_client login"
        )
    if time.time() < data.get("expires_at", 0) - _EXPIRY_MARGIN_S:
        return data["access_token"]
    if not data.get("refresh_token"):
        raise NotAuthenticated(
            "MCP token expired and no refresh token. Run: "
            "uv run python -m console.mcp_client login"
        )
    refreshed = _post(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"],
            "client_id": data["client_id"],
        },
        form=True,
    )
    return _store(refreshed, data["client_id"])["access_token"]


def authenticated() -> bool:
    """Whether a call would work, without making one."""
    try:
        access_token()
        return True
    except MCPError:
        return False


# --- the client -----------------------------------------------------------


@dataclass
class MCPClient:
    """JSON-RPC over the managed HTTP endpoint, with the cluster id supplied.

    `database` and `cluster_id` are injected into every call rather than left to
    the caller: the agent should be reasoning about the fleet, not remembering
    which of the server's arguments this deployment happens to require.
    """

    cluster_id: str = field(
        default_factory=lambda: os.environ.get("CRDB_CLUSTER_ID", "")
    )
    database: str = field(
        default_factory=lambda: os.environ.get("COLONY_DATABASE", "colony")
    )
    endpoint: str = MANAGED_ENDPOINT
    _id: int = field(default=0, init=False)
    calls: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.cluster_id:
            raise MCPError(
                "no cluster id — set CRDB_CLUSTER_ID (Cloud console -> connect)"
            )

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        result = _post(self.endpoint, payload, token=access_token())
        if "error" in result:
            raise MCPError(str(result["error"].get("message", result["error"])))
        return result.get("result", {})

    def list_tools(self) -> list[str]:
        """What the server actually offers. Not what the agent gets — see TOOLS."""
        return [t["name"] for t in self._rpc("tools/list", {}).get("tools", [])]

    def call(self, tool: str, **arguments: Any) -> Any:
        """Invoke one allowlisted tool and return its decoded payload."""
        if tool not in TOOLS:
            raise MCPError(f"{tool!r} is not in the commander's read-only allowlist")
        arguments.setdefault("cluster_id", self.cluster_id)
        arguments.setdefault("database", self.database)
        self.calls += 1
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        return _content(result)


def _content(result: dict[str, Any]) -> Any:
    """Unwrap MCP's content envelope to the payload the agent should read.

    `isError` is checked rather than ignored: the server reports a refused query
    as a *successful* JSON-RPC call carrying an error payload, so a client that
    only checks the RPC layer hands the model an error string and lets it narrate
    it as data.
    """
    blocks = result.get("content", [])
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if result.get("isError"):
        raise MCPError(text or "the managed endpoint refused the call")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Managed MCP Server client (§6.2)")
    parser.add_argument("action", choices=["login", "check", "tools"])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.action == "login":
        login(port=args.port)
        return 0

    if not authenticated():
        print("not authenticated. Run: uv run python -m console.mcp_client login")
        return 1

    client = MCPClient()
    if args.action == "tools":
        offered = client.list_tools()
        print(f"server offers {len(offered)}: {', '.join(sorted(offered))}")
        withheld = sorted(set(offered) - set(TOOLS))
        print(f"agent is given {len(TOOLS)}: {', '.join(TOOLS)}")
        print(f"withheld from the agent: {', '.join(withheld) or 'none'}")
        return 0

    who = client.call("select_query", query="SELECT current_user AS who")
    tables = client.call("list_tables")
    print(f"authenticated ok | connects as: {who['rows'][0]['who']}")
    print(f"tables visible: {len(tables['rows'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
