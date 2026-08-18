# Tools and services — what we used, and what the agent did with it

Submission answers for "which CockroachDB tools", "which AWS services", the
optional architecture diagram, and the optional feedback. Every claim points at
a file you can open. Where a named tool was **not** used, this says so.

- Repo: <https://github.com/psamin/project_flock> (public, Apache 2.0)
- Cluster: CockroachDB v26.2.5 — a Cloud cluster with the schema and
  least-privilege grants applied, plus a 3-node Docker rig for the chaos runs
- Suite: 805 tests, `make test`

---

## 1. CockroachDB tools

| Tool | Used | Where |
|---|---|---|
| **Distributed Vector Indexing** | ✅ load-bearing | [`colony/schema/v1_1.sql`](../colony/schema/v1_1.sql), [`colony/fleetmem/client.py`](../colony/fleetmem/client.py), [`colony/sim/recall.py`](../colony/sim/recall.py) |
| **Managed MCP Server** | ✅ posture real, transport partial | [`infra/mcp.py`](../infra/mcp.py), [`colony/console/`](../colony/console/) |
| **ccloud CLI** | ❌ not used | — |
| **Agent Skills** | ❌ not used | — |

### Distributed vector indexing

Two vector indexes, declared inline in the DDL so the operator class travels
with the column:

```sql
VECTOR INDEX obs_embedding_idx  (mission_id, embedding vector_cosine_ops)  -- observations
VECTOR INDEX mm_situation_idx   (embedding vector_cosine_ops)              -- mission_memories
```

`vector_cosine_ops` is named explicitly. A bare `CREATE VECTOR INDEX (embedding)`
builds an L2 index, and a `<=>` query against it silently falls back to a scan —
so [`tests/test_schema.py`](../colony/tests/test_schema.py) asserts the operator
class is present in `SHOW CREATE`.

**What the agent actually does with them — two searches that scope in opposite
directions:**

| | scope | index | the agent's question | plan |
|---|---|---|---|---|
| **Reconcile gate** | within one mission | `obs_embedding_idx` | is this the victim I already know about? | **`FULL SCAN`**, deliberately |
| **Tactical recall** | across *every* mission and map | `mm_situation_idx` | what do we know about a moment like this? | **`vector search`** |

**Reconcile gate.** A scout sees something. It embeds the sighting with Titan,
then — *inside the same transaction that would insert it* — cosine-searches
existing beliefs of the same `kind` within a 5-tile box. A match merges and bumps
`sightings`; a miss inserts. Two scouts seeing one victim produce one victim, so
the fleet is never dispatched twice. The candidate filter is *in* the query, not
applied to its results: filtering a top-k in Python silently misses real
duplicates.

**Tactical recall.** When a mission ends, Claude reads its figures and derives
what would transfer — *"when a robot has cleared debris to reach a victim and a
medic is not yet present, bring the medic rather than continuing to explore"* —
and each lesson's **situation** is embedded into `mission_memories`. At the next
plan boundary a robot describes what it is facing, cosine search returns the top
3 tactics from situations like it, and those ride into the planning prompt. This
index carries **no prefix column at all**, because any scope would partition
exactly the knowledge it exists to generalise.

Lessons are deliberately *not* where the victims were: the same disaster does not
recur on the same tiles, and a fleet recalling victim coordinates has been handed
the answer. The lesson prompt forbids coordinates outright and the digest it
reads is built without them.

**Measured at scale** ([`docs/scale.md`](scale.md), harness
[`audit/experiment_scale.py`](../audit/experiment_scale.py)) — 50,000
observations and 5,000 lessons, `ANALYZE`d, 25 timed runs each:

| query | rows | plan | p50 | p95 |
|---|---|---|---|---|
| Tactical recall — no prefix | 5,000 | **`vector search`** | 36.0 ms | 57.6 ms |
| Mission-scoped observations — prefix constrained | **50,000** | **`vector search`** | **21.3 ms** | 25.0 ms |
| Reconcile gate — the deliberate full scan | 50,000 | `FULL SCAN` | 426.0 ms | 577.5 ms |

