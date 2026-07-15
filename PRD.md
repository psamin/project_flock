# PRD — Swarm: A Retrieval-as-Policy Memory Substrate for Robot Fleets on CockroachDB

**Status:** Draft v1 · **Owner:** Praneeth Samineni · **Context:** 6-month CockroachDB Vector hackathon
**One-liner:** A distributed, always-on vector store where the database *is* the control policy for a fleet of robots — every control tick is a filtered nearest-neighbor query, and every outcome is written back so the fleet learns from each other's failures in real time.

---

## 1. Background & problem

Robot manipulation policies (VLAs like OpenVLA / π0) compress thousands of demonstrations into frozen weights. Once trained, the demonstrations are gone and the model can't cheaply absorb a new correction — you have to fine-tune or retrain. When one robot in a fleet learns to recover from a failure (or a human rescues it), that lesson is stranded on that robot and that moment.

The original framing for this project treated a shared vector store as *memory that assists a frozen VLA*: retrieve a correction, inject it into the model's prompt, hope the model obeys. That interpretive link — "does the frozen VLA actually translate a retrieved correction into a better grasp?" — is unproven and fragile, and it makes the headline demo ("Site-B succeeds first try") look staged.

**This PRD adopts a different bet.** We remove the trained policy head entirely and make retrieval the decision-maker: a non-parametric controller in the tradition of VINN (Visual Imitation through Nearest Neighbors), Behavior Retrieval, and semi-parametric imitation. The research is established; the contribution here is systems: turning that in-memory, single-machine kNN controller into a **distributed, filtered, multi-region, always-on store shared by a whole fleet** — which is exactly what a CockroachDB vector hackathon should stress.

The strategic reason this fits CockroachDB specifically: retrieval-as-policy moves the vector index from a cron-job (queried a few times per episode) to the **hot path** (queried every ~100 ms per robot). That is what makes distributed low-latency ANN, high write throughput, and multi-region consistency actually matter rather than being decoration.

---

## 2. Goals / non-goals

### Goals
1. Implement a working retrieval-as-policy control loop where the action executed each tick is computed **entirely** from a filtered kNN query against CockroachDB — no trained policy head.
2. Demonstrate real-time fleet learning: a correction written at one site is retrievable by every robot in the fleet the instant its transaction commits.
3. Stress the four load-bearing CockroachDB roles under a realistic control-rate workload: vector index, transactional state/queue, lineage/audit, and multi-region.
4. Ship a demo on LIBERO sim with a binary, honest hero moment (Site-A failure → human rescue → Site-B first-try success → kill the store → fleet freezes).
5. Make safety legible: show a *bad* retrieved neighbor getting rejected by a guard, not just a clean success.

### Non-goals
- Training or fine-tuning a policy network. The only neural net in the loop is a **frozen** perception encoder.
- Beating SOTA task success rates. We're demonstrating a systems capability, not a manipulation benchmark.
- Real hardware. LIBERO sim only; "sites" and "regions" are simulated but mapped to real CockroachDB regions.
- Long-horizon or contact-rich precision tasks where kNN control is known to be weak (see §11).

---

## 3. The core bet: the database is the policy

**Trained policy = closed-book exam** (you memorized until you "just know"; the demos are baked into weights and gone).
**Retrieval-as-policy = open-book exam** (you never internalize; you look up the closest worked examples each tick and reuse their actions). The knowledge lives in the store, not the weights.

Consequences that make the demo honest:
- **No interpretive gap.** The retrieved thing is a *motor action*, executed directly. There is nothing for a model to "obey."
- **Kill-the-store = freeze, not degrade.** There is no fallback policy because none was trained. Cut the store and there is literally no action to compute. Binary and visible on stage.
- **Propagation is a fact, not a mechanism.** Site B's policy *is* a query against the shared store. The instant Site A's rescue row commits, it's a candidate neighbor for Site B's next kNN. CockroachDB's transactional freshness guarantees a committed vector is immediately searchable — so "propagates fleet-wide instantly" is just "the row exists, so the query returns it."

---

## 4. Users & actors (the swarm)

Two surfaces on one store.

