"""The commander agent (§6.2 tools #1 and #3, FR-10).

The agent writes SQL that a language model chose, against the cluster the
mission is running on. §5.4 called that the one part of the console that can
fail in a way nobody recovers from on camera, and the answer to it was not
"trust the model" — it was the canned tier staying the spine, plus the layers
asserted here.

Nothing in this file calls Bedrock. The loop is driven with a scripted
`converse` so the refusals are tested deterministically; the live path is
exercised by the `db`-marked test at the bottom, which skips without a cluster.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest

from console import agent as agent_mod
from console.agent import (
    MAX_FAILURES,
    MAX_TURNS,
    VECTOR_QUERIES,
    Answer,
    CommanderAgent,
)


class FakeMCP:
    """Records what reached the transport. Anything here is something the
    read-only layers let through."""

    def __init__(self, result: Any = None):
        self.calls: list[tuple[str, dict]] = []
        self.result = result if result is not None else {"rows": [{"n": 1}]}

    def call(self, tool: str, **arguments: Any) -> Any:
        self.calls.append((tool, arguments))
        return self.result


def make_agent(
    mcp: FakeMCP | None = None, script: list[dict] | None = None
) -> CommanderAgent:
    """A CommanderAgent with both external services replaced.

    `__post_init__` builds a boto3 client and checks availability, so it is
    bypassed rather than mocked piecemeal — the thing under test is the loop and
    its refusals, not the constructor.
    """
    agent = CommanderAgent.__new__(CommanderAgent)
    agent.client = mcp or FakeMCP()
    agent.model = "test-model"
    agent.region = "us-east-1"
    agent._bedrock = FakeBedrock(script or [])
    return agent


class FakeBedrock:
    """Replays a scripted sequence of Converse responses."""

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.requests: list[dict] = []

    def converse(self, **kwargs: Any) -> dict:
        # Snapshotted, not stored by reference: the agent keeps appending to the
        # same `messages` list, so a stored reference would show every request
        # holding the final state of the conversation and assertions about what
        # turn N sent would silently be assertions about the last turn.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.script:
            return {
                "output": {
                    "message": {"role": "assistant", "content": [{"text": "done"}]}
                }
            }
        return self.script.pop(0)


def use(tool: str, **args: Any) -> dict:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "t1", "name": tool, "input": args}}
                ],
            }
        }
    }


def says(text: str) -> dict:
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}}


# --- the statement layer ----------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM tasks",
        "UPDATE robots SET status = 'idle'",
        "DROP TABLE victims",
        "WITH doomed AS (SELECT id FROM tasks) DELETE FROM tasks",
        "SELECT 1; DELETE FROM tasks",
        "SELECT 1 --\nDELETE FROM tasks",
        "INSERT INTO hazards VALUES (1)",
        "GRANT ALL ON tasks TO commander",
    ],
)
def test_a_write_never_reaches_the_cluster(sql):
    """The layer that travels with the code.

    The managed endpoint would also refuse most of these, and the `commander`
    grant would refuse them on the *other* console path — but neither of those
    is this process, and only this one is true on every deployment. A CTE
    hiding DELETE after an allowed prefix is the case worth having a test for.
    """
    mcp = FakeMCP()
    agent = make_agent(mcp, [use("select_query", query=sql), says("ok")])
    answer = agent.ask("anything")
    assert mcp.calls == [], f"{sql!r} reached the transport"
    assert any(not step.ok for step in answer.steps)


def test_a_refused_statement_is_reported_to_the_model_not_raised():
    """A refusal is information the model can act on — rewrite the query, or
    say it cannot. An exception would end the loop and lose the question."""
    agent = make_agent(
        script=[use("select_query", query="DELETE FROM tasks"), says("I cannot")]
    )
    answer = agent.ask("delete everything")
    assert answer.text == "I cannot"
    assert answer.steps[0].ok is False


def test_an_empty_query_is_refused_by_our_layer(monkeypatch):
    """Not passed through for the server to reject.

    An empty string is falsy, and a truthiness check would skip the read-only
    layer and hand the endpoint a blank statement — which comes back phrased in
    terms of a server the model cannot see, and it retries.
    """
    mcp = FakeMCP()
    agent = make_agent(mcp, [use("explain_query", query=""), says("gave up")])
    agent.ask("explain nothing")
    assert mcp.calls == []


def test_reads_do_reach_the_cluster():
    """The negative tests above would all pass if nothing ever got through."""
    mcp = FakeMCP()
    agent = make_agent(
        mcp, [use("select_query", query="SELECT id FROM robots"), says("four")]
    )
    agent.ask("how many robots")
    assert [c[0] for c in mcp.calls] == ["select_query"]


# --- the allowlist layer ----------------------------------------------------


def test_a_tool_outside_the_allowlist_is_refused_in_process():
    """The model can only call what it was declared, so this is defence in
    depth against a future edit that widens the declared list without widening
    the intent."""
    mcp = FakeMCP()
    agent = make_agent(mcp, [use("insert_rows", table="victims"), says("no")])
    answer = agent.ask("add a victim")
    assert mcp.calls == []
    assert answer.steps[0].ok is False


def test_no_write_tool_is_ever_declared_to_the_model():
    """The first layer: what Bedrock is told exists. The managed server offers
    insert_rows, create_table and create_database; none may appear here."""
    declared = {spec["toolSpec"]["name"] for spec in agent_mod._tool_specs()}
    assert not declared & {"insert_rows", "create_table", "create_database"}
    assert "select_query" in declared


# --- the loop -------------------------------------------------------------


def test_the_loop_is_bounded():
    """An unbounded tool loop is unbounded spend. The cap must produce an
    honest non-answer rather than a confident summary of a half-finished
    investigation."""
    agent = make_agent(script=[use("list_tables") for _ in range(MAX_TURNS + 4)])
    answer = agent.ask("go forever")
    assert answer.turns == MAX_TURNS
    assert "could not finish" in answer.text


def test_repeated_identical_failures_are_called_out():
    """Observed live: a model that cannot form one call re-sent it seven times
    and answered nothing. The tool result tells it to stop rather than the loop
    silently absorbing the repeats."""
    agent = make_agent(
        script=[use("select_query", query="DROP TABLE t") for _ in range(4)]
    )
    agent.ask("drop it")
    sent = agent._bedrock.requests[-1]["messages"][-1]["content"][0]["toolResult"]
    assert "failed the same way twice" in sent["content"][0]["text"]


def test_a_run_of_failures_stops_the_agent_trying_tools():
    """Some questions are genuinely unanswerable through this endpoint —
    privilege auditing is one — and the useful outcome is "I cannot, here is
    what you would run", not eight turns of discovering it."""
    script = [
        use("select_query", query=f"DELETE FROM t{i}") for i in range(MAX_FAILURES)
    ]
    agent = make_agent(script=script + [says("cannot")])
    agent.ask("audit privileges")
    sent = agent._bedrock.requests[-1]["messages"][-1]["content"][0]["toolResult"]
    assert "answer with what you have" in sent["content"][0]["text"]


