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
| **Semantic** | `mission_memories` | what we learned across missions |

Read [`colony/schema/v1_1.sql`](colony/schema/v1_1.sql) first — it is commented
as an argument, not as DDL.

## Quickstart

```bash
cd colony
make dev      # CockroachDB v26.2.5 + schema, one command
make sim      # tick server + renderer -> http://localhost:8000
make test     # 617 tests
```

`make sim` runs without a cluster too — it falls back to in-memory fleet memory
and says so, so nothing is blocked on CockroachDB. Database-backed tests skip
themselves when nothing is listening on 26257, and CI fails the build if they
skip, so a broken cluster cannot masquerade as a green run.

Deeper setup — the 3-node chaos rig, per-robot credentials, live Bedrock,
CockroachDB Cloud — is in [`docs/setup-testing.md`](docs/setup-testing.md).

## Architecture

```
 Browser ── Canvas 2D renderer · fog of war · scoreboard · ON/OFF toggle
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
 Chaos rig ── kills a CockroachDB node on cue
```

The orchestrator is drawn small on purpose. Allocation is decentralized — robots
rank open work and claim it themselves — dependency unblocking happens inside
`complete_task`'s transaction, and recovery is lease-native. The only job left
without another owner is telling the UI that a robot went quiet.

## The three ideas worth reading the code for

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

## Repo map

| Path | What it is |
|---|---|
| [`colony/`](colony/) | The system. Its [README](colony/README.md) has the module-by-module map. |
| [`colony/schema/`](colony/schema/) | The four-memory schema |
| [`colony/fleetmem/`](colony/fleetmem/) | The SDK every robot writes through, plus an in-memory fake |
| [`colony/agents/`](colony/agents/) | Scout, lifter, medic — the sense/sync/think/act/report loop |
| [`colony/sim/`](colony/sim/) | Authoritative world, 4 Hz tick server, websocket protocol |
| [`colony/client/`](colony/client/) | Renderer, fog of war, thought bubbles, scoreboard |
| [`colony/orchestrator/`](colony/orchestrator/) | Lost-marking, and why it does nothing else |
| [`colony/console/`](colony/console/) | The commander console's five questions, read-only |
| [`infra/`](infra/) | 3-node cluster, node-kill chaos rig, per-robot credentials, MCP config |
| [`docs/`](docs/) | Setup, lane handoffs, the changefeed spike |
| [`PRD.md`](PRD.md) | The specification everything above cites by section |

## Tools

**CockroachDB** — serializable claiming, `VECTOR(512)` columns with a cosine
index behind the reconcile gate, survival of a node kill, and the Managed MCP
Server behind the commander console (read-only by grant, not merely by setting).

**AWS Bedrock** — Claude for planning at decision boundaries, Titan Text
Embeddings V2 at 512 dims for observations. Rate-capped per §3.5, with rules as
the floor rather than the fallback: a mission runs identically with no AWS
credentials at all, and `plans.chosen.source` records which one decided — so
"the LLM is driving this" is checkable in SQL rather than asserted.

## Licence

MIT — see [LICENSE](LICENSE).