### Runtime swarm (on the critical path, per robot)
- **Perception step** — frozen encoder (DINO / pretrained ResNet) turns the observation into a query vector. Perceives only; never decides.
- **Retrieval Agent** — issues the filtered kNN query and aggregates retrieved actions into the tick's action.
- **Writeback Agent** — records (embedding, action, outcome, structured metadata) as one atomic row + vector.
- **Intervention Agent** — when a human takes over a failing robot, captures the rescue trajectory and writes it as new retrievable rows.

### Flywheel swarm (async, same store) — now safety-critical, not nice-to-have
Because retrieved rows are executed directly, these agents gate what is allowed into the retrieval pool that *is* the policy:
- **Scoring / Critic Agent** — VLM-based episode quality rating (Robometer-style). Noisy; used as a gate input, not a sole oracle.
- **Novelty Agent** — dedupes new rows against existing memory by vector similarity (ideally at write time).
- **Coverage Agent** — clusters the embedding space to surface gaps ("no low-light reflective-item corrections yet").
- **Curator Agent** — model-aware active learning; versions episodes into training-set snapshots with full lineage (episode → dataset version → model version → eval).

---

## 5. The four load-bearing CockroachDB roles

| Role | What it does here | Why CockroachDB (verified) |
|---|---|---|
| **Vector index** | Retrieval (hot path), novelty, coverage | C-SPANN distributed ANN (preview, v25.2+); filtered ANN via prefix columns; RaBitQ quantization keeps index small; real-time transactional freshness |
| **Transactional state** | Swarm work queue, snapshot versioning | ACID / SERIALIZABLE by default; `SELECT ... FOR UPDATE SKIP LOCKED` claim pattern; resumable on agent death |
| **Lineage / audit** | Regression tracing, provenance | Vectors live in the same rows/queries/txns as structured data — no dual-write drift |
| **Multi-region** | Cross-site federation | `crdb_region` as vector-index prefix column → site-local low-latency reads + data domiciling; survives region failure |

**The one thing a bolt-on vector DB can't do:** embedding, structured metadata, and quality score all update in a **single transaction**, so the semantic and operational layers never drift. This is the crown jewel and should be led with.

---

## 6. Functional requirements

### The control loop (per tick, target 10 Hz)
1. **Embed** the current observation (+ optional robot state) via the frozen encoder → query vector (e.g. 512-dim).
2. **Filtered kNN** in CockroachDB: nearest neighbors to the query vector `WHERE sku = ? AND lighting = ? AND ...`, joining ANN similarity with structured predicates in one query. Each neighbor carries the action taken then.
3. **Guard** (see §7): check top-k agreement and nearest-neighbor distance before trusting the result.
4. **Aggregate** the k retrieved actions → one action (distance-weighted soft kNN à la VINN, or single-nearest).
5. **Execute**, observe result.
6. **Writeback** the new (embedding, action, outcome, metadata) row in one atomic transaction — now retrievable fleet-wide.

FR-1. Every executed action MUST be computed solely from the retrieval result (no policy head).
FR-2. Writeback MUST be atomic across embedding + structured fields + score.
FR-3. A committed rescue row MUST be retrievable by any fleet robot on its next query with no additional sync step.
FR-4. Work items MUST be claimed exactly once; a dead agent's item MUST be resumable by another with zero data loss.
FR-5. The retrieval query MUST support structured filters combined with vector similarity in a single statement.

### Runtime swarm
FR-6. Retrieval p99 latency MUST fit the control budget (target ≤ 30–50 ms p99) or the robot MUST fall back to a defined safe behavior (hold pose), not stall indefinitely.
FR-7. Intervention Agent MUST convert a human rescue into retrievable rows tagged as high-priority provisional corrections.

### Flywheel swarm
FR-8. Novelty check SHOULD run at write time to prevent unbounded near-duplicate growth.
FR-9. Curator MUST version snapshots with full lineage linking episode → dataset version → model version → eval.
FR-10. Coverage MUST be able to report embedding-space gaps as a queryable output.

---

## 7. Guardrails (the honest catch)

kNN-as-policy fails when neighbors disagree or when a poisoned neighbor is executed directly. Guards are mandatory, not optional:

- **Agreement check** — if the top-k actions have high variance (they point in contradictory directions), do NOT average them into a nonsense mean action. Refuse/slow/hold instead.
- **Distance threshold** — if the nearest neighbor is far (out of distribution), you're extrapolating. Refuse or reduce speed.
- **Pool gating** — only rows above a Scoring-Agent quality bar and passing Novelty enter the retrievable pool. A single bad human rescue must not silently become fleet policy.
- **Provisional tier** — human rescues propagate fast but flagged provisional until the async flywheel validates them; this weakens "instantly" slightly but is the honest design and prevents n=1 poisoning.

**Demo beat:** show a bad neighbor getting *rejected* by the agreement/distance guard. Safe-by-rejection is more convincing to judges than lucky-by-success.

---

## 8. Data model (illustrative)

```sql
-- Episodes / per-tick memory rows
CREATE TABLE memory (
  crdb_region     crdb_internal_region NOT NULL,   -- site → region prefix
  id              UUID DEFAULT gen_random_uuid(),
  episode_id      UUID NOT NULL,
  observation_emb VECTOR(512) NOT NULL,             -- query/retrieval vector
  action          JSONB NOT NULL,                   -- the motor action taken
  gripper_state   STRING,
  outcome         STRING,                           -- success | fail | rescue
  quality_score   FLOAT,                            -- from Scoring Agent
  provisional     BOOL DEFAULT true,                -- gated until flywheel validates
  -- structured filters, used as index prefix + WHERE predicates
  sku             STRING, bin STRING, site STRING, lighting STRING,
  created_at      TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (crdb_region, id)
);

-- Filtered + regional ANN: prefix columns make the filter cheap
CREATE VECTOR INDEX idx_mem_region_sku
  ON memory (crdb_region, sku, observation_emb);

-- Work queue for the swarm (claim with FOR UPDATE SKIP LOCKED)
CREATE TABLE work_queue (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  episode_id UUID, task STRING, state STRING,       -- pending|claimed|done
  claimed_by STRING, claimed_at TIMESTAMPTZ, payload JSONB
);

-- Lineage / provenance
CREATE TABLE dataset_versions ( ... );  -- episode ↔ dataset ↔ model ↔ eval
```

Notes grounded in CockroachDB behavior: keep vectors < 1 MB; prefer **small insert batches** (large VECTOR batches degrade write perf); tune recall vs latency with `vector_search_beam_size` rather than rebuilding; the region prefix gives site-local reads and domiciling for free.

---

## 9. Demo & success criteria

### Hero sequence (LIBERO sim, two simulated sites mapped to two CDB regions)
1. Site-A robot attempts a grasp on a hard case (e.g. reflective item, dim lighting) → **fails**.
2. Human takes over → **rescue** trajectory captured by Intervention Agent → written as provisional correction rows.
3. Site-B robot hits a similar observation → its next kNN retrieves the just-committed rescue → **succeeds first try**. No retrain, no sync step — the row simply exists.
4. **Kill the store** live → fleet has no policy to compute → robots **freeze** (binary, visible).
5. **Resilience beat:** kill an *agent* mid-task → another claims the persisted work item and resumes with zero data loss. Optionally kill a *region* mid-loop → robots keep querying surviving replicas.
6. **Safety beat:** inject a bad neighbor → guard rejects it → robot holds instead of executing garbage.

### Success criteria
- Retrieval p99 within control budget under sustained fleet-scale QPS (see §10).
- Site-B first-try success rate on the seeded scenario measurably above baseline (no shared memory).
- Zero lost/duplicated work items across induced agent kills.
- Committed correction retrievable fleet-wide with no manual sync.
- At least one guard-rejection demonstrated on a genuinely bad neighbor.

---

## 10. Load model (why this is a real vector benchmark)

At 10 Hz across a 100-robot fleet:
- **~1,000 filtered-ANN reads/sec**, sustained, under a hard ~30–50 ms p99 budget.
- **~1,000 embedding writes/sec** (per-tick writeback).

Stackable amplifiers (turn on progressively):
1. **Per-frame episodic memory** — write every frame, not just episode summaries → tens of millions to billions of vectors; Novelty dedupes at write time. Maxes index volume + compaction.
2. **Stigmergy / shared vector field** — robots write "pheromone" embeddings others read before acting → every robot is a heavy reader *and* writer of the same index simultaneously. Worst-case contention, best-case showcase.
3. **Multi-region federated policy** — the CockroachDB-specific flex that pgvector-on-one-node cannot do: regional-by-row for site-local corrections, global for shared, survive a region failure mid-loop.