def test_a_failed_tool_result_is_marked_as_an_error():
    """Sent back with status=error rather than as ordinary content, so the model
    treats a refusal as a refusal instead of as data it may quote."""
    agent = make_agent(script=[use("select_query", query="DROP TABLE t"), says("no")])
    agent.ask("drop")
    first = agent._bedrock.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert first["status"] == "error"


def test_large_result_sets_are_truncated_before_the_model_sees_them():
    """A commander summarising a fleet needs the shape, not every row. The
    truncation is reported so the model does not present a slice as the whole."""
    mcp = FakeMCP({"rows": [{"i": i} for i in range(500)]})
    agent = make_agent(mcp, [use("select_query", query="SELECT i FROM t"), says("ok")])
    agent.ask("everything")
    payload = agent._bedrock.requests[1]["messages"][-1]["content"][0]["toolResult"]
    text = payload["content"][0]["text"]
    assert '"truncated_from": 500' in text
    assert text.count('"i"') <= agent_mod.MAX_ROWS


# --- what the console shows -------------------------------------------------


def test_the_answer_carries_the_sql_it_ran():
    """FR-10's claim is that answers come out of fleet memory. The statements
    beside the answer are what make that checkable rather than asserted — the
    same reason the canned tier prints its query."""
    agent = make_agent(
        script=[use("select_query", query="SELECT id FROM robots"), says("four")]
    )
    answer = agent.ask("how many")
    assert answer.sql == ["SELECT id FROM robots"]
    assert answer.as_dict()["sql"] == ["SELECT id FROM robots"]


def test_sql_the_cluster_rejected_is_not_listed_as_sql_it_ran():
    """The panel is labelled as the statements behind the answer. A query the
    endpoint refused did not produce anything, and listing it invites a judge to
    run it and get a different result from the one shown."""

    class Refusing:
        calls: list = []

        def call(self, *_a, **_k):
            raise agent_mod.MCPError("only SELECT statements are allowed")

    agent = make_agent(
        Refusing(), [use("select_query", query="SHOW USERS"), says("cannot")]
    )
    answer = agent.ask("who are the users")
    assert answer.sql == []
    assert answer.steps[0].ok is False