**The gate does not use the index, and we would rather say so than have a judge
discover it.** It constrains `kind` and a position box beside the vector
order-by; neither is a prefix column of `obs_embedding_idx`, and v26.2 declines
to serve an approximate top-k it would then have to filter. `embedding IS NOT
NULL` disables it a second, independent time. Measured at 1047 rows with four
EXPLAINs isolating one clause at a time, so it is the query *shape*, not demo
scale.

We keep the scan. An approximate search that misses a duplicate sends two robots
to one victim — the exact bug the gate exists to prevent — and at mission scale
(~1000 beliefs) the scan is a few milliseconds. At 50k rows it is 20× slower and
would need a prefix redesign, not a filter move.
`test_the_reconcile_gate_query_uses_the_index` is kept as a **strict xfail** so
the gap stays visible and cannot regress into a pass nobody rechecked. Tests
assert `EXPLAIN` **plans** rather than results, because a wrong vector query
still returns perfectly plausible rows.

### Managed MCP Server

[`infra/mcp.py`](../infra/mcp.py) emits the client config for the hosted endpoint
and asserts the posture it depends on:

```bash
uv run python ../infra/mcp.py config --cluster-id <id>   # snippet for Claude Code / Cursor / VS Code
uv run python ../infra/mcp.py check                      # assert read-only before wiring it
```

```json
{"mcpServers": {"colony-fleet-memory": {
  "type": "http",
  "url": "https://cockroachlabs.cloud/mcp?cluster=<id>",
  "readOnly": true,
  "env": {"CRDB_SQL_USER": "commander"}}}}
```

**Read-only is a property of the grant, not a setting.** `commander` holds
`SELECT` and nothing else across all eight tables
([`infra/credentials.py`](../infra/credentials.py)), asserted on the Cloud
cluster by `credentials.py verify`. `readOnly: true` is stated rather than left
to the default so nobody can flip it by accident, and
[`tests/test_mcp_config.py`](../colony/tests/test_mcp_config.py) asserts the
snippet points at the managed endpoint, names the least-privilege role, and never
turns writes on.

**What the agent does with it:** the commander console asks seven canned
questions that between them interrogate all four memory systems —

| question | memory | what it reads |
|---|---|---|
| `why_did_robot` | provenance | `plans` × `observations` through `based_on` |
| `aftershock_response` | provenance | what the fleet re-decided, and when |
| `unreached_victims` | working | `victims` × the task graph blocking them |
| `who_holds_what` | working | `tasks` × `robots`, and the lease on each |
| `what_do_we_know` | episodic | `observations`, merge count visible |
| `beliefs_30s_ago` | episodic | the same rows via **`AS OF SYSTEM TIME`** |
| `what_did_we_learn` | semantic | `mission_memories` — unscoped, on purpose |

`why_did_robot` is the one that matters: it answers with the rows that were in
the prompt, not with a plausible story about them.

**Stated plainly:** the in-app console executes those same audited queries
*directly* as that same least-privilege `commander` role, not through the managed
endpoint. Fixed audited SQL was chosen over free-form NL→SQL deliberately — a
model improvising SQL live is the one component that can fail in a way nobody
recovers from on camera, and a judge can read the statement, run it, and check
the answer. The console is seven queries, not an AI, and we do not describe it as
one. [`colony/console/reader.py`](../colony/console/reader.py) refuses writes at
a second layer, because the grant lives on the cluster and is absent on a laptop
running `make dev`.

### ccloud CLI — not used

Zero files. The Cloud cluster was created in the Cloud console, and per-robot SQL
users and grants are applied over SQL by `infra/credentials.py apply` rather than
through `ccloud`. Our internal plan named it as an optional fourth tool; it was
never delivered and we are not claiming it.

### Agent Skills — not used

Planned in the PRD, never run. Not claimed.

### CockroachDB features used beyond the named tools

- **`SERIALIZABLE` isolation** is what makes decentralized claiming safe. There
  is no allocator: robots rank open work and claim it themselves in one
  `UPDATE ... WHERE (status='open' OR lease_expires_at < now()) RETURNING id`,
  and the database adjudicates. Exercised under real contention with
  `ThreadPoolExecutor` races in
  [`tests/test_claiming.py`](../colony/tests/test_claiming.py), with
  `retry_on_serialization_failure` around SQLSTATE 40001.
