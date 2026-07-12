# Project Flock — Database Migration Copilot with Swarm Verification

> Migrate any database to CockroachDB — with the **human playbook automated as agents**: risks audited, mitigations decided, the runbook driven end-to-end, every row verified by a **swarm**, and every lesson kept. The copilot's memory **compounds**: each migration makes the next one smarter. Underneath it all, one durable, strongly-consistent shared memory: CockroachDB — the destination database is also the migration's brain.

**Hackathon:** CockroachDB × AWS — Build with Agentic Memory
**Version:** v2 — restructured around the human-steps → agents mapping (§2), an explicit agent roster (§3), and a settled memory design (§4)

**One-liner:** MOLT moves and checks the bytes, deterministically and well. **Flock is everything that's currently a human:** it assesses, plans, decides, drives, explains, and remembers — with human sign-off kept at exactly three gates.

---

## TL;DR

A Postgres → CockroachDB migration today is four good CLI tools (MOLT Convert / Fetch / Replicator / Verify) glued together by a human: reading Convert's report against the docs, writing the pre-mortem by hand, resolving every schema finding into a DDL decision, babysitting the load, interpreting verification logs, syncing sequences manually after load, making the cutover call from terminal output, and carrying the lessons out the door when it's over. Flock automates that glue as a roster of agents (§3), each mapped 1:1 to a step of the standard playbook (§2), with humans kept at exactly the three points where judgment should stay human: the risk register, the plan + DDL, and the final cutover.

Two mechanisms make it more than a wrapper. A **gap-audit engine** — mechanical rule-matching over structured schema *and workload* descriptors, seeded with curated quirks and *sharpened by every migration it has run* — produces a provenance-linked risk register and opportunity report. And a **swarm** of verifier agents claims per-object checks atomically (`FOR UPDATE SKIP LOCKED`), runs hierarchical checksum diffs *in-engine across the two databases*, and votes on cutover in one serializable transaction — coordinating entirely through CockroachDB, which acts simultaneously as **task queue, blackboard, pub/sub bus, vector memory, and audit backend**. Every run ends in reflection: the trajectory is distilled into engine-keyed rules with provenance and a lifecycle (`seeded → candidate → confirmed`), stored in CockroachDB's vector index (**the memory**) and rendered to markdown artifacts (**the human view**, §4). Run 2 provably beats run 1 because the memory improved, not the model.

---

## The focus: two pillars (read this first)

The hackathon's center of gravity is one sentence from the brief: *"memory is not an afterthought, it is the thing that makes an agent useful in production."* Flock is built as two proofs of that sentence; everything else in this document is plumbing, sized accordingly.

- **Pillar 1 — working memory at swarm scale.** The verification swarm *is* the brief's motivating image: hundreds of ephemeral Lambda workers spawning autonomously, writing claims/verdicts/heartbeats constantly, dying mid-task while the memory survives and self-heals. Lambda workers are ephemeral by construction — "stateless agents, durable CockroachDB memory" is the runtime model, not a slogan.
- **Pillar 2 — experience memory that compounds.** Migration gives memory a reason to exist beyond coordination: quirks recur across migrations, so remembering pays. The proof is behavioral and takes exactly two runs (§14): run 1 fails on an injected divergence and distills the lesson; run 2 — the *same* migration — flags the problem *before execution*, citing the rule it learned. Better because the memory improved, not the model.

**The submission in one sentence:** a migration copilot whose swarm coordinates through CockroachDB and whose second migration is provably better than its first — because the memory, not the model, improved.

Sized as plumbing in service of the pillars: schema conversion and bulk load (deterministic — MOLT Fetch, wrapped); the gap audit (the *surface where memory becomes visible* — ten curated quirks, not an expert system); the external OTel viewer (garnish — the CockroachDB-backed ledger + dashboard are the observability story). Outside the blast radius entirely (P2): day-2 mode, MySQL, dependency analysis, CDC cutover.

---

## 1. The problem — what's still human after MOLT

Cockroach Labs already ships good deterministic tooling: MOLT Convert flags DDL incompatibilities, Fetch moves data with checkpointed continuation, Replicator streams changes for minimal-downtime cutover with failback, and Verify compares rows concurrently. The gaps that remain are precisely the *human* gaps — and they're the ones this hackathon is about:

1. **Dialect gaps live in the workload, and nothing reads the workload.** Convert audits an uploaded `.sql` file. Nothing in the standard flow reads `pg_stat_statements` or the query log — so a gap that lives in a *query* rather than in DDL (an isolation-sensitive transaction shape, a `LISTEN/NOTIFY` consumer, a lag-tolerant read pattern) is invisible until production. App-semantic differences (serializable retry errors, contention) are documented as "optimize your queries" — i.e., left entirely to the human.
2. **Verification is a single process a human babysits and interprets.** MOLT Verify is multi-threaded but runs as one process on one machine: it ships 20k-row batches over the network to compare in the binary, dies without self-healing, can't compare geospatial types, trips on collated string PKs, and emits *logs* — a human reads `num_mismatch` and conditional-success warnings and *judges* whether the migration is clean. The verdict is a human's interpretation of terminal output, not a queryable fact.
3. **The knowledge doesn't compound.** Each migration is a one-off, consultant-heavy effort — Cockroach's own answer to this gap is a paid migration service with engineers guiding you through the cutover window. What the last run taught about *this engine's quirks* leaves with the people.
4. **Agent systems can't be trusted in production.** Surveys put ~95% of enterprise AI pilots as never reaching production — not for lack of model capability, but for lack of architectural robustness, governance, and traceability. A black-box agent that touches your data is a non-starter.

**What we're really building:** the *human layer* of database migration, automated with agents and made auditable — where memory makes it smart, a swarm makes it fast, and observability makes it trustworthy. We never compete with MOLT on moving bytes; we replace the person driving it.

---

## 2. The playbook, step by step — every human step becomes an agent

This is the product spec. The left columns are the standard migration sequence as Cockroach's own docs describe it — including the parts the docs explicitly assign to humans (the pre-mortem, sequence creation, query optimization). The right columns are the Flock agent that owns each step and what, if anything, stays human.