def test_a_stop_with_no_text_is_not_a_blank_answer():
    """An empty summary line reads as a hung request rather than a finished
    one, and the console has no other place to say what happened."""
    agent = make_agent(
        script=[{"output": {"message": {"role": "assistant", "content": []}}}]
    )
    assert agent.ask("q").text


def test_a_loaded_skill_is_recorded_once(monkeypatch):
    """Judge-visible: "the agent used the skills repo" is a claim, and the name
    of the skill it chose is the evidence. Recorded once so a model that reads
    the same skill twice does not look like it used two."""
    monkeypatch.setattr(agent_mod.skills_mod, "load", lambda name: "body")
    monkeypatch.setattr(agent_mod.skills_mod, "by_name", lambda name: object())
    agent = make_agent(
        script=[
            use("load_skill", name="cockroachdb-sql"),
            use("load_skill", name="cockroachdb-sql"),
            says("done"),
        ]
    )
    answer = agent.ask("write me a query")
    assert answer.skills_used == ["cockroachdb-sql"]


def test_an_unknown_skill_is_not_recorded_as_used(monkeypatch):
    """Otherwise a hallucinated name inflates the claim this repo is making."""
    monkeypatch.setattr(agent_mod.skills_mod, "by_name", lambda name: None)
    agent = make_agent(script=[use("load_skill", name="invented"), says("done")])
    assert agent.ask("q").skills_used == []


def test_the_skills_catalogue_reaches_the_system_prompt():
    agent = make_agent(script=[says("hi")])
    agent.ask("hello")
    system = agent._bedrock.requests[0]["system"][0]["text"]
    assert "Agent Skills" in system or "No CockroachDB Agent Skills" in system


def test_the_schema_is_in_the_prompt_so_the_model_never_guesses_a_column():
    """The console's most visible defect was a red ✕ on the first tool call.

    The model would open with `SELECT id, goal, status FROM tasks` — a
    perfectly reasonable guess, and wrong, because a task's objective is `kind`
    plus `target_x`/`target_y`. It cost a turn, and the failed statement landed
    in the step trace where an operator reads it as the console being broken.
    Handing it every column up front is cheaper than the round trip it replaces.
    """
    agent = make_agent(script=[says("ok")])
    agent.ask("which robots are stuck")
    system = agent._bedrock.requests[0]["system"][0]["text"]

    # Every table the four memory systems are made of, with its columns.
    for table in (
        "robots(",
        "tasks(",
        "victims(",
        "hazards(",
        "observations(",
        "events(",
        "plans(",
        "mission_memories(",
    ):
        assert table in system, f"{table} columns missing from the system prompt"

    # The specific columns the failing query wanted, and the ones that replace
    # them. A schema listing that omits these does not prevent that query.
    for column in ("kind", "target_x", "target_y", "lease_expires_at", "claimed_by"):
        assert column in system

    # Named as absent, because "not in the list" is a weaker signal to a model
    # than "this does not exist".
    assert "no `goal`" in system


def test_the_prompt_schema_cannot_drift_from_the_ddl():
    """The listing claims to be complete, so it has to stay complete.

    A column added to `v1_1.sql` and not here turns the prompt into a
    confident lie — the model is told the name does not exist and will not
    query it. Parsed from the DDL rather than transcribed a second time,
    because a hand-copied list is exactly what this is guarding against.
    """
    ddl = (
        pathlib.Path(__file__).resolve().parents[1] / "schema" / "v1_1.sql"
    ).read_text()

    missing = []
    for table, body in re.findall(
        r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", ddl, re.S
    ):
        assert f"{table}(" in agent_mod.SYSTEM, f"{table} is not in the prompt"
        for line in body.splitlines():
            line = line.split("--")[0].strip()
            column = re.match(r"^([a-z_]+)\s+[A-Z]", line)
            if column and column.group(1) not in agent_mod.SYSTEM:
                missing.append(f"{table}.{column.group(1)}")
    assert not missing, f"columns in the DDL but not in the prompt: {missing}"


