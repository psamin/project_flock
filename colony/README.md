# Colony — fleet coordination layer

Phase 1 (Aug 1): cluster, schema, the `fleetmem` SDK, the `map.json` contract,
CockroachDB vector indexing, the Bedrock adapter.
Phase 2 (Aug 2–3): the walking skeleton — scouts move, write beliefs, and render live in
a browser. All four §5.2 interface contracts are now frozen.
Phase 3 (Aug 3): the fleet — lifter and medic, leased claiming, sector claims, Bedrock
planning at plan boundaries, battery and kit logistics, replan-on-aftershock.
Phase 4 (Aug 3): the demo — pixel-art renderer, fog of war in both modes, thought
bubbles you can click for provenance, the coordination ON/OFF toggle and scoreboard.
Phase 5 (Aug 5): orchestration — lost-marking, the commander console's five read-only
questions, the aftershock retuned so it actually fires, and the changefeed spike.

## Start here

```bash
cd colony
make dev      # CockroachDB v26.2.5 + schema applied, one command
make sim      # tick server + renderer -> http://localhost:8000
make test     # 792 tests
```

To exercise CockroachDB Cloud, the 3-node chaos rig or live Bedrock, see
[`docs/setup-testing.md`](../docs/setup-testing.md) — a green `make test` on a bare
laptop proves the fleet works, not that the integrations do.

`make sim` runs without a cluster too — it falls back to in-memory fleet memory and says
so, so the renderer is never blocked on CockroachDB.

`make dev` prints the DSN and the admin UI URL. `make down` tears it back down.

Tests that need the database skip themselves when nothing is listening on 26257, so
`make test` works before you've started a cluster — but CI fails the build if they skip,
so a broken cluster can't masquerade as a green run.

## What's here

