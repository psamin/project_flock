# Colony — Lane 1 working agreement

## Project

Colony is a shared-memory coordination layer on CockroachDB that lets a
heterogeneous robot fleet run a mission as one team: shared beliefs, transactional
task claiming, automatic handoffs when one robot's step unblocks another's, and
replanning when the world changes. It is demonstrated in a disaster-relief
simulation where the fleet keeps rescuing people while a database node is killed
mid-mission. The schema *is* the thesis — four memory systems (working, episodic,
semantic, provenance) as named tables — because judging criterion #1 and the
tie-break is Agentic Memory Design.

## My lane = Lane 1 only

Lane 1 is the **memory & data layer** (§5.1). I own, and only touch:

- `colony/schema/` — migrations and DDL
- `colony/fleetmem/` — the SDK package (client + fake)
- `colony/tests/` — tests for the above
- `infra/`, `colony/docker-compose.yml`, `colony/Makefile` — cluster and dev scripts
- `TODO.md`

**Off-limits — other lanes own these, do not edit:**

- `colony/agents/` — Lane 2 (robot agents)
- `colony/sim/` — Lane 3 (sim world, tick server, websocket protocol)
- `colony/client/` — Lane 3 (renderer, scoreboard, fog of war)
- `colony/world/` — Lane 5 (map authoring) with Lane 3 loading it
- `colony/bedrock/` — Lane 2 owns planning; Lane 1 only consumes embeddings

If a Lane 1 change requires a change in one of those directories, stop and ask
Praneeth rather than editing across the boundary.

## Interface freeze

**The `fleetmem` SDK public signatures freeze Aug 3** (§5.2 contract 1). Lanes 2
and 4 build against the in-memory fake from day 1, so a signature change breaks
their work silently.

After Aug 3, never change a public `fleetmem` signature without asking Praneeth
first. Additive, non-breaking changes still need the ping — §5.2's words are "a
team ping, not a silent commit". The frozen set:

```
report_observation, claim_task, complete_task, get_beliefs,
heartbeat (status + lease renewal), log_plan, log_event
```

## Source of truth — schema DDL (§4.5)

Grouped by memory type (§4.0); these comments ship in the migration.

```sql
-- ═══ WORKING MEMORY — what is true right now ═══════════════════════

CREATE TABLE robots (
  id STRING PRIMARY KEY, role STRING NOT NULL,
  pos_x INT, pos_y INT, battery INT, status STRING,
  current_task UUID, heartbeat_at TIMESTAMPTZ   -- for lost-marking/UI only
);

CREATE TABLE tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id UUID NOT NULL, kind STRING NOT NULL,
  target_x INT, target_y INT, priority INT DEFAULT 1,
  status STRING NOT NULL DEFAULT 'blocked',
  depends_on UUID[],            -- unblock when all done
  claimed_by STRING, claimed_at TIMESTAMPTZ,
  lease_expires_at TIMESTAMPTZ, -- v3.1: ownership is a lease
  done_at TIMESTAMPTZ,
  INDEX (mission_id, status, lease_expires_at)
);

CREATE TABLE victims (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id UUID, pos_x INT, pos_y INT,
  state STRING NOT NULL DEFAULT 'located',
  vitals_deadline INT, reported_by STRING, confidence FLOAT
);

CREATE TABLE hazards (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id UUID, kind STRING, area JSONB, severity INT, active BOOL
);

-- ═══ EPISODIC MEMORY — what we experienced ═════════════════════════

CREATE TABLE observations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id UUID, robot_id STRING, kind STRING,
  pos_x INT, pos_y INT, payload JSONB,
  embedding VECTOR(512),        -- Titan V2 @ 512 dims
  confidence FLOAT, sightings INT DEFAULT 1,
  observed_at TIMESTAMPTZ DEFAULT now()
);
CREATE VECTOR INDEX obs_embedding_idx ON observations (embedding);

-- ═══ PROVENANCE MEMORY — why we acted ══════════════════════════════

CREATE TABLE events (           -- append-only mission log; powers replay
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id UUID, at TIMESTAMPTZ DEFAULT now(),
  actor STRING, verb STRING, detail JSONB
);

CREATE TABLE plans (            -- v3.1: every Bedrock decision + its sources
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id UUID, robot_id STRING,
  at TIMESTAMPTZ DEFAULT now(),
  trigger STRING,               -- idle | task_done | world_changed | aftershock
  chosen JSONB,                 -- {task_id | explore(sector) | return_to_base}
  rationale STRING,
  based_on UUID[],              -- observation/hazard rows in the prompt digest
  INDEX (mission_id, robot_id, at)
);

-- ═══ SEMANTIC MEMORY — what we learned across missions (P1) ════════

CREATE TABLE mission_memories (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  summary STRING, embedding VECTOR(512), outcome JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

## Source of truth — claiming SQL (§4.4)

```sql
UPDATE tasks
SET status='claimed', claimed_by=$robot, claimed_at=now(),
    lease_expires_at = now() + INTERVAL '15 seconds'
WHERE id=$task
  AND (status='open'
       OR (status IN ('claimed','in_progress') AND lease_expires_at < now()))
RETURNING id;  -- serializable isolation: exactly one winner, always — and a
               -- dead robot's task is claimable the moment its lease lapses
```

Lease renewal, every ~5s, so three renewals can be missed before expiry:

```sql
SET lease_expires_at = now() + INTERVAL '15 seconds'
WHERE claimed_by=$robot AND status IN ('claimed','in_progress')
```

All expiry math uses database `now()`. Robot-local clocks are never trusted, so
clock skew cannot cause a false takeover.