**Headline config for judging: retrieval-as-policy + multi-region.** The first makes the vector index the hot path; the second makes it *have to be* CockroachDB.

---

## 11. Risks & open questions

| Risk | Impact | Mitigation |
|---|---|---|
| CockroachDB vector index is **preview** (v25.2+) | Whole hot path bets on it | Benchmark filtered-ANN recall + p99 at target write rate **first**, before architecting around it; tune `vector_search_beam_size` |
| **Latency is now safety-critical** — a p99 spike stalls a robot | Robot freeze mid-task | Follower/stale reads for the policy query; defined hold-pose fallback (FR-6); small local cache |
| **kNN control is limited** to easier tasks (tabletop, non-contact-rich) | Demo scope | LIBERO *is* tabletop — scope to where VINN-style control is known to work; avoid long-horizon/precision |
| **Poisoning executes directly** — bad neighbor → bad action | Safety | Agreement + distance guards; pool gating; provisional tier (§7) |
| **Neighbor disagreement** → averaged nonsense action | Bad motions | Variance check before aggregation; fall back to single-nearest or refuse |
| **Retrieval key mismatch** — item-visual similarity ≠ failure-mode similarity | Confident wrong retrievals | Consider action-/failure-aware embeddings; use structured filters to constrain |
| **Small-batch write requirement** vs high write rate | Throughput ceiling | Batch tuning; regional sharding of writes; measure early |
| **Embedding-version drift** over time (encoder swap) | Stale/incompatible vectors | Version the encoder; plan re-embedding path (out of scope for demo, flagged) |
| "Swarm" is really a **resumable job queue** (`SKIP LOCKED`) | Framing risk with infra judges | Own it honestly; the value is exactly-once claim + resume, not novelty |

**Open questions:**
- Where do embeddings get computed — on-robot (latency/compute cost) or centrally?
- What exactly does the guard threshold on, and how is it tuned per task?
- How is "similar situation" scoped — pure vision embedding, or vision + proprioception?
- Does "exactly once" claim need to be reconciled with the fact that the physical action already happened (not end-to-end exactly-once)?

---

## 12. Milestones (6 months)

**M1 — Foundation (weeks 1–4).** Stand up multi-region CockroachDB cluster with vector index; benchmark filtered-ANN recall + p99 at target QPS/write-rate; freeze the perception encoder; ingest a LIBERO demo set as (embedding, action) rows. *Gate: p99 within budget or redesign.*

**M2 — Retrieval-as-policy loop (weeks 5–8).** Single-robot closed loop: embed → filtered kNN → aggregate → execute → writeback at 10 Hz on LIBERO. Baseline task success with retrieval-only control.

**M3 — Guards + writeback quality (weeks 9–12).** Agreement + distance guards; Scoring/Novelty gating; provisional tier. Demonstrate a rejected bad neighbor.

**M4 — Fleet + intervention (weeks 13–17).** Multiple robots on shared store; Intervention Agent for human rescue; work queue with `SKIP LOCKED` claim + resume-on-death; kill-an-agent resilience.

**M5 — Multi-region federation (weeks 18–21).** Two simulated sites → two regions via `crdb_region` prefix; Site-A→Site-B propagation; region-failure survival mid-loop.

**M6 — Flywheel + hero polish (weeks 22–26).** Coverage/Curator + lineage snapshots; end-to-end hero sequence (§9) rehearsed; load amplifiers (per-frame / stigmergy) as stretch; final benchmark numbers.

---

## 13. Metrics

- **Hot-path:** filtered-ANN p50/p99 latency; sustained read + write QPS; recall@k of the ANN vs exact kNN.
- **Learning:** Site-B first-try success rate with vs without shared memory; time from rescue-commit to fleet-retrievable.
- **Resilience:** lost/duplicated work items under N induced agent kills (target 0); loop continuity under region failure.
- **Safety:** guard-rejection rate on injected bad neighbors; false-freeze rate (guard refuses when it shouldn't).
- **Store health:** vector count growth, dedup ratio from Novelty, index build/compaction cost.

---

## 14. Out of scope (v1)
Policy training/fine-tuning · real hardware · non-LIBERO tasks · production security/RLS hardening · encoder re-embedding pipeline · human-in-the-loop UI beyond rescue capture.