- **`AS OF SYSTEM TIME`** gives the console time travel over the fleet's memory
  with no snapshot table, audit copy, or event-sourcing replay.
- **Changefeeds** carry an operator intervention to the fleet: the command is a
  `hazards` row and the changefeed is the only path from it to the robots
  ([`colony/fleetmem/changefeed.py`](../colony/fleetmem/changefeed.py)).
- **Multi-node survival.** Three nodes, kill one mid-rescue: zero tasks lost, no
  stall, rehearsed 5/5 ([`audit/x5-node-kill.md`](../audit/x5-node-kill.md)).

---

## 2. AWS services

| Service | Used | How |
|---|---|---|
| **Amazon Bedrock** — Titan Text Embeddings V2 (`amazon.titan-embed-text-v2:0`) | ✅ | Every observation and every learned lesson, at **512 dims** — both vector paths above are Titan vectors |
| **Amazon Bedrock** — Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) | ✅ | Plan-boundary decisions and end-of-mission lesson derivation, via a cross-region inference profile |
| **Amazon EC2** | documented | Free-tier `t3.micro` is the hosting path in [`docs/deploy.md`](deploy.md) — the app container idles at 45 MiB, so the database is what needs the RAM |
| AWS Lambda | ❌ | Not used — the fleet is one long-lived tick loop, not request/response |
| Amazon S3 | ❌ | Not used — the map and the cassette are committed files |

**What the agent does with Bedrock.** At a plan boundary — task selection,
replan-on-aftershock, conflict resolution, never per tick — a robot builds a
digest of *its own local slice* of memory plus the tactics recall returned, and
asks Claude for strict JSON: `{task_id | explore(sector) | return_to_base,
rationale}`. The rationale becomes the thought bubble in the UI and the row in
`plans`. Global state is never in the prompt, because a robot that can read
everything is not solving the problem we set.

**Rules are the floor, not the fallback.** A mission runs identically with no AWS
credentials at all, and `plans.chosen.source` records `bedrock` or `rules` per
decision — so *"the LLM is driving this"* is checkable in SQL rather than
asserted. In a full mission, 15 of 36 decisions are Bedrock's.

**Discipline in [`colony/bedrock/adapter.py`](../colony/bedrock/adapter.py):**

- **Three modes** — `live`, `record`, `replay`. Seeded demo runs replay a
  committed cassette, which is what makes a demo reproducible *and* lets the
  public deployment show real Claude output with **no AWS credential on the box**.
- **`boto3` is an optional extra**, lazily imported. The demo runs with the AWS
  SDK not installed at all.
- **Transient vs. fatal is classified, not blanket-caught.** Throttling, quota
  and timeouts fall back to rules; `AccessDeniedException`,
  `ValidationException` and `ResourceNotFoundException` surface — treating a
  broken IAM policy as weather would mean shipping an AWS integration that never
  once called AWS while looking perfectly healthy.
- **Rate cap** of 4 plan calls per robot per minute
  ([`colony/agents/planning.py`](../colony/agents/planning.py)), with a separate
  smaller reserve for world-changed replans so ordinary planning cannot eat it.
- **Replay with an empty cassette is fatal** (`NoCassette`), because the
  alternative is hash-derived vectors filling a `VECTOR(512)` column that nobody
  can distinguish from Titan's afterwards.

Region defaults to `us-east-1` (`AWS_REGION`); credentials come from the standard
boto3 chain.

---

## 3. Architecture

