"""The commander agent: a Bedrock brain with MCP hands (§6.2 tools #1 and #3).

The canned questions in `questions.py` stay exactly as they are, and remain
the demo's spine — §5.4 puts reliability first and a fixed statement a judge can
read and re-run is still the stronger artefact. This module is the tier above
them: free-form questions, answered by a model that reads live fleet memory
through the CockroachDB Managed MCP Server and equips itself from the
CockroachDB Agent Skills repo on the way.

That makes both tools load-bearing *at runtime*. Before this, the MCP server was
a config snippet the repo could print and the skills repo was a line in the PRD.

## What stops this writing to the cluster

Three layers, and they are genuinely different mechanisms rather than the same
one restated — worth separating, because the repo previously described them as
if one story covered every path:

    allowlist  the agent is handed five read tools (`mcp_client.TOOLS`). The
               managed server also exposes `insert_rows`, `create_table` and
               `create_database`; they are never in the tool list Bedrock sees,
               so no amount of prompting reaches them.
    statement  every `query` the model writes goes through `assert_read_only`
               before it leaves this process — the same check `reader.py` uses,
               imported rather than reimplemented so the two cannot drift.
    server     the managed endpoint rejects non-SELECT in `select_query` and
               blocks `information_schema` outright.

Note which one is *absent*: the `commander` grant. MCP connects as its own
service identity, `managed-mcp`, so the "read-only by grant" property that
covers the psycopg console does not cover this path. The first two layers are
ours precisely because the third is somebody else's.

## Cost

Runs on the same model as robot planning (Haiku 4.5) and is bounded by
`MAX_TURNS`, so a runaway tool loop costs a fixed and small number of calls. It
is operator-initiated — one question, one loop — and sits outside the §3.5
per-robot planning cap, which governs the tick loop and must not be spent by
somebody typing in a console.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from bedrock.adapter import PLAN_MODEL, has_credentials
from console import skills as skills_mod
from console.mcp_client import TOOLS, MCPClient, MCPError, authenticated
from console.reader import NotReadOnly, assert_read_only

# A tool loop that has not answered in this many rounds is not going to. Bounded
# rather than open so an operator question cannot quietly become an unbounded
# spend, and low enough that the console stays a console.
MAX_TURNS = 8

# Failed tool calls tolerated before the model is told to stop and answer from
# what it has. Some questions are genuinely unanswerable through this endpoint —
# privilege auditing is — and the useful outcome there is a clear "I cannot,
# here is what you would run", not eight turns of trying.
MAX_FAILURES = 4

# Rows returned to the model, per call. The managed server defaults to 25 and
# caps at 10000; a commander summarising a fleet needs to *see* the shape, not
# carry every row into its context.
MAX_ROWS = 40

SKILL_TOOL = "load_skill"
VECTOR_PLAN_TOOL = "explain_vector_search"

# The two vector queries the fleet actually runs, transcribed from
# `fleetmem/client.py` so a plan shown in the console is the plan the mission
# gets. A model cannot ask for these itself: the literal is 512 floats, which
# does not fit in a tool-call argument and comes back truncated — that is the
# whole reason this tool exists rather than leaving it to `explain_query`.
#
# `%s` is not used: this is a literal EXPLAIN with no user input in it. The
# vector is generated here, the SQL is fixed text, and nothing the operator or
# the model typed reaches either.
VECTOR_QUERIES = {
    "mission_memories": (
        "SELECT id, embedding <=> {vec} AS distance FROM mission_memories"
        " ORDER BY embedding <=> {vec} LIMIT 3",
        "tactical recall — unscoped across every mission and map (§4.0)",
    ),
    "observations": (
        "SELECT id, embedding <=> {vec} AS distance FROM observations"
        " WHERE mission_id = '00000000-0000-0000-0000-000000000000'"
        "   AND embedding IS NOT NULL AND kind = 'victim'"
        "   AND pos_x BETWEEN 9 AND 19 AND pos_y BETWEEN 4 AND 14"
        " ORDER BY embedding <=> {vec} LIMIT 5",
        "the reconcile gate — scoped to one mission and a 5-tile box",
    ),
}

SYSTEM = """You are the commander console for Colony, a heterogeneous robot \
fleet running a disaster-relief mission. You answer an incident commander's \
questions by reading the fleet's memory in CockroachDB. You are read-only and \
cannot change the mission.

