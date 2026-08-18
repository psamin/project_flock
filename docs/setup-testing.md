# Setting up CockroachDB and AWS to test everything

Colony runs with **neither** of them: no cluster falls back to in-memory fleet
memory, and no AWS credentials falls back to rule-based planning. That is
deliberate (§5.4), and it also means a green `make test` on a bare laptop is
**not** proof the integrations work. This is how to prove them.

Work from `colony/` unless a command says otherwise.

```bash
cd colony
uv sync --extra dev     # once
```

## What runs without any setup

```bash
uv run pytest -q                     # everything that needs nothing
uv run pytest -q --ignore=tests/test_credentials.py   # if docker is missing
make sim                             # http://localhost:8000, fake memory
```

Skips are the tell. `394 passed, 109 skipped` means every database-backed test
sat out. CI fails the build if the schema tests skip, precisely so a broken
cluster cannot masquerade as a green run.

---

## 0. Starting from nothing

Four accounts and installs, in the order that stops you waiting on anything.
**Do step 4 first if you are in a hurry** — Bedrock model access is the only one
with a queue.

### 0.1 Docker Desktop (macOS) — free, ~10 min

Needed for: the local cluster, the 3-node chaos rig, and
`tests/test_credentials.py`, which shells out to `docker`.

```bash
brew install --cask docker     # or download from docker.com
open -a Docker                 # start it, and wait for the whale to settle
docker info                    # must print a server section, not an error
```

Colony needs no Docker login and pulls one public image
(`cockroachdb/cockroach:v26.2.5`, ~500 MB on first `make dev`).

### 0.2 uv and the Python toolchain — free, ~2 min

```bash
brew install uv                # or: curl -LsSf https://astral.sh/uv/install.sh | sh
cd colony && uv sync --extra dev
uv run pytest -q --ignore=tests/test_credentials.py    # should be all green
```

`uv` installs Python 3.13 itself, so there is nothing else to set up.

### 0.3 CockroachDB Cloud account — free tier, ~10 min

1. Sign up at https://cockroachlabs.cloud (GitHub or Google login is fine).
2. **Create Cluster → Basic (serverless)**, cloud provider AWS, region
   `us-east-1` — the same region as Bedrock, because §3.5 budgets fleetmem reads
   at p95 < 60 ms and a cross-region hop spends most of that on its own.
3. On the connect dialog: **Create SQL user**, save the generated password, and
   choose the **General connection string** tab. Download the CA cert if it
   offers one.
4. Keep the string. §2 below turns it into `COLONY_DSN`.

The free tier gives ~50M request units a month, which is far more than a
hackathon's worth of missions. It does **not** expose node control — that is why
FR-11's node-kill runs on the local 3-node rig instead (§1).

### 0.4 AWS account and Bedrock model access — pay-as-you-go, ~15 min plus approval

1. Create an account at https://aws.amazon.com if you have none (needs a card;
   nothing here leaves the free tier except Bedrock's per-token cost).
2. Set the region to **us-east-1** in the console's top-right selector. Both
   models below are region-scoped, and access granted in one region does not
   apply in another.
3. **Bedrock → Model access → Enable specific models**. Request:
   - `Anthropic · Claude Haiku 4.5` — planning (§4.3)
   - `Amazon · Titan Text Embeddings V2` — beliefs (512 dims, matching
     `observations.embedding VECTOR(512)`)

   Anthropic models ask for a short use-case description; approval is usually
   minutes but can take longer, which is why this is the step to start first.
4. Credentials — any of these work, because `has_credentials()` asks botocore
   rather than reading env vars:
   - **Local, simplest:** IAM → Users → create a user with the
     `AmazonBedrockLimitedAccess` policy (or an inline policy allowing
     `bedrock:InvokeModel` on the two model ARNs) → create an access key →
     `aws configure`.
   - **SSO:** `aws configure sso` and `export AWS_PROFILE=…`.
   - **Deployed on ECS Fargate:** the task role. Nothing to export at all.

```bash
export AWS_REGION=us-east-1
uv run python -c "from bedrock.adapter import has_credentials; print(has_credentials())"
# True means botocore resolved credentials by *some* means
```

### 0.5 What each thing costs

| | Cost | Notes |
|---|---|---|
| Docker + local cluster | $0 | disk only |
| CockroachDB Cloud Basic | $0 | free tier is ample for this |
| Bedrock — Titan V2 embeddings | fractions of a cent per mission | one call per new belief |
| Bedrock — Claude Haiku planning | cents per mission | capped at 4 calls/robot/minute (§3.5), enforced in ticks |
| AWS deploy (S3/CloudFront/ECS) | not covered here | lane 5's checklist item |