| # | Playbook step | What the human does today | Flock owner | What stays human |
|---|---|---|---|---|
| 1 | **Assess & discover** | Inventory the source, read Convert's report against the feature-support docs, list what won't map | **Introspector** (deterministic) + **Auditor** | Nothing — output is the register |
| 2 | **Write the pre-mortem** | The docs literally instruct teams to write one: likely failure points, ranked severity, mitigations — on a wiki | **Auditor** — the risk register *is* the pre-mortem, generated with per-finding provenance | **Gate 1: approve the register** |
| 3 | **Plan the migration** | Choose the flow (planned downtime vs replication), sequence the steps, write the runbook | **Planner** | **Gate 2: approve plan + DDL** |
| 4 | **Prepare the environment** | Connectivity, users/permissions, buckets, replication prereqs (`wal_level=logical`, slot capacity) | **Preflight** — deterministic checks; red blocks the run | Fix whatever Preflight flags |
| 5 | **Convert schema + resolve findings** | Run Convert, then resolve each incompatibility by hand: INT width, sequences → UUID or hash-sharded, triggers, unsupported types | **Conversion Resolver** — one decision per finding via mitigation templates, whitelist-validated | DDL diff approved as part of Gate 2 |
| 6 | **The DDL dance** | Apply schema; drop constraints/indexes for load; recreate after | **Load Driver** sequences it | — |
| 7 | **Drive the bulk load** | Run `molt fetch` with the right flags, watch it, resume from checkpoints on failure | **Load Driver** — wraps MOLT Fetch, parses its structured output, auto-continues | — |
| 8 | **Sync sequences** | The official flow leaves sequential keys as a *manual post-load step*; restart values set by hand — the classic post-cutover duplicate-key incident | **Sequence Reconciler** + a first-class `sequence_state` check kind | — |
| 9 | **Verify & interpret** | Run `molt verify`, read the logs, judge whether the warnings matter | **Verifier swarm** + **Quorum** — verdicts are rows; the gate is a serializable transaction, not a judgment call over logs | — |
| 10 | **Diagnose failures** | Grep logs, guess, retry | **Diagnosis agent** — root-cause postmortem written to memory | — |
| 11 | **The cutover call** | Go/no-go decided from terminal output; pause writes; flip traffic | **Gate** arms cutover only on all-leaf-pass | **Gate 3: confirm cutover** |
| 12 | **Capture the lessons** | Retro doc nobody reads; knowledge leaves with the engineers — or was rented from the migration-services team | **Reflection agent** — trajectory → rules with provenance and lifecycle status | — |
| 13 | **Render the paper trail** | Write the runbook / report / retro by hand | **Scribe** — risk report, plan, runbook, postmortems rendered to markdown from the DB | Read them |

**What deliberately stays human (and only this):** the three gates — register, plan + DDL, cutover — plus application-side code changes. Retry loops for serialization errors (`40001`), ORM behavior under Serializable, replacing `LISTEN/NOTIFY` consumers: Flock **flags these as informational findings with effort estimates** (the Auditor sees the transaction shapes in the workload descriptors) but never edits application code. Flagged, not fixed, is the honest boundary.

**Day-2 mode (P2, one paragraph, on purpose):** once you're on CockroachDB, the same loop points inward — source = the live cluster, snapshot = pre-change backup + pinned timestamp, execute = online schema change, verify = old-vs-new `AS OF SYSTEM TIME` diffs, rollback = `RESTORE`. Same memory, same swarm, same ledger; every day-2 change keeps feeding the memory the replatform built. Nothing else in this document spends words on it.

**Direction is deliberately one-way:** sources → CockroachDB. Rules are keyed by source *and* target engine, so bidirectional is future work, not a rewrite. PostgreSQL is P0; MySQL and others are pluggable quirk packs later. **Ecosystem fit in one line:** MOLT moves and checks the bytes; Flock is the layer Cockroach currently staffs with humans and consultants — it assesses, plans, decides, explains, and remembers.

---

## 3. The agent roster

One agent per human step, each with a hard rule about where the LLM sits: **the LLM plans, narrates, diagnoses, and reflects; deterministic code detects, executes, and verifies.** No agent lets a model freely mutate data or DDL — models emit *parameters for deterministic tools*, validated against whitelists.

| Agent | What it does | LLM? | Human gate |
|---|---|---|---|
| **Preflight** | Connectivity, permissions, `wal_level=logical`, replication-slot capacity, bucket access, target reachability. Hard-blocks on red | No (LLM only explains a failure) | Blocks until green |
| **Introspector** | Catalogs + `pg_stat_statements` → **structured feature descriptors** (JSON): types, sequences, triggers, extensions, collations, constraint shapes, query/transaction shapes | No | — |
| **Auditor** | Descriptors × rules → findings. Retrieval: exact `(source_engine, feature)` key match first, vector recall second. **Detection is mechanical; the LLM writes the narrative per finding** | Narration only | Gate 1 |
| **Planner** | Drafts the migration plan grounded in the audit + recalled history; emits converter mappings (from a whitelist), fetch flags, verification decomposition, cutover steps | Yes (strong tier) | Gate 2 |
| **Conversion Resolver** | Per approved finding, applies the mitigation template → concrete DDL decisions; whitelist-validated type mappings | Template-driven | DDL diff in Gate 2 |
| **Load Driver** | Sequences the DDL dance; wraps **MOLT Fetch** — constructs flags, parses structured output, resumes from checkpoints | No on happy path; LLM explains failures | — |
| **Verifier swarm** | Claims leaf checks, runs in-engine checksum diffs across both databases, writes verdicts; stateless, leased, heartbeating | **Never** — pure SQL, zero tokens | — |
| **Sequence Reconciler** | Post-load: creates sequences, sets restart = `max(pk) + headroom`, emits `sequence_state` checks into the same queue | No | — |
| **Quorum / Gate** | Serializable evaluation: all *leaf* checks resolved ∧ zero failed ⇒ arm cutover | No | Gate 3 |
| **Diagnosis** | On no-go: reads the failed check's trace + recalled incidents → structured root-cause postmortem | Yes (strong tier) | — |
| **Reflection** | Trajectory + postmortems → candidate rules; dedupe/link; lifecycle transitions | Yes | — |
| **Scribe** | Renders DB state → markdown artifacts (risk report, plan, cutover runbook, postmortems) to S3 + repo. Makes no decisions | Light | — |

The copilot that sequences these is a **control plane**: sequential, human-gated, and resumable from the `migrations` row — kill it mid-run, restart it, and it picks up at the recorded stage. The verification pass is the **data plane**: orchestrator-free, coordinated entirely through the blackboard + changefeeds. "No central orchestrator" is claimed for the data plane only; the control plane is deliberately boring.

---

## 4. Memory design — CockroachDB is the memory; markdown is the view

The open question ("md files or a vector DB?") is settled: **both, with a strict one-way arrow.**

**CockroachDB is the single source of truth for memory.** Rules, incidents, findings, and the trace ledger are rows — with a vector index for semantic recall, exact keys for deterministic retrieval, provenance as foreign keys, and transactional coupling to the coordination tables. A rule can point at the exact failed check that taught it because they live in the same database; that join is the provenance story, and it's impossible in a folder of markdown.

**Markdown artifacts are rendered views, never the store.** The Scribe regenerates the risk report, migration plan, cutover runbook, and postmortems from the DB whenever state changes, writing them to S3 and the repo — human-readable, diffable in PRs, attachable to tickets. The arrow is one-way (DB → md); nothing ever reads a markdown file back as truth.

Why not md-as-memory: no provenance keys or joins to verdicts; no transactional coupling with coordination (reintroducing exactly the dual-write race the one-substrate design kills); no vector recall; concurrent agent writers mean merge conflicts; and it forfeits one of the hackathon's named tools (the distributed vector index).

### 4.1 What memory holds

| Memory | Contents | Retrieved by |
|---|---|---|
| `memory_rules` | Distilled, engine-keyed rules with detector, mitigation template, provenance, lifecycle status, embedding | Auditor (exact key, then vector), Planner |
| Incidents / postmortems | Diagnosis output: what broke, why, what to check next time — linked to the failed check's trace | Diagnosis, Reflection, Planner |
| `gap_findings` | The risk register: per-finding severity, risk, mitigation, opportunity, rule provenance, approval | Planner, Scribe, dashboard |
| `agent_trace` | Every agent action, claim, verdict, model call — the audit ledger, time-travelable | Everything; MCP; the provenance click-through |