| Path | What it is |
|---|---|
| [`schema/v1_1.sql`](schema/v1_1.sql) | Schema v1.1 (§4.5), grouped by the four memories, validated against live CockroachDB |
| [`fleetmem/client.py`](fleetmem/client.py) | The SDK — shared memory, claiming, reconcile gate |
| [`fleetmem/fake.py`](fleetmem/fake.py) | In-memory implementation; no cluster needed |
| [`bedrock/adapter.py`](bedrock/adapter.py) | Titan V2 embeddings + Claude planning, with offline mode |
| [`world/map_format.py`](world/map_format.py) | `map.json` loader and validator (§4.8) |
| [`world/maps/aftershock.json`](world/maps/aftershock.json) | The Aftershock reference map (§3.3) |
| [`sim/protocol.py`](sim/protocol.py) | Action API + websocket state frame (contracts 2 and 3) |
| [`sim/world.py`](sim/world.py) | Authoritative world state and the tick pipeline (§4.8) |
| [`sim/server.py`](sim/server.py) | 4 Hz tick loop, websocket broadcast, serves the client |
| [`sim/recall.py`](sim/recall.py) | Semantic memory: derive tactics from a finished mission, retrieve them in the next (§4.0) |
| [`sim/seed_memory.py`](sim/seed_memory.py) | Runs missions headless so the fleet has experience before anyone watches |
| [`agents/scout.py`](agents/scout.py) | Scout loop: sense → sync → think → act → report |
| [`agents/worker.py`](agents/worker.py) | Lifter and medic: claim, path, work, complete |
| [`agents/planning.py`](agents/planning.py) | Bedrock at plan boundaries: role cards, digest, rate cap (§4.3, §3.5) |
| [`agents/beliefs.py`](agents/beliefs.py) | The shared hazard map routing is priced against |
| [`agents/logistics.py`](agents/logistics.py) | Battery, charging and supply kits (§3.3) |
| [`client/app.js`](client/app.js) | Renderer: layers per §4.8, fog, bubbles, ticker, scoreboard |
| [`client/atlas.js`](client/atlas.js) | The sprite sheet, drawn in code — no downloads, no licences |
| [`orchestrator/lost.py`](orchestrator/lost.py) | The heartbeat scan, and why it stays off the recovery path |
| [`console/questions.py`](console/questions.py) | The commander console's seven canned questions (FR-10), one of them a time-travel read |
| [`console/reader.py`](console/reader.py) | The read-only execution path the console cannot write through |
| [`fleetmem/changefeed.py`](fleetmem/changefeed.py) | Task unblocks (P1 spike, §4.4) and operator interventions (load-bearing, issue #22) |
| [`sim/interventions.py`](sim/interventions.py) | What an operator may break, what the world refuses, and how the row reaches the fleet |

## Interface contracts (§5.2 — all four frozen Aug 3)

**2. Agent → sim.** One action per robot per tick; the server validates every one and
rejects illegal ones as events rather than exceptions.

```python
Action.move("n" | "s" | "e" | "w")     # advances up to the role's speed (§3.3)
Action.act("clear_debris" | "stabilize", (x, y))       # adjacent, in-bounds
Action.act("recharge" | "restock", (x, y))   # inside the staging zone (§3.3)
Action.idle()
```

Battery drains one point per tick of movement (§3.3 quotes battery in *ticks*),
a flat battery strands a robot for good, and a medic spends one of its two kits
per victim. Agents plan around all three; see `agents/logistics.py`.

**3. Sim → browser.** A full `snapshot` frame on connect, then `diff` frames:

```jsonc
{"tick": 42, "kind": "diff",
 "robots": [{"id": "s1", "role": "scout", "x": 14, "y": 9, "facing": "e", "status": "moving", ...}],
 "victims": [{"id": "v1", "x": 14, "y": 9, "state": "located", ...}],
 "tiles_changed": [{"x": 6, "y": 19, "ground": "open", "object": "rubble_heavy"}],
 "events": [{"tick": 42, "actor": "s1", "verb": "victim_found", "detail": {...}}],
 "metrics": {"victims_located": 3, ...}}
```

Only `tiles_changed` is sent per tick — the full grid rides along once, in the snapshot.

## The demo UI

`make sim`, then http://localhost:8000. Nothing to install: the renderer is
Canvas 2D with no CDN and no WebGL requirement, and the sprites are drawn in code
(`client/atlas.js`), so there is no asset pack to fetch and no licence to track.

| Control | What it shows |
|---|---|
| click a robot | its latest decisions — rationale, trigger, whether Bedrock or rules chose, and the memories behind it (FR-17) |
| `coordination: ON/OFF` | restarts the mission with the whole fleet rebuilt, not just the fog (FR-9) |
| `S` | the exploration sector grid (FR-16) |
| the console panel | seven canned questions answered read-only from live fleet memory, each shown with the SQL that produced it (FR-10) |
| the scoreboard | `tactics` — lessons the fleet carries in, retrieved per decision through the vector index — and `bedrock` mode plus live call count |

The endpoints behind it — everything except the restart is a read:

```
GET  /api/plans/{robot_id}?limit=5   rationale + trigger + source + resolved based_on
GET  /api/console/questions          the seven canned questions and which memory each reads
POST /api/console/ask                {"question": "why_did_robot", "robot_id": "s1"}
POST /api/mission/restart            {"coordinated": false}
GET  /api/runs                       final numbers per mode
```

The console answers with its own SQL alongside the rows, on purpose: FR-10 claims
these answers come out of fleet memory, and the query beside the result is what
makes that checkable rather than asserted.

`COLONY_MAP=path/to/map.json make sim` runs a different map — a playtest variant,
or a copy with the escalation moved earlier to rehearse the aftershock beat.

## For lanes 2 and 4 — start now, no cluster required

`FakeFleetMem` mirrors the real client method for method. Build against it today; swap
the import when the cluster is ready.

```python
from fleetmem.fake import FakeFleetMem     # or: from fleetmem.client import CockroachFleetMem
from bedrock.adapter import BedrockAdapter
import uuid

mem = FakeFleetMem()
mission = uuid.uuid4()
bedrock = BedrockAdapter()                 # replay mode; no AWS credentials needed

# A scout reports what it sees. The reconcile gate merges duplicate sightings,
# so two scouts finding one victim produce one belief, not two.
belief = mem.report_observation(
    mission, "s1", "victim", pos=(14, 9),
    payload={"note": "behind debris"},
    embedding=bedrock.embed("victim under rubble at 14,9"),
)

# The rescue chain: the medic's task waits on the lifter's.
clear   = mem.create_task(mission, "clear_debris", (14, 8))
deliver = mem.create_task(mission, "deliver_kit", (14, 9), depends_on=[clear])

mem.claim_task(clear, "l1")                # True — exactly one robot can win
unblocked = mem.complete_task(clear, "l1") # [deliver] — handoff, no human involved
```

The two implementations are kept honest by [`tests/test_contract.py`](tests/test_contract.py)
(signatures must match) and [`tests/test_fleetmem.py`](tests/test_fleetmem.py) (the same
behavioural suite runs against both). If you need a method the fake doesn't have, add it
to both or the build goes red.

## SDK surface (§5.2 contract 1 — frozen Aug 3)

```python
report_observation(mission_id, robot_id, kind, pos, payload=None, embedding=None, confidence=1.0) -> UUID
get_beliefs(mission_id, area=None, kind=None) -> list[Belief]
claim_task(task_id, robot_id, lease_seconds=15) -> bool   # open OR expired lease
complete_task(task_id, robot_id) -> list[UUID] | None   # unblocked ids; None if it did not apply
heartbeat(robot_id, pos=None, battery=None, status=None, lease_seconds=15) -> None  # renews leases
log_event(mission_id, actor, verb, detail=None) -> None

create_task(mission_id, kind, target=(None, None), priority=1, depends_on=()) -> UUID
open_tasks(mission_id) -> list[Task]
find_similar(mission_id, kind, pos, embedding, limit=5) -> Match | None
register_robot(robot_id, role, pos, battery) -> None
stale_robots(seconds=10) -> list[str]
events(mission_id) -> list[dict]
renew_leases(robot_id, lease_seconds=15) -> int
release_task(task_id) -> None                    # status -> open, lease cleared
log_plan(mission_id, robot_id, trigger, chosen, rationale, based_on=()) -> UUID
plans_for(mission_id, robot_id=None) -> list[Plan]
```

## Semantic memory — the fleet learns tactics

When a coordinated mission finishes, its figures are summarised from the event
log and Claude is asked what would transfer to a *different* map. Each lesson is
a `situation` and what to do when it holds; the situation is embedded with Titan
V2 and stored in `mission_memories`. At every plan boundary a robot describes
what it is facing, cosine search over `mm_situation_idx` returns the tactics
learned in moments like it, and those go into the planning prompt.

Real output, derived live from one Aftershock run:

> **when** a robot has cleared debris to reach a victim and a medic is not yet
> present at that location — **then** immediately signal or move to bring the
> medic to the victim rather than continuing exploration, since response time to
> victims is critical and medics have limited capacity

In the following mission, **14 of 30 logged decisions cited a recalled tactic**,
and `plans.recalled_from` names which ones.

```bash
make seed-memory                     # run missions headless to build experience
COLONY_RECALL=0 make sim             # kill switch: derive nothing this run
```

What this deliberately does **not** store is where the victims were. The same
disaster does not recur on the same tiles, so a coordinate transfers to nothing,
and a fleet recalling victim positions is a fleet handed the answer. Two
mechanisms enforce it rather than one: the lesson prompt forbids coordinates and
sector names outright, and `run_digest` — the model's only view of the run — is
built from counts and outcomes so a place-shaped lesson has nothing to be built
from. `tests/test_recall.py` asserts both.

Three further properties worth knowing:

- **Retrieval is global.** No mission scope, no map scope. A tactic learned
  clearing rubble on one map is exactly what should surface while clearing
  rubble on another, so the vector index is unprefixed and always engages.
- **The baseline never reads it.** An uncoordinated run is a control condition;
  if it recalled what coordinated runs learned, the ON/OFF comparison would be
  measuring its own history.
- **Both halves fail soft.** A throttled model costs one decision its memory,
  and a mission that ends without deriving anything is a mission that still
  ended. The rules floor (§5.4) carries the fleet either way.

Provenance is split across two columns because they resolve against two tables:
`plans.based_on` holds `observations` — what the robot could see — and
`plans.recalled_from` holds `mission_memories` — what it had learned. Merged
into one `UUID[]`, half the ids would resolve to nothing, which is how a
decision trace quietly turns back into a plausible story.

## AWS Bedrock

Defaults to `replay` — deterministic, offline, no credentials. That keeps lanes unblocked
and is what `--seeded` demo runs use (§4.3).

```bash
export COLONY_BEDROCK_MODE=live       # or: record (live + writes a cassette)
export COLONY_BEDROCK_CASSETTE=cassettes/golden-run.json
export AWS_REGION=us-east-1
```

If `live`/`record` is requested without credentials it falls back to `replay` rather than
crashing — a missing credential should mean a degraded demo, not a dead one.

Agents ask Bedrock only at plan boundaries (§4.3) and only when it can answer
better than their own rules: over the §3.5 cap of 4 calls/robot/minute, or in replay
with no cassette entry, the planner declines and the robot uses the rules it always
had. `plans.chosen->>'source'` records which one decided, so "the LLM is driving this"
is a SQL query rather than a claim.

**Verified live (Aug 15–16, 2026):** Titan V2 at 512 dims and Claude Haiku both answer
against the real service, and [`cassettes/golden-run.json`](cassettes/golden-run.json)
holds the recorded responses. `bedrock_calls` on the scoreboard counts calls that
actually reached AWS — cassette hits and the offline fallback do not increment it, so
that number is the honest answer to "is the LLM really deciding".

One caveat worth knowing before relying on similarity: the *offline* embedding
(`_offline_embedding`, used in replay on a cassette miss) gives the same text the same
vector but similar text an unrelated one. It exercises the merge path and it is not
semantic. Anything claiming semantic similarity needs live or recorded Titan.

## Notes for whoever touches the schema

- The observations vector index **must** keep `vector_cosine_ops`. A default
  `CREATE VECTOR INDEX` builds an L2 index and the `<=>` reconcile gate silently
  degrades to a full scan — correct answers, no acceleration.
- `mission_id` leads that index as a prefix column; the gate always filters by mission,
  and prefix columns only engage on an exact-value constraint.
- Don't batch large `VECTOR` inserts, and note `IMPORT INTO` is unsupported on tables
  carrying a vector index — relevant when seeding demo data.
- `mission_memories` has **no prefix at all**: a tactic learned on one map is meant to
  apply on the next, so any scope would partition the knowledge it exists to
  generalise. An unprefixed vector index engages unconditionally.
- **Do not add a secondary b-tree index that covers a vector index's prefix.** One was
  added on `(map_key, created_at DESC)` to serve the no-embedding recall path, and the
  optimizer then preferred scanning it and top-k-sorting over probing the vector index —
  a defensible choice at this row count, and the wrong one when the cosine search *is*
  the capability. Verified by `EXPLAIN` both ways: with both indexes the plan names
  `mm_map_recent_idx` and no `vector search` node appears. It is now dropped in the
  migration block.
- Both vector-index tests assert the **`EXPLAIN` plan**, not the results, because every
  failure mode here returns perfectly plausible rows.
- **`WHERE embedding IS NOT NULL` disables the vector index.** It reads as hygiene and
  costs the capability: only filters matching a prefix column keep the index engaged,
  and that one matches nothing. `<=>` against NULL is NULL and NULLs sort last, so the
  guard was never needed.
- **KNOWN GAP:** `find_similar` — the reconcile gate, the project's headline vector use
  — does *not* hit `obs_embedding_idx` as written, for the same rule: it also constrains
  `kind` and a `pos_x/pos_y BETWEEN` box, and neither is a prefix column. Results stay
  correct and demo-scale data is small, so nothing looks wrong. Pinned by an
  `xfail(strict=True)` in `tests/test_schema.py`; reworking it is a genuine trade,
  because those filters in SQL are what stop the gate silently missing duplicates.
- CockroachDB Cloud creates tables with `schema_locked = true`; migrations unlock and
  relock around themselves. That statement must be sent on its own, which is why
  `schema/apply.py` executes statement by statement rather than sending the file whole.
