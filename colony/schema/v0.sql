-- Colony schema v0 (PRD §4.5)
--
-- Validated against CockroachDB v26.2.5 by tests/test_schema.py, against a live
-- instance — this file is not taken on faith.
--
-- One correction to the PRD's draft DDL, found during that validation: the
-- observations vector index must name `vector_cosine_ops`. See the comment on
-- the index below.
--
-- Idempotent: safe to re-run against an existing database.

CREATE TABLE IF NOT EXISTS robots (
  id            STRING PRIMARY KEY,
  role          STRING NOT NULL,
  pos_x         INT,
  pos_y         INT,
  battery       INT,
  status        STRING,
  current_task  UUID,
  heartbeat_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tasks (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id  UUID NOT NULL,
  kind        STRING NOT NULL,
  target_x    INT,
  target_y    INT,
  priority    INT DEFAULT 1,
  status      STRING NOT NULL DEFAULT 'blocked',
  depends_on  UUID[],                 -- unblocks when every dependency is done
  claimed_by  STRING,
  claimed_at  TIMESTAMPTZ,
  done_at     TIMESTAMPTZ,
  INDEX (mission_id, status)
);

-- The reconcile gate (§4.2 step 3) searches this table by cosine similarity, so
-- the index has to be built for cosine. Two deviations from the PRD's draft DDL,
-- both verified with EXPLAIN against v26.2.5:
--
--   1. `vector_cosine_ops` is required. A bare `CREATE VECTOR INDEX (embedding)`
--      builds a vector_l2_ops index, and a `<=>` query against it silently falls
--      back to a full scan — correct answers, no acceleration, and it looks fine
--      until the table is big enough to matter.
--   2. `mission_id` leads as a prefix column. Every gate query is scoped to one
--      mission, and prefix columns only engage when constrained to an exact
--      value, which ours always are. EXPLAIN shows `vector search` with prefix
--      spans instead of a scan.
--
-- Declared inline rather than as a separate CREATE VECTOR INDEX so the operator
-- class travels with the table definition.
CREATE TABLE IF NOT EXISTS observations (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id   UUID,
  robot_id     STRING,
  kind         STRING,
  pos_x        INT,
  pos_y        INT,
  payload      JSONB,
  embedding    VECTOR(512),           -- Titan V2 @ 512 dims
  confidence   FLOAT,
  sightings    INT DEFAULT 1,
  observed_at  TIMESTAMPTZ DEFAULT now(),
  VECTOR INDEX obs_embedding_idx (mission_id, embedding vector_cosine_ops)
);

CREATE TABLE IF NOT EXISTS victims (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id       UUID,
  pos_x            INT,
  pos_y            INT,
  state            STRING NOT NULL DEFAULT 'located',
  vitals_deadline  INT,
  reported_by      STRING,
  confidence       FLOAT
);

CREATE TABLE IF NOT EXISTS hazards (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id  UUID,
  kind        STRING,
  area        JSONB,
  severity    INT,
  active      BOOL
);

-- Append-only mission log; powers replay and every §4.7 metric.
CREATE TABLE IF NOT EXISTS events (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id  UUID,
  at          TIMESTAMPTZ DEFAULT now(),
  actor       STRING,
  verb        STRING,
  detail      JSONB
);

-- P1 cross-mission learning.
CREATE TABLE IF NOT EXISTS mission_memories (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  summary     STRING,
  embedding   VECTOR(512),
  outcome     JSONB,
  created_at  TIMESTAMPTZ DEFAULT now()
);
