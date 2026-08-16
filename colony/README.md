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
make test     # 617 tests
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
| [`sim/recall.py`](sim/recall.py) | Semantic memory: summarize a finished mission, recall it in the next (§4.0) |
| [`sim/seed_memory.py`](sim/seed_memory.py) | Runs the cold mission headless so the demo is genuinely the second |
| [`agents/scout.py`](agents/scout.py) | Scout loop: sense → sync → think → act → report |
| [`agents/worker.py`](agents/worker.py) | Lifter and medic: claim, path, work, complete |
| [`agents/planning.py`](agents/planning.py) | Bedrock at plan boundaries: role cards, digest, rate cap (§4.3, §3.5) |
| [`agents/beliefs.py`](agents/beliefs.py) | The shared hazard map routing is priced against |
| [`agents/logistics.py`](agents/logistics.py) | Battery, charging and supply kits (§3.3) |
| [`client/app.js`](client/app.js) | Renderer: layers per §4.8, fog, bubbles, ticker, scoreboard |
| [`client/atlas.js`](client/atlas.js) | The sprite sheet, drawn in code — no downloads, no licences |
| [`orchestrator/lost.py`](orchestrator/lost.py) | The heartbeat scan, and why it stays off the recovery path |
| [`console/questions.py`](console/questions.py) | The commander console's six canned questions (FR-10) |
| [`console/reader.py`](console/reader.py) | The read-only execution path the console cannot write through |
| [`fleetmem/changefeed.py`](fleetmem/changefeed.py) | P1 spike: waking on unblocks instead of polling (§4.4) |

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
| the console panel | six canned questions answered read-only from live fleet memory, each shown with the SQL that produced it (FR-10) |
| the scoreboard | `memory` — earlier missions on this map recalled through the vector index — and `bedrock` mode plus live call count |

The endpoints behind it — everything except the restart is a read:

```
GET  /api/plans/{robot_id}?limit=5   rationale + trigger + source + resolved based_on
GET  /api/console/questions          the six canned questions and which memory each reads
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

## Semantic memory — the fleet remembers the map

When a coordinated mission finishes, what it learned is summarized from *belief
rows* (never from simulator state — the fleet may only record what it actually
found), embedded with Titan V2, and written to `mission_memories`. When a mission
starts on the same map, those rows are found by cosine search over
`mm_embedding_idx` and the sectors earlier runs found victims in are seeded at a
higher priority, so they get swept first.

```bash
make seed-memory                     # run the cold mission headless, needs COLONY_DSN
COLONY_RECALL=0 make sim             # kill switch: run as if nothing was ever learned
```

Measured on Aftershock, same seed both ways: **312 ticks cold, 291 ticks with one
memory recalled.** Run it both ways before scripting a demo around it — the prior
is only as sharp as the map, and on a map where victims are spread across half
the sectors "sweep the hot ones first" buys less than it sounds like.

Three things this deliberately does **not** do:

- **It does not tell the fleet where victims are.** It biases search *order*.
  Every victim is still found by a scout that flies over it, which is checkable:
  `victims_located` is counted off belief rows, so a fleet that knew at tick 0
  would show it on the scoreboard.
- **It does not run in the baseline.** An uncoordinated run neither reads nor
  writes memories, or the ON/OFF comparison would be measuring its own history.
- **It does not enter the prompt digest.** Recall shapes task priorities instead,
  because `plans.based_on` is resolved against `observations` in two places and a
  `mission_memories` id put there is silently dropped by both.

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
- `mission_memories` prefixes on `map_key`, **not** `mission_id`: its search crosses
  missions by design. Same rule, opposite scope.
- **Do not add a secondary b-tree index that covers a vector index's prefix.** One was
  added on `(map_key, created_at DESC)` to serve the no-embedding recall path, and the
  optimizer then preferred scanning it and top-k-sorting over probing the vector index —
  a defensible choice at this row count, and the wrong one when the cosine search *is*
  the capability. Verified by `EXPLAIN` both ways: with both indexes the plan names
  `mm_map_recent_idx` and no `vector search` node appears. It is now dropped in the
  migration block.
- Both vector-index tests assert the **`EXPLAIN` plan**, not the results, because every
  failure mode above returns perfectly plausible rows.
- CockroachDB Cloud creates tables with `schema_locked = true`; migrations unlock and
  relock around themselves. That statement must be sent on its own, which is why
  `schema/apply.py` executes statement by statement rather than sending the file whole.
