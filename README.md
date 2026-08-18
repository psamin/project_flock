# Colony — a fleet that remembers together

*project flock, by bird labs*

A heterogeneous robot fleet runs a disaster-relief mission as one team: shared
beliefs, transactional task claiming, automatic handoffs when one robot's work
unblocks another's, and replanning when the world changes underneath them.

There is no robot-to-robot channel. Every robot reads and writes one
CockroachDB cluster, and coordination is what falls out of that. The
demonstration is a database node killed mid-mission while the fleet keeps
rescuing people.

**The schema is the thesis.** Four memory systems, as named tables:

| Memory | Tables | What it holds |
|---|---|---|
| **Working** | `robots`, `tasks`, `victims`, `hazards` | what is true right now |
| **Episodic** | `observations` (512-dim `VECTOR`) | what we experienced |
| **Provenance** | `plans`, `events` | why we acted |
| **Semantic** | `mission_memories` (512-dim `VECTOR`) | what we learned across missions |

Read [`colony/schema/v1_1.sql`](colony/schema/v1_1.sql) first — it is commented
as an argument, not as DDL.

## Quickstart

```bash
cd colony
make dev      # CockroachDB v26.2.5 + schema, one command
make sim      # tick server + renderer -> http://localhost:8000
make test     # 792 tests
```

Two views of the same mission, on the same frames:

| URL | What it is | Needs |
|---|---|---|
| [`/`](http://localhost:8000/) | Canvas 2D top-down. Fog of war, thought bubbles, scoreboard. | nothing |
| [`/sim3d`](http://localhost:8000/sim3d) | Digital twin: an orbitable floating island, robots with sensor volumes and pose telemetry, and a camera that frames the story off the event stream. | WebGL 2 |

`/sim3d` is a second *renderer*, not a second simulation — it reads the same
`/ws` frames and shows the same numbers. It is a separate route rather than a
mode because it requires WebGL and `/` deliberately does not: if a machine
cannot run it, the 2D view still shows the whole mission, and the 3D page says
so and links there.

`make sim` runs without a cluster too — it falls back to in-memory fleet memory
and says so, so nothing is blocked on CockroachDB. Database-backed tests skip
themselves when nothing is listening on 26257, and CI fails the build if they
skip, so a broken cluster cannot masquerade as a green run.

Deeper setup — the 3-node chaos rig, per-robot credentials, live Bedrock,
CockroachDB Cloud — is in [`docs/setup-testing.md`](docs/setup-testing.md).

## Architecture

```
 Browser ── two views of one mission, same frames, same numbers:
    │        /       Canvas 2D · fog of war · scoreboard · ON/OFF toggle
    │        /sim3d  WebGL digital twin · orbitable floating island ·
    │                sensor volumes · pose telemetry · camera director
    ▲ websocket (state frames, 4 Hz)
    │
 Sim server (Python 3.12 / FastAPI) — authoritative world
    · tick loop: apply actions → dynamics → derive percepts → broadcast
    · validates every robot action
    ▲ actions / local percepts (in-process)
    │
 Robot agents — scout · lifter · medic
    sense → sync → think → act → report
       │            │
       │ Bedrock    └── fleetmem SDK ── CockroachDB
       │ (Claude planning,        robots · tasks(+leases) · observations(VECTOR)
       │  Titan V2 embeddings)    victims · hazards · events · plans
       │                              ▲                    ▲
 Orchestrator ────────────────────────┘                    │
    · lost-marking only — allocation and unblocking live in the data model
 Commander console ── read-only SQL / CockroachDB Managed MCP Server
 Operator console ── breaks the world on cue. The command is a `hazards` row and
    a CRDB changefeed carries it to the fleet; there is no other path in.
 Chaos rig ── kills a CockroachDB node on cue
```

The orchestrator is drawn small on purpose. Allocation is decentralized — robots
rank open work and claim it themselves — dependency unblocking happens inside
`complete_task`'s transaction, and recovery is lease-native. The only job left
without another owner is telling the UI that a robot went quiet.

## The four ideas worth reading the code for

**Ownership is a lease.** A claim stamps `lease_expires_at`; the owner renews it
while it works; an expired lease is claimable by anyone *inside the same
claiming transaction*. Under serializable isolation exactly one robot wins, and
a dead robot's work frees itself with no sweep, no watchdog, and nobody on the
recovery path — see `claim_task` in
[`colony/fleetmem/client.py`](colony/fleetmem/client.py). All expiry math uses
database `now()`, never a robot's clock, so clock skew cannot manufacture a
false takeover.

**Reconcile before broadcast.** A new observation is vector-searched against
existing beliefs within 5 tiles, in the same transaction that would insert it —
a match merges and bumps a sighting count, a miss inserts. Two scouts seeing one
victim produce one victim. The candidate filter is *in* the query rather than
applied to its results: filtering a top-k in Python silently misses real
duplicates and dispatches the fleet twice.

**Every decision keeps its sources.** `plans.based_on` holds the observation
rows that were in the prompt digest, so "why did robot X do that" is answered by
a join rather than by a plausible story. Click any robot in the UI, or ask the
commander console.

**The fleet learns tactics, not places.** When a mission ends, Claude reads its
figures and derives what would transfer — *"when a robot has cleared debris to
reach a victim and a medic is not yet present, bring the medic rather than
continuing to explore"* — and each lesson is embedded and stored in
`mission_memories`. At the next plan boundary a robot describes what it is
facing, cosine search returns the tactics learned in situations like it, and
those ride into the planning prompt.

Deliberately **not** where the victims were. The same disaster does not recur on
the same tiles, so a remembered coordinate transfers to nothing — and a fleet
recalling victim positions is a fleet handed the answer. The lesson prompt
forbids coordinates and sector names outright, and the run digest it reads is
built without them so the temptation is never offered.

This is also what makes retrieval real rather than decorative: lessons are
global across every mission and every map, so the index ranks many rows against
"what does this moment resemble?" rather than filtering to a handful.

## Repo map

| Path | What it is |
|---|---|
| [`colony/`](colony/) | The system. Its [README](colony/README.md) has the module-by-module map. |
| [`colony/schema/`](colony/schema/) | The four-memory schema |
| [`colony/fleetmem/`](colony/fleetmem/) | The SDK every robot writes through, plus an in-memory fake |
| [`colony/agents/`](colony/agents/) | Scout, lifter, medic — the sense/sync/think/act/report loop |
| [`colony/sim/`](colony/sim/) | Authoritative world, 4 Hz tick server, websocket protocol |
| [`colony/client/`](colony/client/) | Both renderers. `app.js`+`atlas.js` are the 2D view, `scene3d.js`+`rigs.js`+`director.js` the 3D one, `ui-shared.js` the HUD, ticker and console they share |
| [`colony/orchestrator/`](colony/orchestrator/) | Lost-marking, and why it does nothing else |
| [`colony/sim/interventions.py`](colony/sim/interventions.py) | Operator interventions: what may be broken, and what the world refuses |
| [`colony/console/`](colony/console/) | The commander console's seven questions, read-only — including one that reads the past with `AS OF SYSTEM TIME` |
| [`infra/`](infra/) | 3-node cluster, node-kill chaos rig, per-robot credentials, MCP config |
| [`docs/`](docs/) | Setup, lane handoffs, the changefeed spike |
| [`PRD.md`](PRD.md) | The specification everything above cites by section |

## Tools

**CockroachDB** — serializable claiming, survival of a node kill, and the Managed
MCP Server behind the commander console (read-only by grant, not merely by
setting).

Distributed vector indexing carries weight in **two** places, which is worth
separating because they pull in opposite directions:

| | scope | index | what it answers | plan |
|---|---|---|---|---|
| tactical recall | *across every mission and map* | `mm_situation_idx (embedding)` | what do we know about a moment like this? | **`vector search`** |
| reconcile gate | *within* one mission | `obs_embedding_idx (mission_id, …)` | is this the victim we already know about? | **`FULL SCAN`**, on purpose |

Both are cosine (`vector_cosine_ops`, `<=>`) and they scope in opposite
directions, which is the whole reason they are worth reading together. Tactical
recall has no prefix at all, because any scope would partition exactly the
knowledge it exists to generalise — and that is the one carrying the weight
here: it ranks every lesson from every past mission against "what does this
moment resemble?", and its plan is a real `vector search`.

**The gate deliberately does not use the index, and it is worth saying so before
a judge runs `EXPLAIN` and finds out.** It constrains `kind` and a 5-tile
pos box alongside the vector order-by. Neither is a prefix column of
`obs_embedding_idx`, and v26.2 will not serve an approximate top-k it then has
to filter — it declines the index rather than risk dropping a real match.
Measured at 1047 rows, so this is a property of the query shape, not of demo
scale.

That is the correct trade. An approximate scan that misses a duplicate makes the
fleet dispatch two robots to one victim; a full scan over one mission's
observations does not, and the merge has to be exact for the belief model to
mean anything. Restoring the index would mean filtering after the top-k, which
is precisely the bug the gate exists to avoid.

Get either wrong and the query still returns perfectly plausible rows, which is
why `tests/test_schema.py` and `tests/test_recall.py` assert the `EXPLAIN` plan
rather than the results — including
`test_the_reconcile_gate_query_uses_the_index`, kept as a **strict xfail** so
the gap stays visible and cannot quietly regress into a pass nobody rechecked.

**AWS Bedrock** — Claude for planning at decision boundaries, Titan Text
Embeddings V2 at 512 dims for observations. Rate-capped per §3.5, with rules as
the floor rather than the fallback: a mission runs identically with no AWS
credentials at all, and `plans.chosen.source` records which one decided — so
"the LLM is driving this" is checkable in SQL rather than asserted.

## Licence

Apache 2.0 — see [LICENSE](LICENSE).