### 4.2 The rule schema (settling the "resolve first" item from v1 §17)

A rule is: key `(source_engine, target_engine, feature)`; a **detector** — a structured predicate or SQL over the Introspector's feature descriptors, so matching is *mechanical*; the rule text; a **mitigation template** the Resolver can apply; provenance (`source_migration`, NULL = seeded); and a lifecycle — **`seeded | candidate | confirmed`**. Reflection emits *candidates*; a rule is promoted to *confirmed* when it prevents a failure — which is literally what run 2 of the demo does, so the transition goes on the metrics panel: **caught → learned → prevented → confirmed.** Dedupe: same key + high embedding similarity ⇒ link the rules, don't merge them.

### 4.3 The retrieval rails (why run 2 can't flake on stage)

The run-2 payoff must not depend on an LLM behaving during the demo. So: the Introspector emits structured descriptors; the Auditor retrieves rules by **exact `(source_engine, feature)` key match first, vector similarity second**; whether a rule *fires* is decided by its detector, mechanically; the LLM only writes the human-readable narrative per finding. The citation renders from `rule_id → source_migration` regardless of what the model says. Run 2 flags the learned quirk even on the model's worst day — and as a side effect, the audit never needs to feed a whole schema through Bedrock.

### 4.4 The reflection pipeline

After every run, pass or fail: trajectory + any diagnosis postmortem → candidate rules (ExpeL-style distillation, "engine X feature Y bites in context Z; do W") → dedupe/link against existing rules (A-MEM-style) → embed → store with provenance. On failure, the Diagnosis agent's postmortem is the primary input. This is what makes the memory *compound*: the copilot doesn't just remember what happened — it remembers what it learned, in a form the next audit retrieves mechanically.

---

## 5. The gap audit (learn the schema *and workload*, audit the risks, suggest what's newly possible)

The step that makes Flock a *copilot* rather than a pipeline — and the surface where memory becomes visible: the audit is what looks different between a cold-memory and warm-memory run (§14). Not an expert system; ten curated, provenance-linked quirks beat fifty generic ones. Before anything executes:

1. **Introspect the source.** Schema, types, sequences, defaults, triggers, procedures, extensions, constraints, collations — plus the *workload*: representative queries and transaction shapes from `pg_stat_statements`, because dialect gaps live in queries as much as in DDL. Nothing in the standard toolkit does this half.
2. **Audit against the rules.** Every descriptor is matched against detectors (mechanically), producing the **risk register**: one finding per gap with severity, *why it breaks*, the **mitigation** (equivalent behavior on CockroachDB, or absorbed app-side), and estimated effort.
3. **Suggest opportunities.** The inverse audit: CockroachDB features that fit the *observed* usage — "your schema does X the hard way; the target does it natively."
4. **Render one artifact.** The **Migration Risk & Opportunity Report** — the generated pre-mortem, every claim citing its rule or the migration that taught it — approved by a human before any execution (Gate 1).

Illustrative seed entries (support levels verified against current docs in Phase 0 — they move fast):

| Source pattern | Risk on CockroachDB | Mitigation / opportunity |
|---|---|---|
| `SERIAL` / sequential keys | Write hotspots on ranges | UUID keys or hash-sharded indexes; sequence restart handled by the Reconciler |
| Implicit `INT` width | PG defaults to 32-bit; CockroachDB `INT` = `INT8` — silent width change | Explicit `INT4`/`INT8` decision per column in the DDL diff |
| Long / contended transactions in the workload | Serializable surfaces retry errors (`40001`) the old isolation hid | **Informational, app-side:** retry loops, shorter transactions — flagged with effort, never auto-fixed |
| `LISTEN/NOTIFY`, trigger-based outbox | Not supported / limited | **Changefeeds** — native CDC, no outbox table |
| Cron purge jobs / soft-delete sweepers | Carries over as toil | **Row-level TTL** does it declaratively |
| Stored procedures / triggers | Partial support, dialect differences | Verify support per feature; move logic app-side or to changefeed consumers |
| Full-text search | No native FTS | Trigram indexes for light cases; external search for heavy — with effort estimate |
| Read-replica lag workarounds | Unnecessary | **Follower reads** / global tables — consistent by default |
| Tables without primary keys | Range checksums need stable keys | **Degraded verification** (counts + aggregates) + a register finding — a verifier limitation turned into product behavior |

5. **The learned layer.** Every run's outcomes — which mitigations worked, which mappings bit back, what the diff caught — are distilled into rules (§4.4). The seeded rulebook makes the first migration competent; the learned rules make the fiftieth one sharp. The seed covers the head of the distribution; **learning covers the tail** — that's the answer to "why wasn't the demo quirk seeded," and it's the thesis.

---

## 6. The swarm

### 6.1 Why a swarm actually belongs here

Most "multi-agent" projects are theater — more agents don't make a single reasoning task better. Migrations are the exception, because the expensive part is **breadth, not depth**: parity per table × row-range, sequence state per sequence, presence per index/constraint. That's embarrassingly parallel.

> **Honest framing, calibrated against the real baseline.** MOLT Verify is already concurrent — the differentiator is *not* "parallel vs serial." It's architectural: **distributed** (many machines vs one process), **self-healing** (leases return a dead worker's check to the pool vs start-over), **in-engine** (checksums computed inside both databases, bytes shipped only where ranges diverge, vs 20k-row batches pulled over the network), **recursive** (divergences corner themselves), and **auditable** (verdicts are queryable rows with provenance vs logs a human interprets). The swarm's value is speed, resilience, and trust on parallelizable verification — planning stays careful and human-gated.

### 6.2 Hierarchical cross-database verification (the core)

After load, the planner decomposes verification into per-object checks. Verifiers claim atomically (`FOR UPDATE SKIP LOCKED`), checksum coarse row-ranges **in-engine on both databases**, and only where checksums diverge, recurse — spawning finer child checks back into the same queue for any verifier to claim. Cheap when nothing changed, precise where something did, visible live on the coverage heatmap. Pure SQL: **parity verifiers burn zero LLM tokens.**

**Canonicalization is the hardest technical problem in this document, named as such.** Cross-engine checksum equality requires every value to render identically on both engines: `NUMERIC` trailing zeros, float formatting, `timestamptz` precision, bool rendering, bytea encoding. Two design commitments: an **order-independent aggregate** (sum/XOR of per-row `hash(canonical_row)`) so ordering and collation drop out entirely, and a **per-type canonical-cast layer with a golden test suite** — one table per supported type, identical on both engines, asserting equal checksums — as a named Phase 0/1 exit criterion with real time allocated. This is the risk MOLT Verify sidesteps by shipping rows; we take it on deliberately to buy in-engine efficiency, and the golden suite is how we de-risk it.

### 6.3 Task lifecycle, leases, and what "zero duplicates" means