The schema is four memory systems:
  working     robots, tasks, victims, hazards — what is true right now
  episodic    observations (512-dim VECTOR embedding) — what we experienced
  provenance  plans, events — why we acted. plans.based_on holds the observation
              ids that were in the prompt when that decision was made, so "why
              did robot X do Y" is a join and not a guess.
  semantic    mission_memories (512-dim VECTOR) — tactics learned across
              missions, deliberately holding no coordinates or sector names.

Every column, so you never have to guess one. This list is complete: a name that
is not here does not exist, and querying it costs you a turn for nothing.

  robots(id STRING pk, role, pos_x, pos_y, battery, status, current_task UUID,
         heartbeat_at)
  tasks(id UUID pk, mission_id UUID, kind, target_x, target_y, priority, status,
        depends_on UUID[], claimed_by, claimed_at, lease_expires_at, done_at)
  victims(id UUID pk, mission_id, pos_x, pos_y, state, vitals_deadline,
          reported_by, confidence)
  hazards(id UUID pk, mission_id, kind, area JSONB, severity, active BOOL)
  observations(id UUID pk, mission_id, robot_id, kind, pos_x, pos_y,
               payload JSONB, embedding VECTOR(512), confidence, sightings,
               observed_at)
  events(id UUID pk, mission_id, at, actor, verb, detail JSONB)
  plans(id UUID pk, mission_id, robot_id, at, trigger, chosen JSONB, rationale,
        based_on UUID[])
  mission_memories(id UUID pk, mission_id, situation, lesson,
                   embedding VECTOR(512), evidence JSONB, confidence,
                   times_recalled, created_at)

Traps in that list, because they are where a sensible guess is wrong:
- There is no `goal`, `name`, `description`, `target` or `position` column
  anywhere. A task's objective is `kind` plus `target_x`/`target_y`; a robot's
  position is `pos_x`/`pos_y`. Coordinates are always two INT columns, never a
  point or a pair.
- What a robot decided lives in `plans.chosen` (JSONB) and `plans.rationale`,
  not on `tasks` and not on `robots`.
- `robots.status` is the robot's own state. `tasks.status` is the work's. They
  are different columns with different vocabularies; do not join on them.
- `events.verb` is the event type — there is no `type` or `kind` on events.

Ownership is a lease: tasks.claimed_by with tasks.lease_expires_at. An expired
lease is claimable by anyone, so "stuck" usually means an expired lease or a
dependency that has not completed, not a crashed robot.

Two facts about the vector queries, so you do not misreport them as defects:
- Tactical recall over mission_memories uses mm_situation_idx and its plan says
  "vector search". That is the index doing its job.
- The reconcile gate over observations is a FULL SCAN **by design**, and the
  optimizer's index recommendation for it should not be endorsed. The gate
  constrains kind and a 5-tile box, neither a prefix column of the index, and
  CockroachDB will not serve an approximate top-k it then has to filter. An
  approximate scan that misses a duplicate would make the fleet dispatch two
  robots to one victim; an exact scan over one mission's observations will not.
  If asked, explain the trade — do not call it a performance problem.

What the managed endpoint will not let you do, so you do not spend turns
rediscovering it:
- SHOW USERS, SHOW GRANTS and SHOW SYSTEM GRANTS are refused. Privilege
  auditing is not reachable from this console; say so and name the statements
  an operator would run themselves.
- SHOW is accepted only for SCHEMAS, INDEXES, REGIONS, CONSTRAINTS and CREATE
  TABLE, and only through show_statement — never wrapped in a SELECT.
- information_schema and crdb_internal are blocked.

How to work:
- The column list above is complete, so write your query from it directly. Call
  get_table_schema only to check an index or a default, never to look up a
  column name you were already given.
- Consult a skill when one matches what you are doing. Read its description in
  the catalogue below and call load_skill before you rely on it.
- Prefer one good query over several speculative ones.
- All expiry comparisons must use the database's now(), never a literal time.
- You can read the PAST. `SELECT ... AS OF SYSTEM TIME '-30s'` returns what the
  fleet believed then, which is how to answer "what changed" or "did it already
  know" without any history table. The canned `beliefs_30s_ago` question does
  this; you may use it anywhere. Stay inside the garbage-collection window —
  minutes, not hours.
- Answer the commander in at most four sentences, in plain language, leading
  with the answer.
- Name robots by their short id — s1, l1, m1 — always. Those are the names on
  the map and the names an operator says out loud.
