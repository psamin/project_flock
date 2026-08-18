# Colony — setup and testing walkthrough

A phase-by-phase guide from a bare laptop to every claim demonstrated.

Every command marked **✅ verified** below was actually run on this machine on
2026-08-12, and the output shown is the real output. Anything marked
**⚠️ unverified** has never been executed by anyone — treat those as work, not
as documentation.

This complements [`setup-testing.md`](setup-testing.md), which is the reference.
Where the two disagree, this file is the one that was run. See
[Corrections](#corrections-to-setup-testingmd) at the end.

---

## Contents

- [Current state of this machine](#current-state-of-this-machine)
- [Phase 0 — prerequisites](#phase-0--prerequisites)
- [Phase 1 — baseline, no setup at all](#phase-1--baseline-no-setup-at-all)
- [Phase 2 — local CockroachDB](#phase-2--local-cockroachdb)
- [Phase 3 — CockroachDB Cloud](#phase-3--cockroachdb-cloud)
- [Phase 4 — AWS Bedrock](#phase-4--aws-bedrock)
- [Phase 5 — the demo, by eye](#phase-5--the-demo-by-eye)
- [Verification matrix](#verification-matrix)
- [Troubleshooting](#troubleshooting)
- [Known issues](#known-issues)

---

## Current state of this machine

| Component | Status |
|---|---|
| `uv` 0.12.1 | ✅ installed, deps synced |
| Python 3.13 | ✅ via uv, `colony/.venv` present |
| Docker | ✅ installed, daemon up |
| 3-node chaos rig (`colony3`) | ✅ running, schema applied, grants applied |
| Single-node dev cluster (`colony`) | ⏸️ stopped, volumes intact |
| Full test suite | ✅ 629 passed, 0 skipped |
| Per-robot credentials | ✅ applied and verified |
| Chaos rehearsal | ✅ 1/1 survived |
| CockroachDB Cloud | ❌ not set up |
| AWS Bedrock | ❌ not set up, never called by anyone |
| `colony/cassettes/` | ❌ does not exist |

**Phases 0–2 are already complete.** If you only want what is left, skip to
[Phase 3](#phase-3--cockroachdb-cloud).

### The two clusters

Both bind port 26257, so exactly one runs at a time.

```bash
cd colony
docker compose start                 # single-node dev cluster
docker compose stop                  # stop it, keep the data
make down                            # stop it, DESTROY the volumes

../infra/cluster3.sh up              # 3-node rig  (= make cluster-3)
../infra/cluster3.sh down            # tear the rig down
```

The 3-node rig is a strict superset — same schema, same software, plus node
kill. There is no reason to go back to single-node except faster startup.

---

## Phase 0 — prerequisites

### 0.1 uv

```bash
brew install uv
cd colony && uv sync --extra dev
```

uv installs Python 3.13 itself. Nothing else to configure.

### 0.2 Docker Desktop

Needed for: the local cluster, the 3-node rig, and `tests/test_credentials.py`,
which shells out to the `docker` binary.

```bash
brew install --cask docker
open -a Docker            # wait for the whale to settle
docker info               # must print a server section, not an error
```

No Docker login required. One public image, `cockroachdb/cockroach:v26.2.5`,
about 500 MB on first pull.

### 0.3 Accounts, if you are doing Phases 3–4

Start **Bedrock model access first** — it is the only step with an approval
queue and everything else waits on it. See [Phase 4](#phase-4--aws-bedrock).

### 0.4 What it costs

| | Cost |
|---|---|
| Docker + local clusters | $0, disk only |
| CockroachDB Cloud Basic | $0, free tier is ample |
| Bedrock — Titan V2 embeddings | fractions of a cent per mission |
| Bedrock — Claude Haiku planning | cents per mission, capped at 4 calls/robot/min |

§3.5 budgets under $40 for the whole hackathon. The rate cap is what keeps a
runaway loop from approaching it.

---

## Phase 1 — baseline, no setup at all

The system degrades rather than blocking: no cluster falls back to in-memory
fleet memory, no AWS credentials falls back to rule-based planning. Both are
deliberate (§5.4).

### 1.1 Tests with no cluster

```bash
cd colony
uv run pytest -q --ignore=tests/test_credentials.py
```

✅ **Verified output:** `461 passed, 152 skipped, 1 warning in 23.51s`

> **The `--ignore` is mandatory if Docker is not installed.**
> `tests/test_credentials.py` shells out to `docker` at *collection* time, so
> without the flag pytest aborts the whole run with
> `FileNotFoundError: [Errno 2] No such file or directory: 'docker'` and you get
> **zero** tests, not a partial pass. With Docker installed you can drop it.

**The skips are the whole point.** 152 database-backed tests sat out. A green
run here proves the fleet logic and proves nothing about the integrations. CI
fails the build if the schema tests skip, precisely so a broken cluster cannot
masquerade as a green run.

### 1.2 The sim with no cluster

```bash
make sim        # http://localhost:8000
```

✅ Verified — HTTP 200, console API answering. Startup log:

```
[sim] no CockroachDB (OperationalError); using in-memory fleet memory
[sim] mission <uuid> ticking at 4 Hz (fake memory, 4 scouts)
```

Two things that look like bugs and are not:

- **"4 scouts" is a mislabel.** The fleet is 2 scouts + 1 lifter + 1 medic. See
  [Known issues](#known-issues).
- **`/api/runs` returns `{"current":"coordinated","runs":{}}`** until a mission
  *finishes*. Missions are 1200 ticks at 4 Hz ≈ **5 minutes**.

```bash
COLONY_MEMORY=fake make sim                 # force in-memory even with a cluster
COLONY_MAP=path/to/variant.json make sim    # a playtest map
```

---

## Phase 2 — local CockroachDB

Covers every claim including node-kill. **Already done on this machine.**

### 2.1 Start a cluster

```bash
cd colony
make dev                # single-node: cluster + schema, one command
# or, preferred:
make cluster-3          # 3-node rig: starts 3 nodes, waits for all to join, applies schema
```

`make dev` prints the DSN `postgresql://root@localhost:26257/colony?sslmode=disable`
and the admin UI at http://localhost:8080.

The rig waits for all three nodes before applying the schema — a node answers
before the cluster has formed, and a one-node check previously let the schema
land on a partly-joined cluster.

✅ Verified: `crdb-1: up / crdb-2: up / crdb-3: up / 3/3 nodes up`, all 8 tables
present (`events`, `hazards`, `mission_memories`, `observations`, `plans`,
`robots`, `tasks`, `victims`).

### 2.2 The full suite

```bash
uv run pytest -q        # no --ignore needed once Docker is installed
```

✅ **Verified output:** `629 passed, 1 warning in 47.32s` — **zero skips.**

This is the single highest-value command in this document. It converts the 152
skips from Phase 1 into real evidence: `test_schema.py` against live DDL,
`test_claiming.py`'s 1,000-way claim races and lease takeover, and the
CockroachDB half of `test_fleetmem.py`.

> Restart pytest **after** the cluster is up. The suite probes `localhost:26257`
> once at import time and caches the result in `DB_UP`, so a cluster started
> mid-run is not noticed.

### 2.3 Per-robot credentials

> ⚠️ **This requires the 3-node rig, not the dev cluster.**
> `infra/credentials.py:82-106` hardcodes `docker-compose.3node.yml`, project
> `colony3`, service `crdb-1`. `tests/test_credentials.py:38-50` hardcodes the
> same three. Run against `make dev` and you get:
> ```
> failed: CREATE USER IF NOT EXISTS s1
> service "crdb-1" is not running
> ```
> `setup-testing.md` places this section under the dev-cluster heading and says
> it "needs docker + the dev cluster". That is wrong.

```bash
cd colony
docker compose stop                              # free 26257 if dev cluster is up
../infra/cluster3.sh up
uv run python ../infra/credentials.py apply
uv run python ../infra/credentials.py verify
uv run pytest -q tests/test_credentials.py
```

✅ **Verified output:**

```
granted: 4 robot users + commander (read-only)
posture holds: 4 robots write-but-cannot-escalate, commander is read-only across 8 tables
16 passed in 1.25s
```

Three roles: `robot` writes observations/claims/events but cannot DROP, ALTER,
or read `plans`; `commander` is SELECT-only everywhere and is the identity the
MCP console uses; `admin` is for migrations only. The grant *is* the security
story rather than a setting — that is what `verify` asserts.

### 2.4 The node-kill rehearsal (FR-11)

```bash
make cluster-3
uv run python ../infra/chaos.py --rehearsals 5     # §5.4 wants ≥5 before recording
```

✅ **Verified** with `--rehearsals 1`, and it is fast — **~9 seconds per
rehearsal**, so 5 takes under a minute:

```
rehearsal 1/1
  tick 40: killed a node with 3 tasks in flight
  tick 100: node back
  SURVIVED: 3 tasks in flight at the kill, 0 lost, 1 completions before / 12 after,
            9 victims stabilized over 312 ticks

1/1 rehearsals survived
```

`chaos.py` runs a real mission, kills a node mid-run, and checks the two things
FR-11 claims — zero task loss and no fleet stall — measured from **fleet memory
rather than from the sim**, because the point is that the memory survived. It
revives the node afterwards; ✅ verified 3/3 nodes up after the run.

`tests/test_chaos.py` asserts the verdict logic against a fake and needs no
cluster. It is what stops the rig reporting success no matter what.

Manual control, if you want to do it live on camera:

```bash
make cluster-3-health      # exit 1 if any node is down
make cluster-3-kill        # kill node 2 — the demo beat
../infra/cluster3.sh revive
make cluster-3-down
```

### 2.5 The vector index

A wrong operator class degrades the reconcile gate to a full scan — correct
answers, no acceleration, nothing in the logs. Check it explicitly.
`SHOW INDEXES` does **not** show the operator class; use the DDL:

```bash
docker compose -f ../infra/docker-compose.3node.yml -p colony3 exec -T crdb-1 \
  ./cockroach sql --insecure -d colony -e "SHOW CREATE TABLE observations"
```

✅ **Verified output:**

```
VECTOR INDEX obs_embedding_idx (mission_id, embedding vector_cosine_ops)
```

Both required properties hold: `vector_cosine_ops` is named, and `mission_id`
leads as a prefix column — the gate always filters by mission, and prefix
columns only engage on an exact-value constraint.

### 2.6 The sim against a real cluster

```bash
make sim
```

Let it run, then confirm the fleet actually wrote:

```bash
docker compose -f ../infra/docker-compose.3node.yml -p colony3 exec -T crdb-1 \
  ./cockroach sql --insecure -d colony -e "
  SELECT verb, count(*) FROM events GROUP BY verb ORDER BY 2 DESC LIMIT 10;
  SELECT robot_id, trigger, rationale FROM plans ORDER BY at DESC LIMIT 5;
  SELECT kind, status, count(*) FROM tasks GROUP BY 1, 2;"
```

`plans` rows with `based_on` populated is FR-17 working end to end.

---

## Phase 3 — CockroachDB Cloud

⚠️ **Entirely unverified — nothing below has been run.**

**Optional.** Local covers every claim, including node-kill, which Cloud
*cannot* do — the free tier does not expose node control. Do this only to claim
the managed-service integration.

### 3.1 Create the cluster

1. Sign up at https://cockroachlabs.cloud (GitHub or Google login is fine).
2. **Create Cluster → Basic (serverless)**, provider **AWS**, region
   **`us-east-1`**.
3. On the connect dialog: **Create SQL user**, save the generated password (it
   is shown once), choose the **General connection string** tab, download the CA
   cert if offered.

The region is not arbitrary. §3.5 budgets fleetmem reads at p95 < 60 ms, and a
cross-region hop to Bedrock spends most of that budget by itself. Match this to
wherever Bedrock lives.

### 3.2 Apply the schema and point everything at it

```bash
export COLONY_DSN='postgresql://<user>:<pass>@<host>:26257/colony?sslmode=verify-full'
cd colony
uv run python -m schema.apply "$COLONY_DSN"   # creates the db, then applies v1_1.sql
uv run pytest -q          # the same suite, now against Cloud
make sim                  # the sim, now against Cloud
```

This is the one step in the walkthrough with no container to shell into. Local
dev never needs the `cockroach` CLI on the host — `make schema` runs it *inside*
the container — so `schema/apply.py` does the same work over psycopg, which is
already a dependency. If you would rather use the CLI
(`brew install cockroachdb/tap/cockroach`), this is the equivalent:

```bash
cockroach sql --url "$COLONY_DSN" -f schema/v1_1.sql
```

`COLONY_DSN` is the only knob — `CockroachFleetMem` (`fleetmem/client.py`) reads
it directly. No code change, no second variable.

### 3.3 Verify

Re-run the [vector index check](#25-the-vector-index) against Cloud — the DDL
travelled, but confirm rather than assume:

```sql
SHOW CREATE TABLE observations;   -- must still name vector_cosine_ops
EXPLAIN SELECT id FROM observations
 WHERE mission_id = '<a mission>'
 ORDER BY embedding <=> '[...]'::vector LIMIT 5;   -- expect an index scan
```

Free tier is ~50M request units/month, far more than a hackathon's missions.

> **Do not seed demo data with `IMPORT INTO`** — it is unsupported on tables
> carrying a vector index. Avoid large batched `VECTOR` inserts too.

---

## Phase 4 — AWS Bedrock

⚠️ **Entirely unverified.** `colony/README.md:198` states plainly that **nobody
has ever made a live Bedrock call.** Titan V2's 512-dim width and the
request/response shapes were written to the documented API and never checked
against the real service.

**Budget debugging time here, not just configuration time.** This is the step
most likely to break, and it is the one gated behind an approval queue.

### 4.1 Request model access — do this first

Console → set region to **us-east-1** (top-right selector) →
**Bedrock → Model access → Enable specific models**:

- `Anthropic · Claude Haiku 4.5` — planning (§4.3),
  `us.anthropic.claude-haiku-4-5-20251001-v1:0`
- `Amazon · Titan Text Embeddings V2` — beliefs, `amazon.titan-embed-text-v2:0`,
  512 dims to match `observations.embedding VECTOR(512)`

Model access is **region-scoped**: granted in one region does not apply in
another. Anthropic models ask for a short use-case description. Approval is
usually minutes but can take longer, which is why this is step one.

### 4.2 Credentials

Any mechanism works — `has_credentials()` asks botocore rather than reading env
vars, so an SSO profile or an ECS task role counts.

```bash
aws configure           # IAM user with AmazonBedrockLimitedAccess, or an inline
                        # policy allowing bedrock:InvokeModel on the two model ARNs
# or: aws configure sso && export AWS_PROFILE=…
# or, on ECS Fargate: the task role, nothing to export

export AWS_REGION=us-east-1
cd colony
uv run python -c "from bedrock.adapter import has_credentials; print(has_credentials())"
# True means botocore resolved credentials by *some* means
```

### 4.3 Confirm the models answer

```bash
export COLONY_BEDROCK_MODE=live
uv run python -c "
from bedrock.adapter import BedrockAdapter, has_credentials
print('credentials:', has_credentials())
a = BedrockAdapter(mode='live')
v = a.embed('victim under rubble at 14,9')
print('embedding dims:', len(v), 'calls:', a.calls)
p = a.plan('You are m1, a medic.', '- victim at (14,9) seen 2x confidence 0.90',
           [{'id': 'abc', 'kind': 'deliver_kit', 'target_x': 14, 'target_y': 9, 'priority': 5}])
print('plan:', p)
"
```

Expect `credentials: True`, `embedding dims: 512`, and a
`Plan(action='claim_task', …)` carrying a rationale.

A misconfiguration — no model access, wrong region, typo'd model id — **raises**
rather than silently falling back. That is deliberate: it stops you demoing an
AWS integration that never called AWS.

### 4.4 Record the golden cassette — do not skip this

`colony/cassettes/` does not exist. In default `replay` mode with no cassette
the planner declines every call and the entire fleet runs on rules. **Your demo
currently contains no LLM at all**, and nothing on screen says so.

```bash
export COLONY_BEDROCK_MODE=record
export COLONY_BEDROCK_CASSETTE=cassettes/golden-run.json
uv run python -c "
from fleetmem.fake import FakeFleetMem
from sim.mission import run_mission
from world.map_format import load_map
from bedrock.adapter import adapter_from_env
run = run_mission(load_map('world/maps/aftershock.json'), FakeFleetMem(),
                  seed=7, embedder=adapter_from_env())
print(run.metrics)"
```

Commit the cassette. Then the demo replays with no network at all:

```bash
export COLONY_BEDROCK_MODE=replay
export COLONY_BEDROCK_CASSETTE=cassettes/golden-run.json
make sim
```

### 4.5 Prove Bedrock actually decided

Availability is not the claim; deciding is. Bedrock decisions are tagged:

```sql
SELECT chosen->>'source' AS source, count(*) FROM plans GROUP BY 1;
-- 'bedrock' rows are model decisions; 'rules' rows are the fallback path
```

### 4.6 Cost control

The cap is 4 plan calls per robot per minute (`agents/planning.py`), enforced in
**ticks rather than wall-clock**, so it holds in replay too. A 6-robot 5-minute
mission is bounded at 120 Haiku calls plus one embedding per new belief.

---

## Phase 5 — the demo, by eye

No automated test covers any of this. `make sim`, then http://localhost:8000.

Mission shape, ✅ verified from `world/maps/aftershock.json`: 2 scouts, 1 lifter,
1 medic; 8 victims; **1200 ticks at 4 Hz ≈ 5 minutes**; the aftershock fires at
**tick 180, roughly 45 seconds in**.

| On screen | What it demonstrates |
|---|---|
| Fog filling in as scouts sweep | FR-8. In baseline the map stays dimmer and the badge reads `PRIVATE MAPS` |
| Thought bubbles (`🔍 scanning sector C1`) | §3.6, written by the agents every tick |
| **Click a robot** | FR-17 — rationale, trigger, whether Bedrock or rules decided, and the memories behind it |
| `coordination: ON/OFF` | FR-9 — restarts with the whole fleet rebuilt, not just the fog |
| Amber line under the scoreboard | §4.7 coordination gain |
| **Press `S`** | the sector grid (FR-16) |
| Event ticker | the coordination story in words, including the aftershock |

> **The gain line needs BOTH modes run to completion** — about **10 minutes of
> wall clock**. It deliberately refuses to compare a half run against a whole
> one, so it renders nothing until both finish. This reads as "broken" if you do
> not know it.

### The commander console (FR-10)

Five canned questions, ✅ verified present, each answered read-only and shown
next to the SQL that produced it:

| id | Memory | Question |
|---|---|---|
| `why_did_robot` | provenance | Why did robot {robot_id} do what it did? |
| `unreached_victims` | working | Which victims are still unreached, and what is blocking them? |
| `what_do_we_know` | episodic | What does the fleet know about the area around {x},{y}? |
| `who_holds_what` | working | Who is working on what right now, and are any leases lapsed? |
| `aftershock_response` | provenance | How did the fleet respond to the aftershock? |

The SQL is shown beside the result on purpose: FR-10 claims these answers come
out of fleet memory, and the query is what makes that checkable rather than
asserted.

Endpoints — everything except the restart is a read:

```
GET  /api/plans/{robot_id}?limit=5   rationale + trigger + source + resolved based_on
GET  /api/console/questions          the five questions and which memory each reads
POST /api/console/ask                {"question": "why_did_robot", "robot_id": "s1"}
POST /api/mission/restart            {"coordinated": false}
GET  /api/runs                       final numbers per mode
```

---

## Verification matrix

| Claim | Command | Passing looks like | Status |
|---|---|---|---|
| Fleet logic, no infra | `uv run pytest -q --ignore=tests/test_credentials.py` | `461 passed, 152 skipped` | ✅ |
| Everything, live cluster | `uv run pytest -q` | `629 passed`, **0 skipped** | ✅ |
| Schema valid on real CRDB | `uv run pytest tests/test_schema.py -v` | `13 passed`, no skips | ✅ |
| One winner per task (FR-2) | `uv run pytest tests/test_claiming.py -v` | `15 passed`, no skips | ✅ |
| Lease takeover (FR-5) | `uv run pytest tests/test_claiming.py -k lease -v` | no skips | ✅ |
| Reconcile gate (FR-4) | `uv run pytest tests/test_fleetmem.py -k similar -v` | runs twice: fake + cockroach | ✅ |
| Least privilege (§3.5) | `uv run python ../infra/credentials.py verify` | exits 0 | ✅ |
| Credentials enforced | `uv run pytest -q tests/test_credentials.py` | `16 passed` | ✅ |
| Node kill (FR-11) | `uv run python ../infra/chaos.py --rehearsals 5` | every rehearsal survives | ✅ (1/1) |
| Vector index is cosine | `SHOW CREATE TABLE observations` | names `vector_cosine_ops` | ✅ |
| Bedrock replay determinism | `uv run pytest tests/test_planning.py -v` | `12 passed` | ✅ |
| Frame carries what UI draws | `uv run pytest tests/test_frame_contract.py -v` | `20 passed` | ✅ |
| Toggle + provenance endpoints | `uv run pytest tests/test_server.py -v` | `19 passed` | ✅ |
| Works with neither | `COLONY_MEMORY=fake make sim` | banner says fake | ✅ |
| Cloud as fleet memory | `COLONY_DSN=… uv run pytest -q` | no skips | ❌ |
| Bedrock live | [§4.3](#43-confirm-the-models-answer) | 512 dims, real rationale | ❌ |
| Bedrock actually decides | `SELECT chosen->>'source' …` | `bedrock` rows exist | ❌ |
| Demo reads correctly | [Phase 5](#phase-5--the-demo-by-eye) | by eye, ~10 min | ❌ |

---

## Troubleshooting

**`FileNotFoundError: 'docker'`, zero tests run.** `tests/test_credentials.py`
shells out to docker at collection time. Start Docker Desktop, or use
`--ignore=tests/test_credentials.py`.

**Tests skip even though the cluster is up.** The suite probes `localhost:26257`
at import time and caches it. Restart pytest after the cluster is ready.

**`service "crdb-1" is not running`.** You are on the single-node dev cluster
and the script wants the 3-node rig. `docker compose stop && ../infra/cluster3.sh up`.
See [§2.3](#23-per-robot-credentials).

**`make sim` says "no CockroachDB".** It fell back to the fake. Check
`COLONY_DSN`, or that the schema finished applying.

**Port 26257 already allocated.** Both clusters want it. Stop one first.

**`/api/runs` is empty.** No mission has *finished*. That is 5 minutes per run.

**Bedrock `AccessDeniedException`.** Model access not granted *in that region*,
or the identity lacks `bedrock:InvokeModel`. Surfaces as an exception on
purpose; it is not treated as transient.

**Bedrock `ThrottlingException`.** Handled — the adapter falls back to rules and
the mission continues. Nothing to fix, but the run is no longer exercising the
model.

---

## Known issues

**1. `sim/server.py:406` mislabels the fleet.** Logs
`{len(mission.agents)} scouts`, but `mission.agents` is every robot — scouts,
lifters and medics. Prints "4 scouts" for a fleet of 2 scouts + 1 lifter +
1 medic. Cosmetic only. **Not fixed.**

**2. `infra/docker-compose.3node.yml` init container restart loop.** **Fixed
2026-08-12.** The command used a YAML folded scalar whose continuation lines
were *more* indented, so the newlines were preserved instead of folded. bash
received three lines, ran `./cockroach sql` with no statement, and tried to
execute `-e` as a command:

```
/bin/bash: line 3: -e: command not found
ERROR: cluster has already been initialized
```

With `restart: on-failure` that became a permanent restart loop. It stayed
invisible because `cluster3.sh` creates the database itself, so the cluster
worked regardless. Fixed by aligning the continuation line and bounding retries
to `on-failure:3`. Verified: `exited exit=0 restarts=0`, and `CREATE DATABASE`
now actually runs.

**3. Live Bedrock has never been exercised.** See
[Phase 4](#phase-4--aws-bedrock). The most likely thing to break under demo
pressure.

---

## Corrections to `setup-testing.md`

- **Per-robot credentials need the 3-node rig, not the dev cluster.** The
  reference places that section under `## 1. CockroachDB, locally` and states it
  "needs docker + the dev cluster". Both `credentials.py` and
  `test_credentials.py` hardcode `docker-compose.3node.yml` / `-p colony3` /
  `crdb-1`.
- **The suite is 629 tests, not 617**, as the READMEs say. Measured with a live
  cluster and zero skips.
- **`test_frame_contract.py` is 20 tests, not 19.** The reference's "one command
  per claim" table is stale on that row.
- **`SHOW INDEXES FROM observations` does not reveal the operator class.** The
  reference suggests it for verifying `vector_cosine_ops`. Use
  `SHOW CREATE TABLE observations`.
