# Colony — fleet coordination layer

Phase 1 (Aug 1): cluster, schema, the `fleetmem` SDK, the `map.json` contract,
CockroachDB vector indexing, the Bedrock adapter.
Phase 2 (Aug 2–3): the walking skeleton — scouts move, write beliefs, and render live in
a browser. All four §5.2 interface contracts are now frozen.

## Start here

```bash
cd colony
make dev      # CockroachDB v26.2.5 + schema applied, one command
make sim      # tick server + renderer -> http://localhost:8000
make test     # 202 tests
```

`make sim` runs without a cluster too — it falls back to in-memory fleet memory and says
so, so the renderer is never blocked on CockroachDB.

`make dev` prints the DSN and the admin UI URL. `make down` tears it back down.

Tests that need the database skip themselves when nothing is listening on 26257, so
`make test` works before you've started a cluster — but CI fails the build if they skip,
so a broken cluster can't masquerade as a green run.

## What's here

| Path | What it is |
|---|---|
| [`schema/v0.sql`](schema/v0.sql) | Schema v0 (§4.5), validated against live CockroachDB |
| [`fleetmem/client.py`](fleetmem/client.py) | The SDK — shared memory, claiming, reconcile gate |
| [`fleetmem/fake.py`](fleetmem/fake.py) | In-memory implementation; no cluster needed |
| [`bedrock/adapter.py`](bedrock/adapter.py) | Titan V2 embeddings + Claude planning, with offline mode |
| [`world/map_format.py`](world/map_format.py) | `map.json` loader and validator (§4.8) |
| [`world/maps/aftershock.json`](world/maps/aftershock.json) | The Aftershock reference map (§3.3) |
| [`sim/protocol.py`](sim/protocol.py) | Action API + websocket state frame (contracts 2 and 3) |
| [`sim/world.py`](sim/world.py) | Authoritative world state and the tick pipeline (§4.8) |
| [`sim/server.py`](sim/server.py) | 4 Hz tick loop, websocket broadcast, serves the client |
| [`agents/scout.py`](agents/scout.py) | Scout loop: sense → sync → think → act → report |
| [`client/app.js`](client/app.js) | Renderer, with client-side interpolation between ticks |

## Interface contracts (§5.2 — all four frozen Aug 3)

**2. Agent → sim.** One action per robot per tick; the server validates every one and
rejects illegal ones as events rather than exceptions.

```python
Action.move("n" | "s" | "e" | "w")     # advances up to the role's speed (§3.3)
Action.act("clear_debris" | "stabilize" | "recharge" | "restock", (x, y))   # adjacent only
Action.idle()
```

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
claim_task(task_id, robot_id) -> bool
complete_task(task_id, robot_id) -> list[UUID]   # ids of tasks this unblocked
heartbeat(robot_id, pos=None, battery=None, status=None) -> None
log_event(mission_id, actor, verb, detail=None) -> None

create_task(mission_id, kind, target=(None, None), priority=1, depends_on=()) -> UUID
open_tasks(mission_id) -> list[Task]
find_similar(mission_id, kind, pos, embedding, limit=5) -> Match | None
register_robot(robot_id, role, pos, battery) -> None
stale_robots(seconds=10) -> list[str]
events(mission_id) -> list[dict]
```

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

**Not yet done:** nobody has run a live Bedrock call. Titan V2 embedding width (512) and
the request/response shapes are written to the documented API but are unverified against
the real service. Whoever wires credentials first should run in `record` mode and commit
the cassette.

## Notes for whoever touches the schema

- The observations vector index **must** keep `vector_cosine_ops`. A default
  `CREATE VECTOR INDEX` builds an L2 index and the `<=>` reconcile gate silently
  degrades to a full scan — correct answers, no acceleration.
- `mission_id` leads that index as a prefix column; the gate always filters by mission,
  and prefix columns only engage on an exact-value constraint.
- Don't batch large `VECTOR` inserts, and note `IMPORT INTO` is unsupported on tables
  carrying a vector index — relevant when seeding demo data.