- Never write a uuid into your answer. A commander cannot act on
  "249456ae-143d-405a-b59c-2be85247fd56"; they can act on "the deliver_kit task
  at 26,10". Identify tasks by kind and target, victims by position, missions
  not at all. Select uuid columns only when you need them to join.
- Write plain prose. No markdown: no **bold**, no bullet lists, no headings.
  The console renders your answer as text, so markup arrives as literal
  asterisks in front of whoever is watching.
- If the data does not support an answer, say so. Do not fill the gap.
- You are reading a live mission; rows change between calls.
"""


@dataclass
class Step:
    """One tool call, kept so the UI can show its work."""

    tool: str
    argument: str
    ok: bool
    detail: str = ""


@dataclass
class Answer:
    """What the console renders."""

    text: str
    steps: list[Step] = field(default_factory=list)
    sql: list[str] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)
    turns: int = 0
    elapsed_s: float = 0.0
    model: str = PLAN_MODEL

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "sql": self.sql,
            "skills_used": self.skills_used,
            "turns": self.turns,
            "elapsed_s": round(self.elapsed_s, 2),
            "model": self.model,
            "steps": [
                {"tool": s.tool, "argument": s.argument, "ok": s.ok, "detail": s.detail}
                for s in self.steps
            ],
        }


class AgentUnavailable(RuntimeError):
    """Bedrock or MCP is not wired up. The caller falls back to canned questions."""


def availability() -> dict[str, Any]:
    """Why the agent is or is not usable, in terms the UI can show a judge."""
    creds = has_credentials()
    mcp_ok = authenticated()
    # Checked here rather than left to the constructor. A token proves somebody
    # logged in; it does not prove this deployment knows which cluster to ask,
    # and without the id every call fails with "cluster_id not provided". A
    # console that advertises the agent and then fails on the first question is
    # worse than one that says up front what is missing.
    cluster = bool(os.environ.get("CRDB_CLUSTER_ID"))
    ok = creds and mcp_ok and cluster
    if not creds:
        reason = "no AWS credentials"
    elif not mcp_ok:
        reason = "MCP not authenticated — run: uv run python -m console.mcp_client login"
    elif not cluster:
        reason = "no CRDB_CLUSTER_ID — take it from the Cloud console's connect dialog"
    else:
        reason = ""
    return {
        "available": ok,
        "bedrock": creds,
        "mcp": mcp_ok,
        "cluster": cluster,
        "skills": len(skills_mod.catalog()),
        "model": PLAN_MODEL,
        "reason": reason,
    }


def _tool_specs() -> list[dict[str, Any]]:
    """The tools Bedrock is told about. The allowlist, plus skill loading.

    Written out rather than derived from the server's `tools/list`, so adding a
    tool to the agent is an edit to this file and cannot happen by the managed
    endpoint growing one.
    """
    cluster = {"database": {"type": "string", "description": "always 'colony'"}}
    specs: list[dict[str, Any]] = [
        {
            "name": "select_query",
            "description": (
                "Run one read-only SELECT against the live mission cluster. "
                "Add your own LIMIT; the server imposes one otherwise."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    **cluster,
                    "query": {"type": "string", "description": "a single SELECT"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "explain_query",
            "description": (
                "Show the execution plan for a query without running it. Use to "
                "check whether a vector search uses its index."
            ),
            "schema": {
                "type": "object",
                "properties": {**cluster, "query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "get_table_schema",
            "description": "Columns and indexes for one table.",
            "schema": {
                "type": "object",
                "properties": {
                    **cluster,
                    "table": {"type": "string"},
                },
                "required": ["table"],
            },
        },
        {
            "name": "list_tables",
            "description": "Every table in the mission database, with row counts.",
            "schema": {"type": "object", "properties": {**cluster}},
        },
        {
            "name": "show_running_queries",
            "description": "Statements currently executing on the cluster.",
            "schema": {"type": "object", "properties": {**cluster}},
        },
        {
            "name": "show_statement",
            "description": (
                "Run a SHOW statement for introspection — SHOW INDEXES FROM "
                "<table>, SHOW GRANTS, SHOW CREATE TABLE <table>, SHOW "
                "CONSTRAINTS FROM <table>. Use this for anything a SELECT "
                "cannot express; wrapping SHOW inside a SELECT is rejected."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    **cluster,
                    "query": {"type": "string", "description": "a single SHOW statement"},
                },
                "required": ["query"],
            },
        },
        {
            "name": VECTOR_PLAN_TOOL,
            "description": (
                "Show the query plan for one of the fleet's two real vector "
                "searches: 'mission_memories' (tactical recall) or "
                "'observations' (the reconcile gate). Use this instead of "
                "writing the query yourself — the 512-float literal does not "
                "fit in a tool argument. Look for 'vector search' versus "
                "'FULL SCAN' in the plan."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "enum": sorted(VECTOR_QUERIES),
                    }
                },
                "required": ["table"],
            },
        },
        {
            "name": SKILL_TOOL,
            "description": (
                "Read one CockroachDB Agent Skill in full. Call this when a "
                "skill's description in the catalogue matches what you are "
                "about to do."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "the skill's name"}
                },
                "required": ["name"],
            },
        },
    ]
    return [
        {
            "toolSpec": {
                "name": spec["name"],
                "description": spec["description"],
                "inputSchema": {"json": spec["schema"]},
            }
        }
        for spec in specs
    ]


@dataclass
class CommanderAgent:
    """One question in, one answer out. Not a chat — each ask starts clean.

    Stateless on purpose: the mission underneath is changing at 4 Hz, so a
    conversation carried across questions would be reasoning partly over rows
    that no longer exist.
    """

    client: MCPClient | None = None
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "us-east-1"))
    model: str = PLAN_MODEL

    def __post_init__(self) -> None:
        state = availability()
        if not state["available"]:
            raise AgentUnavailable(state["reason"])
        self.client = self.client or MCPClient()
        import boto3  # lazily, exactly as bedrock/adapter.py does

        self._bedrock = boto3.client("bedrock-runtime", region_name=self.region)

    # --- tool execution ---------------------------------------------------

    def _run_tool(self, name: str, args: dict[str, Any], answer: Answer) -> tuple[str, bool]:
        """Execute one tool call. Returns (payload_for_model, ok)."""
        if name == SKILL_TOOL:
            skill_name = str(args.get("name", ""))
            body = skills_mod.load(skill_name)
            found = skills_mod.by_name(skill_name) is not None
            if found and skill_name not in answer.skills_used:
                answer.skills_used.append(skill_name)
            answer.steps.append(
                Step(tool=SKILL_TOOL, argument=skill_name, ok=found,
                     detail="" if found else "no such skill")
            )
            return body, found

        if name == VECTOR_PLAN_TOOL:
            table = str(args.get("table", ""))
            entry = VECTOR_QUERIES.get(table)
            if entry is None:
                answer.steps.append(
                    Step(tool=name, argument=table, ok=False, detail="unknown table")
                )
                return f"no vector query for {table!r}. Try: {', '.join(sorted(VECTOR_QUERIES))}", False
            template, note = entry
            # A probe vector, not a meaningful one: EXPLAIN does not execute, so
            # the plan depends on the query's shape and the index, never on
            # which point in the space we asked about.
            sql = template.format(vec="'[" + ",".join(["0.01"] * 512) + "]'::VECTOR(512)")
            answer.sql.append(f"-- {note}\nEXPLAIN {template.format(vec='<512-float probe>')}")
            try:
                result = self.client.call("explain_query", query=sql)
            except MCPError as exc:
                answer.steps.append(Step(tool=name, argument=table, ok=False, detail=str(exc)))
                return f"the cluster refused that: {exc}", False
            plan = "\n".join(
                r.get("info", "") for r in (result.get("rows") or []) if isinstance(r, dict)
            )
            answer.steps.append(Step(tool=name, argument=table, ok=True, detail=note))
            return f"{note}\n\n{plan}", True

        if name not in TOOLS:
            # Unreachable through the declared tool list; kept because a model
            # that hallucinates a tool name should get a refusal it can read
            # rather than an exception that ends the loop.
            answer.steps.append(Step(tool=name, argument="", ok=False, detail="not allowed"))
            return f"{name!r} is not available to you.", False

        # `in args` rather than a truthiness test: a tool call that arrives with
        # an empty query is a malformed call, and it must be refused by *our*
        # layer with a message the model can act on. Letting it through because
        # "" is falsy hands the managed endpoint a blank statement and returns
        # an error phrased in terms of a server the model cannot see.
        query = args.get("query")
        if "query" in args:
            try:
                assert_read_only(str(query or ""))
            except NotReadOnly as exc:
                answer.steps.append(
                    Step(tool=name, argument=str(query or ""), ok=False, detail=str(exc))
                )
                return f"refused: {exc}", False

        try:
            result = self.client.call(name, **{k: v for k, v in args.items() if k != "database"})
        except MCPError as exc:
            answer.steps.append(
                Step(tool=name, argument=str(query or ""), ok=False, detail=str(exc))
            )
            return f"the cluster refused that: {exc}", False

        # Recorded only once the cluster has actually run it. The console labels
        # this panel as the SQL behind the answer, and a statement the endpoint
        # rejected is not that — it belongs in the step trace, where the ✕ says
        # what happened to it.
        if query is not None:
            answer.sql.append(str(query))

        if isinstance(result, dict) and isinstance(result.get("rows"), list):
            rows = result["rows"]
            if len(rows) > MAX_ROWS:
                result = {"rows": rows[:MAX_ROWS], "truncated_from": len(rows)}
        payload = json.dumps(result, default=str)
        answer.steps.append(
            Step(tool=name, argument=str(query or args.get("table", "")), ok=True,
                 detail=f"{len(payload)} chars")
        )
        return payload, True

    # --- the loop ---------------------------------------------------------

    def ask(self, question: str, mission_id: str | None = None) -> Answer:
        """Answer one free-form question from live fleet memory."""
        answer = Answer(text="", model=self.model)
        started = time.monotonic()

        system = SYSTEM + "\n\n" + skills_mod.catalog_prompt()
        if mission_id:
            system += (
                f"\n\nThe mission running right now is {mission_id}. Scope working, "
                "episodic and provenance queries to it. Do NOT scope "
                "mission_memories — those tactics are global across missions by "
                "design, and filtering them defeats their purpose."
            )

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"text": question}]}
        ]
        tool_config = {"tools": _tool_specs()}
        repeats: dict[tuple[str, str], int] = {}
        failures = 0

        for turn in range(MAX_TURNS):
            answer.turns = turn + 1
            response = self._bedrock.converse(
                modelId=self.model,
                messages=messages,
                system=[{"text": system}],
                toolConfig=tool_config,
                inferenceConfig={"maxTokens": 1400, "temperature": 0.0},
            )
            message = response["output"]["message"]
            messages.append(message)

            uses = [c["toolUse"] for c in message.get("content", []) if "toolUse" in c]
            if not uses:
                answer.text = "".join(
                    c.get("text", "") for c in message.get("content", [])
                ).strip()
                if not answer.text:
                    # A stop with neither tools nor text. Rare, but an empty
                    # summary line in the console reads as a hung request rather
                    # than as a finished one.
                    answer.text = (
                        "The model stopped without answering. The steps below are "
                        "what it read; try asking more specifically."
                    )
                break

            results = []
            for use in uses:
                payload, ok = self._run_tool(use["name"], use.get("input", {}) or {}, answer)
                if not ok:
                    failures += 1
                    # A model that gets the same refusal twice will usually get
                    # it a third time; without this it spends the whole budget
                    # re-sending one malformed call and answers nothing. Telling
                    # it so in the tool result is more useful than silently
                    # capping, because it can then answer from what it has.
                    signature = (use["name"], payload[:120])
                    repeats[signature] = repeats.get(signature, 0) + 1
                    if repeats[signature] >= 2:
                        payload += (
                            "\n\nThis call has now failed the same way twice. Do "
                            "not retry it — either take a different approach or "
                            "answer with what you already have, saying what you "
                            "could not determine."
                        )
                results.append(
                    {
                        "toolResult": {
                            "toolUseId": use["toolUseId"],
                            "content": [{"text": payload}],
                            **({} if ok else {"status": "error"}),
                        }
                    }
                )
            if failures >= MAX_FAILURES:
                # Distinct from the repeat guard: that one catches a model
                # re-sending one bad call, this catches it working through a
                # list of things the endpoint will not do. Both end with the
                # model answering from what it has rather than the loop simply
                # running out, which is what produces "I could not finish"
                # instead of the useful partial answer it could have given.
                results[-1]["toolResult"]["content"][0]["text"] += (
                    f"\n\n{failures} calls have now failed. Stop trying tools "
                    "and answer with what you have, stating plainly what you "
                    "could not determine and why."
                )
            messages.append({"role": "user", "content": results})
        else:
            # Fell out of the loop still calling tools. Said plainly rather than
            # presented as an answer, because a half-finished investigation
            # narrated confidently is the worst thing a console can do.
            answer.text = (
                f"I could not finish within {MAX_TURNS} steps. "
                f"What I read is listed below."
            )

        answer.elapsed_s = time.monotonic() - started
        return answer