States: `pending → claimed → passed | failed | split`. A diverging parent transitions to `split` and **writes no verdict** — its children carry the truth. Long checks **heartbeat** (bump `lease_expires`) so a three-minute checksum doesn't outlive a 60-second lease and manufacture a fake duplicate. Each claim carries an **epoch**; verdicts are idempotent on `(task_id, claim_epoch)`, so at-least-once execution can't double-count. The duplicate-claim metric is precisely defined — **two distinct owners producing verdicts for the same epoch** — which is what makes "pinned at 0" falsifiable rather than decorative.

### 6.4 Check kinds beyond row parity

`parity` (hierarchical checksum), `sequence_state` (restart value ≥ max(pk) — the classic post-cutover duplicate-key incident that row checksums can't catch, and a step the official flow leaves manual), `object_presence` (indexes, constraints recreated after load), and `aggregate` (per-column count/sum/min/max — also the degraded mode for PK-less tables). All flow through the same queue, leases, and quorum.

### 6.5 Quorum cutover gate

Verifiers vote per-object results into `verify_results`. Cutover is armed **only when every *leaf* task is resolved and zero have failed** — evaluated in a serializable transaction, so recursion mid-flight can't produce a premature pass and split parents can't wedge the gate. One mismatch ⇒ **no-go: abort, and the untouched source keeps serving.** Before cutover, aborting is free — that's the replatform safety story.

### 6.6 Diagnosis on failure

Abort isn't the end. On no-go, a **diagnosis agent** (strong tier, D-Bot lineage) reads the failed check's trace + recalled incidents and writes a structured root-cause postmortem into memory, where Reflection distills it into a rule. Re-plan the same migration and the copilot cites the lesson.

### 6.7 Parallel dependency analysis *(P2)*

The swarm crawls the dependency graph and workload queries — which queries touch objects whose semantics change — each agent claiming a subgraph. Valuable; outside the demo's blast radius.

---

## 7. The copilot loop

```
connect ─▶ preflight ─▶ introspect ─▶ recall ─▶ gap audit ─▶ [G1: approve register]
   ─▶ plan + resolve conversions ─▶ [G2: approve plan + DDL] ─▶ convert ─▶ load (MOLT Fetch)
   ─▶ reconcile sequences ─▶ swarm verify ─▶ quorum ─▶ [G3: confirm cutover] ─▶ cutover
   ─▶ reflect ─▶ render artifacts
```

- **Preflight.** Deterministic environment checks; red blocks the run before anything touches data.
- **Introspect.** Catalogs + workload → structured feature descriptors (§5).
- **Recall.** Rules and incidents retrieved from the vector index — exact engine/feature keys first, similarity second: *have we migrated from this engine before? what bit us? what rule did it teach?*
- **Gap audit.** Risk register + opportunity report; Gate 1.
- **Plan + resolve.** The Planner drafts the plan; the Resolver turns each approved finding into whitelist-validated DDL decisions; Gate 2 approves plan and DDL diff together. The LLM plans; **deterministic tools execute.**
- **Convert + load.** DDL applied; the Load Driver runs the drop-indexes / MOLT Fetch / recreate sequence, parsing Fetch's output and resuming from checkpoints. The LLM never hand-copies rows.
- **Reconcile sequences.** Restart values set and verified as first-class checks.
- **Verify.** The swarm (§6).
- **Cutover gate.** Leaf-quorum all-pass **and** human confirmation ⇒ cutover. Any failure ⇒ automatic abort; the source keeps serving, unharmed.
- **Reflect + render.** Rules distilled with provenance; the Scribe regenerates the markdown artifacts.

---

## 8. One substrate, five jobs

Every serious swarm needs coordination services *and* a telemetry story. The default stack wires them as separate systems with no consistency *between* them — the dual-write races between queue, state store, bus, vector DB, and audit log are where swarms silently corrupt. Flock collapses all five into CockroachDB — **the destination database is also the migration's brain** — so claiming a check, writing its verdict, updating the quorum, and recording the audit row happen in **one atomic transaction**:

| Need | Default stack | Flock |
|---|---|---|
| Task queue (exactly-once claims) | SQS | `verify_tasks` + `FOR UPDATE SKIP LOCKED` |
| Shared state / blackboard | Postgres | `verify_results`, `migrations`, `gap_findings`, serializable txns |
| Pub/sub (wake agents on change) | Redis / Kafka | **Changefeeds** → webhook sink → Lambda |
| Vector memory (semantic recall) | Pinecone | **Distributed vector index**: incidents + rules, same rows as operational data |
| Observability / audit backend | Datadog + a separate audit store | `agent_trace` ledger — SQL-queryable, **time-travelable** with `AS OF SYSTEM TIME` |

## 9. Architecture & observability

```
  source DB (Postgres on RDS)
        │ preflight ▸ introspect (catalogs + workload)
        ▼
  recall ─▶ gap audit ─▶ [G1] ─▶ plan + resolve ─▶ [G2] ─▶ convert ─▶ load (MOLT Fetch)
  (rules,    (risk register +                                   │ ▸ reconcile sequences
   incidents) opportunity report)                               ▼
                                                    decompose verification
                                                  (per object × check kind × range)
                                                                │
            ┌──────────────────  CockroachDB (target)  ────────┴────────────┐
            │ verify_tasks │ verify_results │ gap_findings │ rules │ vectors │
            │                agent_trace (audit ledger)                     │
            └───────┬───────────────────────────────┬───────────────────────┘
        claim (SKIP LOCKED, epoch,          changefeed (webhook sink)
                heartbeat)                          │
                │                         ┌─────────┴──────────┐
                ▼                         ▼                    ▼
      ┌─────────────────────┐     ┌───────────────────┐  ┌───────────────┐
      │  verifier agents    │────▶│ quorum / gate      │  │ live dashboard │
      │  in-engine checksum │     │ leaf all-pass ⇒ arm│  │ + provenance   │
      │  source ↔ target    │     │ cutover; fail⇒abort│  │  click-through │
      └─────────────────────┘     └─────────┬─────────┘  └───────────────┘
                ▲                            │                    │
                └──────── self-heal ◀────────┘          Scribe ─▶ S3 (md artifacts)
               (expired leases re-pool; epoch++)
```

Stateless verifiers (Lambda for bursty fan-out, ECS if the pass outgrows the 15-minute ceiling); all state in the shared memory; the data plane has no orchestrator — coordination is emergent from the blackboard + changefeeds.

### 9.1 Why observability is non-negotiable

The canonical war story: an agent quietly served stale data, every health check green, and it took six hours of log-grepping because tool executions weren't first-class spans. **A silent migration break is exactly this failure**, and a swarm multiplies the opacity. Observability turns "trust me" into "look" — and it's the single biggest thing between an agent demo and a production-credible system, which is where the judging weight sits.

### 9.2 What we instrument (OTel GenAI conventions)

Vendor-neutral spans capturing the *decision graph*, not just the I/O boundary:

```
invoke_agent  (gen_ai.agent.name = "verifier")
├─ memory.retrieve      -- recall incidents + rules (vector index)
├─ llm.chat             -- gen_ai.request.model, token usage   (absent for parity verifiers)
├─ db.execute           -- the in-engine checksum diff (hot path)
└─ event: verification.verdict { object, epoch, passed, detail }
```

Agent / LLM / DB / memory-retrieval / verification span kinds; the whole migration is one connected trace, gate to gate. Content lives in span *events* (droppable at the Collector), never attributes; overhead <1% with async batch export.

### 9.3 CockroachDB *is* the observability backend

- **Durable audit ledger** — every action, audit decision, and verdict is an append-only `agent_trace` / `gap_findings` row: SQL-queryable, time-travelable ("what did the swarm believe at any past instant?").
- **Live dashboard via changefeed** for quorum wake-up and event streaming; the dashboard read path may fall back to indexed polling (§13.1) — the ledger, not the transport, is the story.
- **Provenance click-through as a first-class view:** finding → rule → run-1 postmortem → the failed check's trace. This is the demo's most memorable artifact and the MCP moment's fallback.
- **MCP as the operator's window** — the managed MCP server (read-only role, audit-logged) lets an operator interrogate live swarm state, the register, and the ledger in natural language from Claude Code.
- **External OTel viewer** (Tempo / Langfuse / OpenObserve): P2 garnish for span drill-down.

### 9.4 Three views for the demo

1. **Live swarm dashboard**: agents online, checks by state, the **duplicate-claim counter pinned at 0**, coverage heatmap filling in real time, checks/sec.
2. **Per-migration trace**: the decision graph gate-to-gate; click any span for the recall, model call + cost, diff query, verdict.
3. **Audit / provenance ledger**: who did what, when, and why — including why each gap was mitigated the way it was — reconstructable at any past instant.

### 9.5 Metrics (SLOs for a migration)

Verification coverage %, checks/sec, **duplicate-claim rate (must be 0, as defined in §6.3)**, breaking-change catch rate (from the fault-injection library), caught → learned → prevented → confirmed, mean abort time, token cost per tier — the reliability standards you hold for APIs, applied to every inference.

---

## 10. Research → real failure → mechanism

Every idea maps to a concrete failure mode — nothing adopted for novelty. **Implemented** = built into Flock; **validates design** = justifies a choice.

| Research idea | The real failure it addresses | Mechanism in Flock | Status |
|---|---|---|---|
| **Datafold DMA / data-diff** | Migration silently diverges data | **Hierarchical checksum diff** (§6.2) with in-engine canonical hashing | **Implemented** — core verifier |
| **ExpeL / A-MEM** (2024–25) | Failures are forgotten; lessons don't compound | **Reflection** (§4.4): trajectories → engine-keyed rules with provenance + lifecycle, linked to related memories | **Implemented** — core memory |
| **D-Bot / DBAIOps** (VLDB 2024–26) | Failures get rolled back but never explained | **Diagnosis agent** (§6.6): root-cause postmortem into memory | **Implemented** — P1 |
| **Illusion of Multi-Agent Advantage / AI Agents That Matter** | Multi-agent theater; benchmarks ignoring cost | **Baselines that bite** (§13.19): same harness at `max_workers=1` *and* `molt verify` on the same schema — same verdicts, N× wall-clock, no self-heal, no ledger | **Implemented** — demo evidence |
| **OTel GenAI conventions + AgentOps, AgentTrace, "Agent Traces to Trust"** | Black-box agents you can't audit | §9: standardized traces, live dashboard, queryable provenance | **Implemented** — core |
| **CrackSQL** (SIGMOD 2025) | LLM freely rewrites SQL/DDL, introduces breakage | LLM constrained to planning/auditing/narration; deterministic tools mutate. (Its dialect-translation mechanism = P2 stretch) | Validates design |
| **"Data Agents: Hype?"** survey (2025) | Over-trust in autonomous agents | Safety envelope: three gates, source untouched until cutover, verify-before-commit | Validates design |
| **Online schema change** (F1, VLDB 2013) | Blocking DDL causes downtime | Native online schema changes (day-2, P2) | Validates design |
| **Project Sid / PIANO, AgentSociety** | Large populations corrupt shared state | One strongly-consistent memory; one decision bottleneck (the quorum) | Validates design |

---

## 11. CockroachDB usage — relational *and* distributed

`(*)` = one of the hackathon's four named tools (minimum two; Flock uses three).

| Feature | Role |
|---|---|
| CockroachDB as migration *target* | The product story: Flock is the agentic on-ramp (the layer around MOLT) |
| Serializable txns + `FOR UPDATE SKIP LOCKED` | Exactly-once claiming across a concurrent swarm |
| Changefeeds (CDC) | Wake the quorum as results land; stream events — no separate bus |
| Row-level TTL + leases + epochs | Dead verifiers' checks re-pool; the pass self-heals |
| `AS OF SYSTEM TIME` | Time-travel over the audit ledger (and day-2 diffs, P2) |
| Backups via **ccloud CLI** `(*)` | Provisioning/ops scripts; day-2 snapshot + rollback path (P2) |
| **Distributed vector index** `(*)` | The compounding memory: rules + incidents, same rows as operational data |
| **Managed MCP Server** `(*)` | Read-only, audit-logged operator window into swarm state, register, provenance |
| Survivability | Kill half the swarm mid-pass; the verdict comes out identical |

## 12. AWS usage

| Service | Role |
|---|---|
| **Amazon Bedrock** | Where language lives: audit narration, planning, diagnosis, reflection; embeddings. Parity verifiers call no model |
| **Amazon RDS (PostgreSQL)** | The demo's *source* — the thing being migrated |
| **AWS Lambda** | Changefeed consumers (quorum) and bursty verifier fan-out |
| **Amazon S3** | The Scribe's rendered artifacts: risk reports, plans, runbooks, postmortems |

No SQS, Redis, external vector DB, or separate telemetry store — CockroachDB is the queue, bus, vector memory, and audit backend.

---

## 13. Design decisions (settled now, so architecture doesn't churn)

1. **Coordination bus = changefeed webhook (HTTPS) sink fronting Lambda.** No native Lambda sink exists; delivery is at-least-once ⇒ every consumer idempotent. Changefeeds are kept for **quorum wake-up**, where those semantics fit. The dashboard needs a browser path a webhook can't provide, so a small always-on service exists regardless — its read path may simply poll indexed views; a config choice, not a redesign. If the Cloud tier gates changefeeds (Phase 0 verifies), indexed polling backs the same consumer interface everywhere.
2. **Exactly-once *claiming*, at-least-once *execution*, idempotent effects.** Claims carry epochs; verdicts are keyed `(task_id, claim_epoch)` so re-runs can't double-count in the quorum (§6.3).
3. **Scale target: ~50 tables, 1,000+ leaf checks, ~300 concurrent verifiers** (config-driven). The swarm story is check count and concurrency, not table count — and fabricating 300 plausible tables with believable data is a hidden multi-day tar pit that buys nothing.
4. **Single-region cluster is the core deployment.** Chaos story = SIGKILL half the verifiers; multi-region is stretch, only after the demo is frozen.
5. **Exactly three human gates** — register (G1), plan + DDL (G2), cutover (G3). Failure needs no human: abort is automatic; the source keeps serving.
6. **LLM placement rule (roster-wide):** the LLM plans, narrates, diagnoses, reflects; deterministic code detects, executes, verifies. Parity verifiers are pure SQL — zero tokens at any scale. A full verification pass costs cents.
7. **The dashboard, traces, risk report, provenance click-through, and chaos controls are core product** — they are the functional demo URL judges use.
8. **Snapshot discipline.** The parity reference is the frozen source snapshot (freeze-window, #14). Day-2 (P2): raise `gc.ttlseconds` for the run so `AS OF SYSTEM TIME` never loses its reference.
9. **OTel GenAI conventions, defensively adopted.** Dual-emission (`OTEL_SEMCONV_STABILITY_OPT_IN`); content in span events, never attributes; checksums and verdicts logged, never data rows.
10. **Agents hit the databases directly on the hot path.** MCP is the *operator's* window — read-only via a SELECT-only SQL role — not a data plane for 300 workers. **MCP demo fallback:** the provenance click-through view (§9.3) delivers the same moment if MCP misbehaves on stage.
11. **Verifier algorithm = hierarchical checksum diff with a canonical-cast layer.** Order-independent aggregate (sum/XOR of per-row hashes); per-type canonicalization proven by a **golden test suite** — every supported type, both engines, equal checksums — a named Phase 0/1 exit criterion. Recursion depth and fan-out capped by config; row-level comparison only at leaves.
12. **Direction: sources → CockroachDB only.** Rules keyed `(source, target, feature)` so bidirectional is future work, not a rewrite.
13. **Sources: PostgreSQL P0; MySQL and others are P2 quirk packs.** "Any database" is an architecture property, not a hackathon promise.
14. **Cutover: Replicator + failback is the production path; freeze-window is the demo simplification, stated as such.** The quorum gate is the decision layer that *arms* the drain-and-cutover; post-cutover failback is what makes the production story complete. We say this out loud rather than letting a judge say it first.
15. **Quirk KB = seeded + learned rules, both with provenance and lifecycle** (`seeded | candidate | confirmed`, §4.2). Seed curated against current docs in Phase 0; a confident-but-stale mitigation is this system's most dangerous output.
16. **Bulk movement = wrap MOLT Fetch. Decided now, not Phase 0.** Load is a solved tar pit (snapshots, FK handling, resumability) and wrapping is the stronger ecosystem story. The Phase 0 spike verifies flag construction and output parsing — not the decision. Schema conversion: assume the Schema Conversion Tool isn't scriptable; the Resolver is a small in-house converter constrained to a whitelisted mapping set, with the demo schema chosen inside that set.
17. **Injected demo quirk = timestamp precision truncation.** Plausibly absent from a curated seed (unlike `BIGINT→INT`, which judges would expect seeded), crisp on the heatmap, and it sets up the thesis line: *the seed covers the head of the distribution; learning covers the tail.*
18. **Run 1 → run 2 reset is a scripted, rehearsed runbook.** Wipe and reload the target; preserve `memory_*` and run-1 `agent_trace` in a separate schema the reset cannot touch. The wipe happens on camera — it makes the surviving memory visible.
19. **Baselines that bite:** (a) the same harness at `max_workers = 1`, and (b) **`molt verify` on the same schema** — the baseline in every judge's head. Claim wall-clock, self-healing under SIGKILL, and what logs can't answer that the ledger can. Don't claim token parity — parity checks are token-free, so it reads as padding.
20. **App-side changes: out of scope to execute, in scope to flag** (§2). The Auditor emits informational findings with effort estimates; Flock never edits application code.

---

## 14. The demo — two runs, one lesson

A two-act arc: migrate a ~50-table PostgreSQL database (on RDS, with generated data, seeded quirks, and generated workload traffic) to CockroachDB, **twice**. Act one shows working memory at swarm scale (pillar 1); act two proves the experience memory compounds (pillar 2). Every beat has a degradation ladder: chaos at 30 verifiers reads the same as 300; the two-run arc on 10 tables still proves pillar 2.

**Run 1 — cold memory:**

1. **Preflight + introspect + audit.** Environment goes green; the schema and workload map fill in; the **risk register** renders from the seeded rulebook — the `SERIAL` hotspot with a hash-sharded mitigation, the trigger outbox with a changefeed alternative, the purge cron as a row-level-TTL opportunity, the contended transaction shape as an app-side `40001` advisory — each finding citing its rule. Gate 1: approve.
2. **Plan + convert + load.** The DDL diff is approved (Gate 2); the Load Driver runs the dance and MOLT Fetch moves the data; the Reconciler sets sequence restarts; the heatmap lights up with 1,000+ pending checks; Lambda verifiers spin up.
3. **Zero double-work.** The duplicate-claim counter stays pinned at **0** (`SKIP LOCKED` + epochs). Toggle to a naive read-then-update claim (quarantined pool) and watch it climb — the money shot, now falsifiable by definition (§6.3).
4. **Survive chaos.** SIGKILL half the verifiers mid-pass — leases expire, epochs bump, checks re-pool, the pass finishes with zero lost work. Ephemeral agents, durable memory.
5. **Catch the silent divergence.** The injected timestamp-precision truncation turns a range checksum **red** — the mismatch recurses live, the red cell fanning into finer child checks until the diverging rows are cornered. **No-go: cutover aborts; the untouched source keeps serving.** Diagnosis posts its postmortem; Reflection distills the candidate rule.
6. **The reset, on camera.** The target is wiped and reloaded from the runbook — and the memory schema visibly survives the wipe. Ephemeral everything, durable memory, again.

**Run 2 — warm memory (the payoff):**

7. **The same migration, replanned.** The audit now flags the truncation **before execution** — deterministically, via the rule's detector (§4.3) — citing the rule learned one act ago, with its provenance chain one click deep: finding → rule → run-1 postmortem → the failed check's trace. The plan routes around it; verification goes green; the human cuts over. The rule transitions to **confirmed**. Ask via MCP: *"what did you learn from run 1?"* — answered from the ledger; if MCP misbehaves, the click-through *is* the moment.
8. **Close on the metrics panel.** 100% coverage, 0 duplicates, **caught → learned → prevented → confirmed**, mean abort time, token cost (parity: zero) — next to the baselines: the same harness at `max_workers=1`, and `molt verify` on the same schema — same verdicts, N× the wall-clock, no self-heal, no queryable ledger. The swarm buys speed and trust, not theater.

All coordinated through the shared memory, no orchestrator in the data plane. Every beat reproducible by a judge at the demo URL — whose default view is a **time-travel replay of a golden run** (a scrubber over `agent_trace` via `AS OF SYSTEM TIME`), with live runs behind one button so judges never share mutable state. The video (<3 min) follows the same arc.

---

## 15. Honest caveats

- **Breadth, not brains.** The swarm parallelizes verification coverage; it does not make the plan smarter. Planning, audit approval, and cutover stay human-gated.
- **Canonicalization is the hardest problem here** (§6.2). The golden test suite is the mitigation, and it gets real calendar time — a checksum layer that lies is worse than no verifier.
- **Wrong advice is worse than no advice.** Seeded rules are curated against current docs in Phase 0, carry provenance, and are versioned; a confident-but-stale mitigation is the most dangerous output this system can produce.
- **The app layer is flagged, never fixed.** Retry loops, ORM behavior, `LISTEN/NOTIFY` consumers are informational findings with effort estimates. Executing app-side changes is out of scope, full stop.
- **Freeze-window is a demo simplification.** Production cutover is Replicator + failback (§13.14); we scope honestly rather than hiding the assumption.
- **"Any database" discipline.** The architecture is pluggable; the hackathon claim is Postgres. Overpromising engine coverage is how the core demo dies.
- **Contention.** Naive claiming thrashes hot rows. Mitigations: `SKIP LOCKED`, sharded `verify_tasks` if load tests demand it, short heartbeated leases.
- **Lambda networking is a real decision, not a detail.** 300 workers = a connection storm against Cloud connection limits plus VPC-to-RDS with egress to CockroachDB. Jittered spawn, connection reuse across warm invocations, ECS if the sustained pass demands it.
- **Observability ≠ correctness.** A green dashboard with a weak diff is false comfort — diff on multiple signals (counts, checksums, aggregates, sequence state) against generated-but-realistic data.
- **Trace content is sensitive.** Prompts/results live in droppable span events; the ledger stores checksums and verdicts, never data rows. Secrets via Secrets Manager; MCP behind a SELECT-only role.
- **Bedrock quotas.** Mostly defused by design — parity verifiers call no model. Audit/diagnosis bursts get quota increases filed in Phase 0 plus a token bucket + jittered backoff.

---

## 16. Judging alignment

| Criterion | How Flock scores |
|---|---|
| **Agentic Memory Design** | Memory is the coordination substrate (queue + blackboard + bus + vectors) *and* the compounding cross-migration memory *and* the audit store — one store, many roles. Proven on camera: run 2 beats run 1 because the memory improved, not the model, and the rule's lifecycle (`candidate → confirmed`) is the receipt |
| **Technical Implementation** | Serializable `SKIP LOCKED` claiming with epochs and heartbeats, changefeed coordination, in-engine hierarchical checksum diffs over a golden-tested canonical layer, transactional leaf-quorum, OTel GenAI instrumentation — correct, current, non-trivial |
| **Real-World Impact** | Getting *onto* CockroachDB is the journey Cockroach built MOLT for — Flock automates the layer they currently staff with humans and migration-services engineers, and every data team fears this workflow |
| **Production Readiness** | Three human gates, source-untouched-until-cutover, self-healing leases, chaos-tested, falsifiable SLOs, queryable + time-travelable audit — directly answering the trust gap that sinks ~95% of agent pilots |
| **Creativity & Originality** | The destination database doubles as the migration's coordination brain **and** observability backend — including time-travel over the audit trail itself; memory that compounds across migrations with a visible lifecycle |

---

## 17. Phases

Sequential; a phase starts only when the previous phase's exit criteria pass.

| Phase | Focus | Exit criteria |
|---|---|---|
| **Phase 0** | Spikes + foundations | Verified on the real Cloud tier: changefeed webhook sink, vector index, `SKIP LOCKED` micro-benchmark, MCP connect; OTel dual-emission smoke test. RDS source provisioned. **MOLT Fetch wrap spike** (flag construction + output parsing — the decision is made, §13.16). **Canonical-cast golden suite green across all supported types.** **Schema + data generator with seeded quirks; workload generator** (`pg_stat_statements` needs traffic to introspect); fault-injection library (also produces the catch-rate metric). Sequence-reconcile spike. Seeded quirk KB curated against current docs. Bedrock quota increases filed |
| **Phase 1** | Pillar 2 end-to-end (serial) | The full **two-run arc** on a small schema with one serial verifier: preflight → introspect → audit → G1 → plan + resolve → G2 → convert → load → reconcile → verify → abort on injected divergence → diagnose → distill → **reset via runbook** → re-run where the audit flags the problem *before execution via the rule's detector*, citing provenance. Scribe renders artifacts. Exit bar: run 2 provably differs because of a stored memory; instrumented from the first span |
| **Phase 2** | Pillar 1 at scale | Concurrent claiming with epochs + heartbeats (duplicates = 0 at 100+ verifiers, per the §6.3 definition) → hierarchical diff with recursive child checks + `split` lifecycle (pure SQL, zero tokens) → all four check kinds → leases/self-heal under SIGKILL → changefeed-driven leaf-quorum; `agent_trace` populated by every agent; claim-path load test; Lambda connection strategy settled |
| **Phase 3** | The two-run demo surface | Dashboard (heatmap, counters, SLO panel, register view, **provenance click-through**) + naive-claim toggle + chaos control at ~300 verifiers; the full §14 arc runs unnarrated, including the reset beat and the MCP moment with its fallback; **both baselines captured** (`max_workers=1` and `molt verify`); golden-run replay scrubber; ccloud scripts |
| **Phase 4** | Freeze & polish | All demo beats pass ≥3 consecutive full runs; README + diagram; video recorded; demo URL soak-tested. Stretch (multi-region, MySQL pack, CDC cutover, day-2) only after this passes |
| **Phase 5** | Submit | Repo public (Apache-2.0), submission filed with ≥24h buffer |

## 18. Open questions (agenda for the architecture discussion)

- **Reflection prompt wording + rule-merge policy.** The schema and lifecycle are settled (§4.2); what remains is the distillation prompt itself and the exact link-vs-merge threshold.
- **Where `molt verify` fits as a signal:** baseline only (§13.19), or also invoked as a leaf-level check kind for defense in depth?
- **Demo schema design:** which ~50-table schema + generator — needs the seeded quirks (sequences, trigger outbox, purge cron, a contended transaction shape) and believable volume.
- **Checksum function + range sizing + recursion caps** — constrained by one rule: it must survive the golden suite identically on both engines.
- Language/stack (Python vs TypeScript; Bedrock SDK ergonomics).
- Verifier substrate: Lambda (bursty, webhook-native) vs ECS (no 15-min ceiling) — one primary, harness portable.
- CockroachDB Cloud tier, given Phase 0 findings on changefeeds/vector/MCP.
- Embedding model on Bedrock (Titan vs Cohere) + index parameters.
- OTel viewer (Tempo vs Langfuse vs OpenObserve) and Collector deployment.
- Dashboard stack + read strategy that never degrades the claim path (polling is acceptable here; changefeeds stay for quorum).

---

## Appendix A — Swarm-verification SQL patterns (illustrative)

> Illustrative of required semantics; exact syntax fixed against current CockroachDB docs after Phase 0.

**Task lifecycle columns (see §6.3):**
```sql
CREATE TABLE verify_tasks (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  migration_id  UUID NOT NULL,
  object_type   STRING, object_name STRING,
  check_kind    STRING,        -- parity | sequence_state | object_presence | aggregate
  bounds        JSONB,         -- range bounds for parity checks
  parent_task   UUID,          -- non-NULL for recursion children
  status        STRING NOT NULL DEFAULT 'pending',  -- pending|claimed|passed|failed|split
  owner         STRING,
  claim_epoch   INT NOT NULL DEFAULT 0,
  lease_expires TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT now()
);
```

**Atomic claim — no two agents verify the same object:**
```sql
UPDATE verify_tasks
   SET status = 'claimed', owner = $agent_id,
       claim_epoch = claim_epoch + 1,
       lease_expires = now() + INTERVAL '60 seconds'
 WHERE id = (
       SELECT id FROM verify_tasks
        WHERE migration_id = $mig AND status = 'pending'
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1)
RETURNING id, claim_epoch, object_name, check_kind, bounds;
```

**Heartbeat — a long check renews its lease instead of manufacturing a duplicate:**
```sql
UPDATE verify_tasks
   SET lease_expires = now() + INTERVAL '60 seconds'
 WHERE id = $task AND owner = $agent_id AND claim_epoch = $epoch;
```

**Idempotent verdict — keyed by (task, epoch) so re-runs can't double-count:**
```sql
INSERT INTO verify_results (task_id, claim_epoch, migration_id, object_name, passed, detail)
VALUES ($task, $epoch, $mig, $obj, $passed, $detail)
ON CONFLICT (task_id, claim_epoch) DO NOTHING;
UPDATE verify_tasks SET status = CASE WHEN $passed THEN 'passed' ELSE 'failed' END
 WHERE id = $task AND claim_epoch = $epoch;
```

**Hierarchical diff — a diverging range becomes 'split' (no verdict) and spawns children:**
```sql
INSERT INTO verify_tasks (migration_id, object_name, check_kind, bounds, parent_task, status)
SELECT $mig, $obj, 'parity', r.bounds, $task, 'pending'
  FROM split_bounds($lo, $hi, 8) AS r(bounds);   -- fan-out + depth capped by config
UPDATE verify_tasks SET status = 'split' WHERE id = $task AND claim_epoch = $epoch;
```

**Sequence-state check — the classic post-cutover incident row checksums can't catch:**
```sql
-- worker reads target sequence restart and max(pk); passes iff restart > max
SELECT last_value FROM $sequence;             -- on target
SELECT max($pk_col) FROM $table;              -- on target
-- verdict written via the idempotent pattern above
```

**Leaf-quorum cutover gate — split parents can't wedge it; mid-flight recursion can't fake a pass:**
```sql
UPDATE migrations
   SET status = CASE
     WHEN NOT EXISTS (SELECT 1 FROM verify_tasks
                       WHERE migration_id = $mig
                         AND status IN ('pending','claimed'))          -- all leaves resolved
      AND NOT EXISTS (SELECT 1 FROM verify_tasks
                       WHERE migration_id = $mig AND status = 'failed') -- zero failures
     THEN 'ready_for_cutover'   -- Gate 3: human confirms
     ELSE 'aborted'             -- source untouched, still system of record
   END
 WHERE id = $mig;
```

**Self-heal — a dead verifier's check returns to the pool (next claim bumps the epoch):**
```sql
UPDATE verify_tasks SET status = 'pending', owner = NULL
 WHERE status = 'claimed' AND lease_expires < now();
```

**Coordination bus — quorum wakes as results land (webhook sink; at-least-once ⇒ idempotent consumers):**
```sql
CREATE CHANGEFEED FOR TABLE verify_results
  INTO 'webhook-https://<bus-endpoint>'
  WITH updated, resolved;
```

## Appendix B — Observability schema & queries (illustrative)

**Append-only audit / trace ledger (in the shared memory):**
```sql
CREATE TABLE agent_trace (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trace_id     STRING NOT NULL,          -- OTel trace id
  span_id      STRING NOT NULL,
  migration_id UUID,
  agent_id     STRING,
  kind         STRING,                    -- invoke_agent|llm.chat|db.execute|memory.retrieve|verdict|gate
  object_name  STRING,
  claim_epoch  INT,
  passed       BOOL,
  tokens_in    INT, tokens_out INT, cost_usd DECIMAL,
  detail       JSONB,                     -- checksums + verdicts, never data rows
  ts           TIMESTAMPTZ DEFAULT now()
);
```

**Every failed check with its reasoning:**
```sql
SELECT object_name, detail->>'reason' AS why, agent_id, ts
  FROM agent_trace
 WHERE migration_id = $mig AND kind = 'verdict' AND passed = false
 ORDER BY ts;
```

**Time-travel the audit — what had the swarm verified as of a past instant? (also powers the demo's replay scrubber):**
```sql
SELECT count(*) FILTER (WHERE passed) AS passed,
       count(*) FILTER (WHERE NOT passed) AS failed
  FROM agent_trace AS OF SYSTEM TIME '-10m'
 WHERE migration_id = $mig AND kind = 'verdict';
```

**Live SLO panel — coverage, failures, cost:**
```sql
SELECT count(DISTINCT object_name)                           AS objects_checked,
       count(*) FILTER (WHERE kind='verdict' AND NOT passed) AS failures,
       sum(cost_usd)                                         AS run_cost
  FROM agent_trace
 WHERE migration_id = $mig;
```

## Appendix C — Compounding memory & the gap audit (illustrative)

**Distilled rules with detector, mitigation template, provenance, and lifecycle (§4.2):**
```sql
CREATE TABLE memory_rules (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_engine    STRING NOT NULL,   -- 'postgresql' | 'mysql' | ...
  target_engine    STRING NOT NULL,   -- 'cockroachdb' (direction-agnostic by design)
  feature          STRING NOT NULL,   -- 'sequences' | 'type:timestamptz' | 'txn:contended' | ...
  detector         JSONB,             -- structured predicate / SQL over feature descriptors — matching is mechanical
  rule             TEXT NOT NULL,     -- "timestamptz(6)→lower precision silently truncates; map precision explicitly"
  mitigation       TEXT,              -- template the Resolver applies
  status           STRING NOT NULL DEFAULT 'candidate',  -- seeded | candidate | confirmed
  source_migration UUID,              -- provenance: the run that taught this (NULL = seeded)
  links            UUID[],            -- related rules/incidents (dedupe by key + similarity ⇒ link, don't merge)
  embedding        VECTOR(1536),      -- dimension per chosen embedding model
  created_at       TIMESTAMPTZ DEFAULT now()
);
```

**Gap-audit recall — exact key first (deterministic rail), similarity second:**
```sql
SELECT rule, feature, status, source_migration FROM memory_rules
 WHERE source_engine = $src AND target_engine = 'cockroachdb' AND feature = ANY($observed_features);

SELECT rule, feature, status, source_migration FROM memory_rules
 WHERE source_engine = $src AND target_engine = 'cockroachdb'
 ORDER BY embedding <-> $schema_embedding LIMIT 10;
```

**Rule confirmation — the run-2 transition that goes on the metrics panel:**
```sql
UPDATE memory_rules SET status = 'confirmed'
 WHERE id = $rule_id AND status = 'candidate';   -- fired pre-execution and prevented the run-1 failure
```

**Risk register — the generated, human-approved pre-mortem:**
```sql
CREATE TABLE gap_findings (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  migration_id UUID NOT NULL,
  feature      STRING NOT NULL,
  severity     STRING NOT NULL,      -- 'blocker' | 'risk' | 'opportunity' | 'informational'
  risk         TEXT,
  mitigation   TEXT,
  opportunity  TEXT,
  rule_id      UUID,                 -- provenance: the memory rule behind this finding
  approved     BOOL DEFAULT false,   -- Gate 1 sign-off, per finding
  created_at   TIMESTAMPTZ DEFAULT now()
);
```