def test_the_model_is_told_not_to_answer_in_uuids():
    """A commander cannot act on a 36-character surrogate key. The UI elides
    them as a backstop, but an answer built around one is already useless by
    then — "the deliver_kit task at 26,10" is the answer, not its uuid."""
    agent = make_agent(script=[says("ok")])
    agent.ask("what is m1 doing")
    system = agent._bedrock.requests[0]["system"][0]["text"]
    assert "Never write a uuid into your answer" in system
    # Robot ids survive: they are the names on the map, and an answer that
    # cannot say "s1" is worse than one full of uuids.
    assert "s1, l1, m1" in system


def test_the_mission_scopes_the_question_but_not_semantic_memory():
    """Tactics are global across missions by design (§4.0). A prompt that says
    "scope everything to this mission" would defeat the one memory system whose
    whole point is transferring between them."""
    agent = make_agent(script=[says("ok")])
    agent.ask("what do we know", mission_id="m-1")
    system = agent._bedrock.requests[0]["system"][0]["text"]
    assert "m-1" in system
    assert "Do NOT scope" in system


# --- the vector-plan tool ---------------------------------------------------


def test_the_vector_plan_tool_covers_both_real_queries():
    """The two vector searches this submission claims. Transcribed from
    fleetmem/client.py so a plan shown in the console is the plan the mission
    gets, rather than a simplified one that happens to use the index."""
    assert set(VECTOR_QUERIES) == {"mission_memories", "observations"}


def test_the_reconcile_gate_query_keeps_the_filters_that_cost_it_the_index():
    """The gate's FULL SCAN is a deliberate trade, and it is only deliberate if
    the query shown still carries the filters that cause it. A tidied-up version
    would use the index and quietly misrepresent the design."""
    sql, _ = VECTOR_QUERIES["observations"]
    for clause in ("kind =", "pos_x BETWEEN", "pos_y BETWEEN", "mission_id ="):
        assert clause in sql


def test_the_recall_query_carries_no_scope_at_all():
    """Any WHERE would partition exactly the knowledge tactical recall exists to
    generalise, and would also be the thing that stops the index being used."""
    sql, _ = VECTOR_QUERIES["mission_memories"]
    assert "WHERE" not in sql.upper()


def test_the_vector_plan_tool_builds_a_full_width_vector():
    """512 to match observations.embedding VECTOR(512). A short literal is a
    type error the model cannot diagnose from inside the loop."""
    mcp = FakeMCP({"rows": [{"info": "vector search"}]})
    agent = make_agent(
        mcp, [use("explain_vector_search", table="mission_memories"), says("yes")]
    )
    agent.ask("is the index used")
    ((_tool, args),) = mcp.calls
    # Counted per literal, not across the statement: the recall query
    # interpolates the vector twice (once to select the distance, once to order
    # by it), so a whole-string count is double and proves nothing about width.
    literals = re.findall(r"\[([0-9.,]+)\]", args["query"])
    assert literals, "no vector literal in the statement"
    assert all(len(lit.split(",")) == 512 for lit in literals)
    assert "VECTOR(512)" in args["query"]


def test_the_vector_plan_tool_shows_the_query_without_the_literal():
    """512 floats in the console would push the plan off the screen, and the
    literal is the least interesting part of the statement."""
    mcp = FakeMCP({"rows": [{"info": "vector search"}]})
    agent = make_agent(
        mcp, [use("explain_vector_search", table="mission_memories"), says("yes")]
    )
    answer = agent.ask("is the index used")
    assert "512-float probe" in answer.sql[0]
    assert "0.01,0.01" not in answer.sql[0]


def test_an_unknown_vector_table_is_refused_with_the_options():
    agent = make_agent(
        script=[use("explain_vector_search", table="robots"), says("no")]
    )
    answer = agent.ask("plan for robots")
    assert answer.steps[0].ok is False


# --- serialisation ----------------------------------------------------------


def test_the_answer_serialises_for_the_console():
    answer = Answer(text="hi", turns=2, elapsed_s=1.234)
    data = answer.as_dict()
    assert data["text"] == "hi"
    assert data["elapsed_s"] == 1.23
    assert isinstance(data["steps"], list)


# --- live -------------------------------------------------------------------


@pytest.mark.db
def test_the_agent_answers_from_the_live_cluster():
    """The one test that proves the claim rather than the guardrails.

    Skips without Bedrock credentials or an MCP login, which is the same
    condition under which the console falls back to the canned questions.
    """
    from console.agent import availability

    state = availability()
    if not state["available"]:
        pytest.skip(f"agent unavailable: {state['reason']}")

    answer = CommanderAgent().ask("How many robots are in the fleet?")
    assert answer.text
    assert answer.sql, "the agent answered without reading anything"
    assert any(step.ok for step in answer.steps)