The §3.5 ceiling is $40 for the whole hackathon. The rate cap is what keeps a
runaway loop from getting anywhere near it.

---

## 1. CockroachDB, locally (15 minutes, needs Docker)

The single-node dev cluster covers everything except the node-kill segment.

```bash
make dev        # starts CockroachDB v26.2.5, creates the DB, applies schema/v1_1.sql
make down       # tear down, including volumes
```

`make dev` prints the DSN (`postgresql://root@localhost:26257/colony?sslmode=disable`)
and the admin UI at http://localhost:8080.

**Verify — every skipped test should now run:**

```bash
uv run pytest -q                                   # skips should drop to ~0
uv run pytest -q tests/test_schema.py -v           # DDL against a live cluster
uv run pytest -q tests/test_claiming.py -v         # 1,000-way claim races, lease takeover
uv run pytest -q tests/test_fleetmem.py -v         # same suite against fake AND cockroach
```

`tests/test_claiming.py` is the one worth watching: it opens a connection per
robot and asserts exactly one winner per task under serializable isolation, plus
expired-lease takeover. That is FR-2 and FR-5 demonstrated rather than asserted.

**Run the sim against the real cluster:**

```bash
make dev
make sim        # picks up CockroachDB automatically; prints which memory it used
```

Then check the fleet actually wrote to it:

```bash
docker compose exec -T cockroach ./cockroach sql --insecure -d colony -e "
  SELECT verb, count(*) FROM events GROUP BY verb ORDER BY 2 DESC LIMIT 10;
  SELECT robot_id, trigger, rationale FROM plans ORDER BY at DESC LIMIT 5;
  SELECT kind, status, count(*) FROM tasks GROUP BY 1, 2;"
```

`plans` having rows with `based_on` populated is FR-17 working end to end.

### Per-robot credentials (§3.5, judge-visible security posture)

```bash
uv run python ../infra/credentials.py apply    # create robot/commander/admin roles
uv run python ../infra/credentials.py verify   # assert least privilege holds
uv run pytest -q tests/test_credentials.py     # needs docker + the dev cluster
```

The commander role is SELECT-only everywhere — that is the identity the MCP
console uses (FR-10), so the grant is the security story rather than a setting.

### The 3-node chaos rig (FR-11, §6.5)

The Cloud free tier does not expose node control, so the node-kill beat runs on
a local 3-node cluster.

```bash
make cluster-3          # start 3 nodes, wait for all to join, apply schema
make cluster-3-health   # exit 1 if any node is down
make cluster-3-kill     # kill node 2 — the demo beat
../infra/cluster3.sh revive
make cluster-3-down
```

The rig binds node 1 on the usual 26257, with nodes 2 and 3 on 26258/26259, so
the app connects exactly as it does to the dev cluster. Bring `make down` first
if the single-node cluster is still running — they share the port.

**Verify resilience properly.** `infra/chaos.py` drives the whole rehearsal:
runs a real mission, kills a node mid-run, and checks the two things FR-11
claims — zero task loss and no fleet stall — measured from fleet memory rather
than from the sim, because the point is that the *memory* survived.

```bash
make cluster-3
uv run python ../infra/chaos.py --rehearsals 5    # §5.4 wants ≥5 before recording
make cluster-3-down
```

`tests/test_chaos.py` asserts the verdict logic against a fake and needs no
cluster; it is what stops the rig reporting success no matter what.

## 2. CockroachDB Cloud (the primary fleet memory, §4.6)

1. Create a free serverless cluster at https://cockroachlabs.cloud, in the same
   region as the AWS work (`us-east-1`) — §3.5 budgets p95 < 60 ms for fleetmem
   reads and cross-region kills that.
2. Create a SQL user, download the CA cert, and copy the connection string.
3. Apply the schema:

```bash
cockroach sql --url "$COLONY_DSN" -f schema/v1_1.sql
```

4. Point everything at it:

```bash
export COLONY_DSN='postgresql://<user>:<pass>@<host>:26257/colony?sslmode=verify-full'
uv run pytest -q                # the same suite, now against Cloud
make sim                        # the sim, now against Cloud
```

`COLONY_DSN` is read by `CockroachFleetMem` (`fleetmem/client.py`), so no code
changes and no other env vars.

**Verify the vector index is doing work** — this is the CRDB tool the submission
claims (§4.6), and a wrong operator class silently degrades it to a full scan
with correct answers:

```sql
SHOW INDEXES FROM observations;   -- obs_embedding_idx must use vector_cosine_ops
EXPLAIN SELECT id FROM observations
 WHERE mission_id = '<a mission>'
 ORDER BY embedding <=> '[...]'::vector LIMIT 5;   -- expect an index scan
```

## 3. AWS Bedrock

Two models (§4.6): **Claude Haiku 4.5** for planning
(`us.anthropic.claude-haiku-4-5-20251001-v1:0`) and **Titan Text Embeddings V2**
for beliefs (`amazon.titan-embed-text-v2:0`, 512 dims to match
`observations.embedding VECTOR(512)`).

1. In the Bedrock console → **Model access**, request access to both. Haiku is
   usually instant; do this first, because everything else waits on it.
2. Give whatever identity you use `bedrock:InvokeModel` on both model ARNs.
3. Configure credentials however you normally do — env vars, `AWS_PROFILE`, SSO,
   or an ECS task role. `has_credentials()` asks botocore rather than checking
   env vars, so a task role on Fargate counts (§4.6).

```bash
export AWS_REGION=us-east-1
export COLONY_BEDROCK_MODE=live      # replay (default) | record | live
```

**Verify credentials resolve and the models answer:**

```bash
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

Expect `credentials: True`, `embedding dims: 512`, and a `Plan(action='claim_task', …)`
with a rationale. A misconfiguration (no model access, wrong region, typo'd
model id) **raises** rather than silently falling back — that is deliberate, so
we never demo an AWS integration that never called AWS.

### Record the golden cassette (§4.3 `--seeded`, §5.4)

Replay mode with no cassette means every robot runs on rules and the planner
declines — correct, and invisible. Record a real run once and commit it:

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

Then the demo replays it with no network at all:

```bash
export COLONY_BEDROCK_MODE=replay
export COLONY_BEDROCK_CASSETTE=cassettes/golden-run.json
make sim
```

**Verify Bedrock is actually deciding**, not just being available — Bedrock
decisions are tagged in `plans.chosen`:

```sql
SELECT chosen->>'source' AS source, count(*) FROM plans GROUP BY 1;
-- 'bedrock' rows mean a model decision; 'rules' rows are the fallback path
```

### Cost

§3.5 budgets < $40 total. The rate cap is 4 plan calls per robot per minute
(`agents/planning.py`), so a 6-robot 5-minute mission is bounded at 120 Haiku
calls plus one embedding per new belief — cents per mission. The cap is
enforced in ticks, not wall-clock, so it holds in replay too.

## 3.5 The commander agent (Managed MCP Server + Agent Skills)

The console's seven canned questions need only a cluster. Its **free-form** tier
needs Bedrock (§3 above), the Managed MCP Server, and the Agent Skills repo.
All three are optional — without them the canned tier still answers, and the
console says why the other one is off rather than hiding it.

```bash
cd colony
make skills        # 34 skills at a pinned commit -> colony/skills/ (gitignored)
make mcp-login     # one browser login; stores a refresh token in ~/.colony/
make console-check # prints exactly what is missing, if anything
```

`console-check` on a working setup:

```
authenticated ok | connects as: managed-mcp
tables visible: 8
{'available': True, 'bedrock': True, 'mcp': True, 'cluster': True, 'skills': 34, ...}
```

### Why the login is interactive, once

The endpoint advertises `authorization_code` and `refresh_token` and **no**
`client_credentials` grant, so a server-side process cannot mint a token from a
secret. `make mcp-login` does dynamic client registration plus PKCE, and stores
the refresh token 0600 in `~/.colony/mcp-token.json` — outside the repo, so no
.gitignore rule stands between it and a commit. Every later run refreshes
headlessly; you should not need to log in again.

### Three things that will surprise you

Found by calling the endpoint rather than by reading about it, and all three
contradict something we had written down:

1. **It connects as `managed-mcp`, not as your SQL user.** `SELECT current_user`
   through the server proves it. The `CRDB_SQL_USER` key in the Cloud console's
   config snippet is not read. Our `commander` role still exists and still holds
   SELECT and nothing else — it governs `console/reader.py`, the psycopg path,
   and not this one.
2. **`?cluster=<id>` in the URL is not read.** Calls fail with "cluster_id not
   provided" unless the id is *also* a tool argument. `console/mcp_client.py`
   injects it; if you write your own client, remember to.
3. **Write tools are still offered with `readOnly: true` set.** `insert_rows`,
   `create_table` and `create_database` appear in `tools/list`. Compare what the
   server offers against what our agent is given:

```bash
uv run python -m console.mcp_client tools
# server offers 12: create_database, create_table, explain_query, ...
# agent is given 6: select_query, explain_query, get_table_schema, ...
# withheld from the agent: create_database, create_table, get_cluster, insert_rows, ...
```

### What the endpoint will not do

Worth knowing before you write a question it cannot answer: `SHOW USERS`,
`SHOW GRANTS` and `SHOW SYSTEM GRANTS` are refused, `information_schema` and
`crdb_internal` are blocked, and `SHOW` is accepted only for `SCHEMAS`,
`INDEXES`, `REGIONS`, `CONSTRAINTS` and `CREATE TABLE`. So a privilege audit is
not reachable from the console; the agent will tell you that and name the
statements to run yourself.

### Verify it end to end

```bash
make sim   # then, in the console's ask box:
```

- *"Is the vector index on mission_memories actually used, or a full scan?"* —
  should come back with `vector search` on `mm_situation_idx`, and should
  describe the `observations` full scan as deliberate rather than as a bug.
- *"Audit whether our SQL user privileges are hardened."* — should load the
  `hardening-user-privileges` skill and then say it cannot complete the audit
  through this endpoint. Both halves are the correct answer.

---

## 4. The renderer (no setup at all)

```bash
make sim                 # http://localhost:8000
COLONY_MEMORY=fake make sim          # force the in-memory store
COLONY_MAP=path/to/variant.json make sim   # a playtest map, or an early aftershock
```

What to look at, and what each thing proves:

| On screen | What it demonstrates |
|---|---|
| Fog filling in as scouts sweep | FR-8; in baseline the same map stays dimmer and the badge reads `PRIVATE MAPS` |
| Thought bubbles (`🔍 scanning sector C1`, `🧱 clearing debris`) | §3.6; written by the agents every tick |
| **Click a robot** | FR-17 — rationale, trigger, whether Bedrock or rules decided, and the memories behind it |
| `coordination: ON/OFF` button | FR-9 — restarts the mission with the whole fleet rebuilt, not just the fog |
| The amber line under the scoreboard | §4.7's coordination gain, once **both** modes have run to the end |
| **Press `S`** | the sector grid (FR-16) |
| Event ticker | the coordination story in words, including the aftershock |

The gain line deliberately refuses to compare two runs unless both finished —
half a run against a whole one produces a number nobody can defend.

## 5. One command per claim

| Claim | Command | Passing looks like |
|---|---|---|
| Schema is valid on real CRDB | `uv run pytest tests/test_schema.py -v` | no skips |
| Exactly one winner per task (FR-2) | `uv run pytest tests/test_claiming.py -v` | no skips |
| Lease takeover self-heals (FR-5) | `uv run pytest tests/test_claiming.py -k lease -v` | no skips |
| Reconcile gate merges duplicates (FR-4) | `uv run pytest tests/test_fleetmem.py -k similar -v` | runs twice: fake + cockroach |
| Least privilege (§3.5) | `uv run python ../infra/credentials.py verify` | exits 0 |
| Node kill (FR-11) | `make cluster-3 && uv run python ../infra/chaos.py --rehearsals 5` | every rehearsal survives |
| Bedrock live | the snippet in §3 above | 512 dims, a real rationale |
| Bedrock replay is deterministic | `uv run pytest tests/test_planning.py -v` | 12 passed |
| The frame carries what the UI draws | `uv run pytest tests/test_frame_contract.py -v` | 19 passed |
| Toggle and provenance endpoints | `uv run pytest tests/test_server.py -v` | 19 passed |
| Fleet still works with neither | `COLONY_MEMORY=fake make sim` | mission runs, banner says fake |

## Troubleshooting

**`FileNotFoundError: 'docker'`** — `tests/test_credentials.py` shells out to
docker. Use `--ignore=tests/test_credentials.py` or start Docker Desktop.

**Tests skip even though `make dev` succeeded** — the suite probes
`localhost:26257` at import time. Restart pytest after the cluster is up;
`DB_UP` is computed once per session.

**`make sim` says "no CockroachDB"** — it fell back to the fake. Check
`COLONY_DSN`, or that `make dev` finished applying the schema.

**Bedrock `AccessDeniedException`** — model access has not been granted in that
region, or the identity lacks `bedrock:InvokeModel`. This surfaces as an
exception on purpose; it is not treated as transient.

**Bedrock `ThrottlingException`** — handled: the adapter falls back to rules and
the mission continues. Nothing to fix, but the run is no longer exercising the
model.
