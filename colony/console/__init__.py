"""Lane 4 — the commander console (FR-10, §5.1, §6.2).

A human asks a question about the mission and gets an answer read straight out
of fleet memory. §5.1 scopes it to five canned demo questions, and §6.2 names
the transport: CockroachDB's Managed MCP Server, read-only.

The two halves are separable on purpose, because they have different blockers:

    questions.py   the five questions as read-only SQL, including the
                   `plans x observations` join behind "why did robot X do Y".
                   This is the substance, and it runs against any cluster.
    reader.py      the read-only execution path — the posture that makes
                   "the console cannot write" true rather than configured.

The managed endpoint itself (`cockroachlabs.cloud/mcp`) needs a CockroachDB
Cloud cluster, which is the one item still parked in TODO.md. `infra/mcp.py`
generates the config for it and checks the same read-only posture locally, so
the hookup is a connection-string swap rather than unwritten work.
"""

from console.questions import QUESTIONS, Answer, Question, answer, catalog
from console.reader import NotReadOnly, ReadOnlyReader

__all__ = [
    "QUESTIONS",
    "Answer",
    "NotReadOnly",
    "Question",
    "ReadOnlyReader",
    "answer",
    "catalog",
]
