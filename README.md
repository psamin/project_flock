# project flock — a fleet that remembers together

*by bird labs* · <https://github.com/psamin/project_flock> · Apache 2.0

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

## Requirements

- **Docker** — CockroachDB v26.2.5 comes up from `colony/docker-compose.yml`
- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/) — deps are declared in
  [`colony/pyproject.toml`](colony/pyproject.toml) (FastAPI, uvicorn, psycopg 3;
  `boto3` is an optional extra)
- **No AWS credentials.** Bedrock replays a committed cassette by default, so a
  full mission runs offline. Credentials are needed only for live planning and
  for the console's free-form tier.

## Quickstart

```bash
git clone https://github.com/psamin/project_flock.git
cd project_flock/colony
make dev      # CockroachDB v26.2.5 + schema, one command
make sim      # tick server + renderer -> http://localhost:8000
make test     # 896 tests
```

The commander console's free-form tier needs two more things, both optional and
both one-time. Without them the console still answers its canned questions,
which is the tier the demo leans on:

```bash
make skills          # fetch the CockroachDB Agent Skills repo (pinned)
make mcp-login       # authorise once in a browser; every later run is headless
make console-check   # is it wired up here? prints why not, if not
```

Two views of the same mission, on the same frames:

| URL | What it is | Needs |
|---|---|---|
| [`/`](http://localhost:8000/) | Digital twin: an orbitable floating island, robots with sensor volumes and pose telemetry, a camera that frames the story off the event stream — and every panel below. | WebGL 2 |
| [`/2d`](http://localhost:8000/2d) | Canvas 2D top-down. Fog of war, thought bubbles, scoreboard. | nothing |

`/2d` is a second *renderer*, not a second simulation — same `/ws` frames, same
numbers, and both pages share `ui-shared.js` for the HUD, memory rail, fleet
panel, coordination feed, operator controls and commander console. It is a
separate route rather than a mode because the twin needs WebGL and `/2d`
deliberately does not: the front door is the page that can fail on a laptop with
hardware acceleration off, so the twin checks for WebGL *before* fetching 756K
of Three.js and sends anyone it cannot serve to `/2d`. `/sim3d` still resolves
to the twin, because the design doc and video script name that URL.

`make sim` runs without a cluster too — it falls back to in-memory fleet memory
and says so. Database-backed tests skip themselves when nothing is listening on
26257, and CI fails the build if they skip, so a broken cluster cannot
masquerade as a green run.

Other entry points: `make demo` (reset, learn from one headless mission, then
serve a mission that draws on it), `make preflight` (is this demo recordable
right now), `make cluster-3` + `make cluster-3-kill` (the node-kill chaos rig),
`make smoke` (both renderers actually draw).

## Configuration

Everything has a working default; nothing below is required for the quickstart.

| Variable | Default | What it does |
|---|---|---|
| `COLONY_DSN` | `postgresql://root@localhost:26257/colony?sslmode=disable` | Cluster the fleet writes to |
| `COLONY_CONSOLE_DSN` | — | Console's separate identity (`commander`, SELECT-only) |
| `COLONY_MEMORY` | — | `fake` forces the in-memory store |
| `COLONY_BEDROCK_MODE` | `replay` | `live` · `record` · `replay` |
| `COLONY_BEDROCK_CASSETTE` | `colony/cassettes/golden-run.json` | Recorded Bedrock responses |
| `COLONY_MAP` | `colony/world/maps/aftershock.json` | The scenario to run |
| `COLONY_RECALL` | `1` | `0` disables semantic recall, for the contrast run |
| `COLONY_SKILLS_DIR` | `colony/skills/` | Where `make skills` fetched the Agent Skills repo |
| `CRDB_CLUSTER_ID` | — | Cloud cluster the MCP client passes to every call |
| `COLONY_MCP_TOKEN_PATH` | `~/.colony/mcp-token.json` | OAuth refresh token from `make mcp-login` |

Example configs and data live in the repo: `colony/docker-compose.yml`
(single node), `colony/docker-compose.deploy.yml` (one-command hosting),
[`infra/docker-compose.3node.yml`](infra/docker-compose.3node.yml) (chaos rig),
[`colony/world/maps/aftershock.json`](colony/world/maps/aftershock.json) (the
scenario), [`colony/cassettes/golden-run.json`](colony/cassettes/golden-run.json)
(recorded Bedrock responses).

Deeper setup — the 3-node chaos rig, per-robot credentials, live Bedrock,
CockroachDB Cloud — is in [`docs/setup-testing.md`](docs/setup-testing.md).
Hosting the demo is in [`docs/deploy.md`](docs/deploy.md).

## Architecture

```
 Browser ── two views of one mission, same frames, same numbers:
    │        /       WebGL digital twin · orbitable island · sensor volumes ·
    │                pose telemetry · camera director · operator · console
    │        /2d     Canvas 2D · fog of war · scoreboard · no WebGL needed
    ▲ websocket (state frames, 4 Hz)
    │
 Sim server (Python 3.12 / FastAPI) — authoritative world
    · tick loop: apply actions → dynamics → derive percepts → broadcast
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
 Commander console ── two tiers, both read-only
    · seven canned questions ─ psycopg as `commander` (SELECT-only grant)
    · ask anything ────────── Claude on Bedrock, reading the cluster through the
      CockroachDB Managed MCP Server, equipping itself from the CockroachDB
      Agent Skills repo
 Operator console ── breaks the world on cue, through a `hazards` row and a
    CRDB changefeed; there is no other path in.
 Chaos rig ── kills a CockroachDB node on cue
```

The orchestrator is drawn small on purpose: allocation is decentralized, robots
rank open work and claim it themselves, dependency unblocking happens inside
`complete_task`'s transaction, and recovery is lease-native. The only job left
without another owner is telling the UI that a robot went quiet. A fuller
diagram showing where AWS sits is in
[`docs/tools-and-services.md`](docs/tools-and-services.md#3-architecture).

## The four ideas worth reading the code for

**Ownership is a lease.** A claim stamps `lease_expires_at`; the owner renews it
while it works; an expired lease is claimable by anyone *inside the same claiming
transaction*. Under serializable isolation exactly one robot wins, and a dead
robot's work frees itself — no sweep, no watchdog, nobody on the recovery path
(`claim_task` in [`colony/fleetmem/client.py`](colony/fleetmem/client.py)). All
expiry math uses database `now()`, so clock skew cannot manufacture a takeover.

**Reconcile before broadcast.** A new observation is vector-searched against
existing beliefs within 5 tiles, in the same transaction that would insert it: a
match merges and bumps a sighting count, a miss inserts. Two scouts seeing one
victim produce one victim. The candidate filter is *in* the query — filtering a
top-k in Python silently misses duplicates and dispatches the fleet twice.

**Every decision keeps its sources.** `plans.based_on` holds the observation rows
that were in the prompt digest, so "why did robot X do that" is answered by a
join rather than by a plausible story.

**The fleet learns tactics, not places.** When a mission ends, Claude derives what
would transfer — *"when a robot has cleared debris to reach a victim and a medic
is not yet present, bring the medic rather than continuing to explore"* — and each
lesson is embedded into `mission_memories`; at the next plan boundary cosine
search returns the tactics learned in situations like this one. Deliberately
**not** where the victims were: the same disaster does not recur on the same
tiles, and a fleet recalling victim positions has been handed the answer.

## Repo map

| Path | What it is |
|---|---|
| [`colony/`](colony/) | The system. Its [README](colony/README.md) has the module-by-module map. |
| [`colony/schema/`](colony/schema/) | The four-memory schema |
| [`colony/fleetmem/`](colony/fleetmem/) | The SDK every robot writes through, plus an in-memory fake |
| [`colony/agents/`](colony/agents/) | Scout, lifter, medic — the sense/sync/think/act/report loop |
| [`colony/bedrock/`](colony/bedrock/) | Bedrock adapter: live / record / replay |
| [`colony/sim/`](colony/sim/) | Authoritative world, 4 Hz tick server, websocket protocol |
| [`colony/client/`](colony/client/) | Both renderers. `scene3d.js`+`rigs.js`+`director.js` are the twin at `/`, `app.js`+`atlas.js` the 2D view at `/2d`, `ui-shared.js` every panel they share |
| [`colony/console/`](colony/console/) | Both console tiers, read-only: `questions.py` the seven canned reads, `agent.py` the Bedrock+MCP agent, `mcp_client.py` the managed-endpoint client, `skills.py` the Agent Skills loader |
| [`colony/orchestrator/`](colony/orchestrator/) | Lost-marking, and why it does nothing else |
| [`colony/tests/`](colony/tests/) | 896 tests |
| [`infra/`](infra/) | 3-node cluster, node-kill chaos rig, per-robot credentials, MCP config |
| [`audit/`](audit/) | The experiments behind every number, including three retracted findings |
| [`docs/`](docs/) | Setup, hosting, tooling writeup, the changefeed spike |
| [`PRD.md`](PRD.md) | The specification everything above cites by section |

## Tools

**CockroachDB** — distributed vector indexing in two places that scope in
opposite directions, serializable claiming, `AS OF SYSTEM TIME`, changefeeds,
survival of a node kill, the Managed MCP Server as the commander agent's hands,
and the Agent Skills repo as its reference.

The console has two tiers and they are read-only for *different* reasons, which
is worth separating because it is easy to state as one story and be wrong:

| | connects as | what stops a write |
|---|---|---|
| seven canned questions | `commander` | the grant — SELECT and nothing else, asserted on the Cloud cluster by `credentials.py verify` |
| ask anything | `managed-mcp` | a read-only tool allowlist, `assert_read_only` on every statement, and the managed server's own refusals |

The second row is a correction we made after calling the endpoint for real: MCP
does **not** inherit the `commander` grant, and the `CRDB_SQL_USER` key in the
published config snippet is inert.

**AWS Bedrock** — Claude Haiku 4.5 for planning at decision boundaries and for
the commander agent, Titan Text Embeddings V2 at 512 dims for beliefs and
lessons. Rules are the floor rather than the fallback: a mission runs identically
with no AWS credentials at all, and `plans.chosen.source` records which decided —
so "the LLM is driving this" is checkable in SQL rather than asserted.

What each tool actually does, what we deliberately did *not* use, the measured
plans at 50k rows, and feedback for CockroachDB:
[`docs/tools-and-services.md`](docs/tools-and-services.md).

## Licence

Apache 2.0 — see [LICENSE](LICENSE).