```
                                 ┌─────────────────────────────────────┐
   Browser                       │            AWS                      │
   ┌──────────────────┐          │  ┌───────────────────────────────┐  │
   │ /       2D canvas│          │  │ Amazon Bedrock                │  │
   │ /sim3d  WebGL    │          │  │  · Claude Haiku 4.5           │  │
   └────────▲─────────┘          │  │      plan boundaries, lessons │  │
            │ websocket          │  │  · Titan Text Embeddings V2   │  │
            │ 4 Hz frames        │  │      512-dim, every belief    │  │
   ┌────────┴─────────┐          │  └───────────▲───────────────────┘  │
   │ Sim server       │          │              │ boto3, rate-capped   │
   │ FastAPI, 4 Hz    │          │              │ live│record│replay   │
   │ authoritative    │          └──────────────┼──────────────────────┘
   │ world + physics  │                         │
   └────────▲─────────┘                         │
            │ percepts / validated actions      │
   ┌────────┴──────────────────────────────────┴──────┐
   │ Robot agents — scout · lifter · medic            │
   │   sense → sync → think → act → report            │
   │   no robot-to-robot channel, by construction     │
   └────────────────────────▲─────────────────────────┘
                            │ fleetmem SDK (psycopg 3, SERIALIZABLE)
                            │ report_observation · claim_task · complete_task
                            │ get_beliefs · heartbeat · log_plan · log_event
   ┌────────────────────────▼─────────────────────────────────────────┐
   │ CockroachDB v26.2.5                                              │
   │                                                                  │
   │  WORKING     robots · tasks (leases) · victims · hazards         │
   │  EPISODIC    observations  VECTOR(512) → obs_embedding_idx       │
   │  PROVENANCE  plans (based_on) · events                           │
   │  SEMANTIC    mission_memories VECTOR(512) → mm_situation_idx     │
   │                                                                  │
   │  changefeed ──► operator interventions reach the fleet           │
   │  AS OF SYSTEM TIME ──► the console reads the past                │
   └───▲──────────────────────────────────▲───────────────────────────┘
       │ SELECT-only as `commander`       │
       │                                  │ kill a node mid-mission
   ┌───┴──────────────────────┐      ┌────┴──────────────────┐
   │ Commander console        │      │ Chaos rig, 3 nodes    │
   │ 7 audited questions ·    │      │ infra/cluster3.sh     │
   │ Managed MCP Server       │      └───────────────────────┘
   │ config via infra/mcp.py  │
   └──────────────────────────┘
```

Three arrows carry the whole design: robots talk **only** to CockroachDB, Bedrock
is consulted **only** at plan boundaries, and the console reads with a grant that
cannot write.

---

## 4. Feedback for CockroachDB

From things that cost us time, not from the docs.

**Vector index + non-prefix filters is the sharp edge.** The behaviour is correct
— declining an approximate top-k that would then be filtered is the safe choice —
but it is *silent*. The query returns plausible rows and the only signal is
`EXPLAIN`. We shipped a passing test against a query shape we were not running. A
planner hint, or an opt-in warning like *"vector index not used: non-prefix
filter on `kind`"*, would have saved us the most expensive misconception in this
build.

**`CREATE VECTOR INDEX` prefix semantics deserve a worked counter-example.** The
docs explain that prefix columns must be constrained to exact values. What is
missing is the case that bit us: an *additional* non-prefix predicate disabling
the index entirely rather than being applied after the top-k. A three-line "this
plan is a full scan, and here is why" example would carry it.

**A bare `CREATE VECTOR INDEX (embedding)` silently builds `vector_l2_ops`**, and
a `<=>` query against it falls back to a scan with no error. The operator class
being required-in-practice but optional-in-syntax is a trap worth a warning.

**Serializable retry was a genuine non-event, and that is worth saying.** One
`retry_on_serialization_failure` decorator around 40001, and we never thought
about contention again with six agents claiming against shared rows at 4 Hz.
Coming from databases where this is a project, it was a day-one correctness win.

**Lease-in-the-`WHERE`-clause deserves to be a documented pattern.** Our entire
fault-tolerance story is one `UPDATE ... WHERE lease_expires_at < now() RETURNING
id`. We arrived at it ourselves; it is the single highest-leverage thing
CockroachDB let us *delete* — a watchdog, a sweeper, and a supervisor. We would
have adopted it on day one from a docs page called *"task queues without a
coordinator"*.

**v26.2 restricting `crdb_internal` and `system` broke our node-health check**
with a message that reads like a permissions bug rather than a deliberate policy.
`cockroach node status` was the answer; a pointer in the error text would have
shortened that from an hour to a minute.
