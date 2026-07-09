# Project Flock — Database Migration Copilot with Swarm Verification

> Migrate any database to CockroachDB — schema quirks audited, risks mitigated, alternatives suggested, and every row verified by a **swarm** of agents. Then keep making schema changes safely once you're there. The copilot's memory **compounds**: every migration it runs makes the next one smarter. Underneath it all, one durable, strongly-consistent shared memory: CockroachDB — the destination database is also the migration's brain.

**Hackathon:** CockroachDB × AWS — Build with Agentic Memory

**One-liner:** Point Flock at a database; it learns the schema's quirks, audits what won't map cleanly to CockroachDB and how to mitigate it, suggests what CockroachDB newly makes possible, plans the migration with memory of every migration before it, moves the data, and verifies parity with a swarm — cutover only on unanimous pass plus human sign-off.

---

## TL;DR

Migrating between databases isn't one action — it's a *gap analysis plus the same verification work repeated across a huge surface*: hundreds of tables, types, sequences, triggers, and dependent queries, each of which can silently break or silently diverge. Flock attacks both halves. A **gap audit** engine — seeded with engine-specific quirk knowledge and *sharpened by every migration it has run* — produces a risk register (what won't map, how to mitigate it) and an opportunity report (what the target newly offers that fits your use cases). Then a **swarm** of verifier agents claims per-object checks atomically (`FOR UPDATE SKIP LOCKED`), runs hierarchical checksum diffs *across the two databases* concurrently, and votes on cutover — coordinating entirely through CockroachDB, which acts simultaneously as the **task queue, the blackboard, the pub/sub bus, the vector memory, and the observability/audit backend**. Every design choice is grounded in a specific, published failure mode — and the strongest paper ideas are *implemented, not just cited* (§4): hierarchical checksum diffing, reflection-distilled memory that compounds across runs, a diagnosis agent that turns every failure into a lesson, and a cost-controlled serial baseline to prove the swarm isn't theater. The swarm is instrumented to OpenTelemetry GenAI conventions so nothing it does is a black box (§9).

---

## The focus: two pillars (read this first)

The hackathon's center of gravity is one sentence from the brief: *"memory is not an afterthought, it is the thing that makes an agent useful in production."* Flock is built as two proofs of that sentence; everything else in this document is plumbing, sized accordingly.

- **Pillar 1 — working memory at swarm scale.** The verification swarm *is* the brief's motivating image: hundreds of ephemeral Lambda workers spawning autonomously, writing claims/verdicts/heartbeats constantly, dying mid-task while the memory survives and self-heals. Lambda workers are ephemeral by construction — "stateless agents, durable CockroachDB memory" is the runtime model, not a slogan.
- **Pillar 2 — experience memory that compounds.** Migration gives memory a reason to exist beyond coordination: quirks recur across migrations, so remembering pays. The proof is behavioral and takes exactly two runs (§13): run 1 fails on an injected divergence and distills the lesson; run 2 — the *same* migration — flags the problem *before execution*, citing the rule it learned. Better because the memory improved, not the model.

**The submission in one sentence:** a migration copilot whose swarm coordinates through CockroachDB and whose second migration is provably better than its first — because the memory, not the model, improved.

Sized as plumbing in service of the pillars: schema conversion and bulk load (deterministic, boring); the gap audit (the *surface where memory becomes visible* — ten curated quirks, not an expert system); the external OTel viewer (garnish — the CockroachDB-backed ledger + dashboard are the observability story). Outside the blast radius entirely (P2): day-2 mode, MySQL, dependency analysis, CDC cutover.

---

## 1. The real problem (stated plainly)

Database migrations — both moving *between* engines and changing schemas *within* one — are the most feared workflow in data infrastructure, and the failures are *silent*: green health checks, zero error rate, but a lossy type mapping truncated values or a downstream query now returns the wrong thing. Four compounding gaps:

1. **Dialect gaps get discovered in production.** Sequences that hotspot, triggers that don't exist on the target, isolation semantics that differ — found the hard way, weeks after cutover.
2. **Verification doesn't scale.** Checking that every row and every dependent query survived across hundreds of tables is slow when done serially — so teams sample, and sampling misses.
3. **The knowledge doesn't compound.** Each migration is a months-long, consultant-heavy, one-off effort. What the last team learned about *this engine's quirks* leaves with them.
4. **Agent systems can't be trusted in production.** Surveys put ~95% of enterprise AI pilots as never reaching production — not for lack of model capability, but for lack of architectural robustness, governance, and traceability. A black-box agent that touches your data is a non-starter.

**What we're really building:** *safe, fast, and auditable database migration at scale* — where memory makes it smart, a swarm makes it fast, and observability makes it trustworthy.

---

## 2. Two modes, one loop

| Mode | What it is | Priority |
|---|---|---|
| **Replatform** (the flagship) | Any source database → CockroachDB: introspect → gap audit → plan → convert schema → load data → swarm-verify parity → cutover gate | P0 |
| **Day-2** | Once you're on CockroachDB, the *same loop* runs your ongoing schema changes: online DDL, `AS OF SYSTEM TIME` old-vs-new diffs, backup-based rollback | P2 (epilogue at best) |

The arc: **Flock gets you to CockroachDB, then keeps you safe on it** — and both modes share the same memory, swarm, and observability substrate, so every day-2 change keeps feeding the memory the replatform built.

**Direction is deliberately one-way for now:** sources → CockroachDB. The quirk engine is designed direction-agnostic (rules are keyed by source *and* target engine), so bidirectional / any-to-any is a future extension, not a rewrite — but promising "any two databases" in a hackathon dilutes the story CockroachDB judges care about and explodes the quirk matrix. PostgreSQL is the P0 source, MySQL P1, others as pluggable quirk packs later.

**Ecosystem fit:** Cockroach Labs already ships **MOLT** (Fetch for data movement, Verify for row checks) and a Schema Conversion Tool — because getting *onto* CockroachDB is the journey their users actually take. Flock is the **agentic layer around that journey**: MOLT moves bytes deterministically; Flock plans, audits gaps, verifies at swarm scale with full provenance, and *remembers*. Whether Flock drives the MOLT tools or implements its own load path is a Phase 0 spike (§17).

---

## 3. Why a swarm actually belongs here

Most "multi-agent" projects are theater — more agents don't make a single reasoning task better. Migrations are the exception, because the expensive part is **breadth, not depth**:

- Verifying data parity between source and target — per table, per row-range.
- Diffing representative query results across two engines — per query.
- Checking every downstream query that touches a changed object still returns correctly — per dependency.

That's embarrassingly parallel. A single agent does it serially; on a large schema a full verification pass takes hours. A swarm fans out and does it concurrently. **The swarm isn't bolted onto migrations — it's the natural shape of the verification workload.**

> **Honest framing:** the swarm's value is *speed and coverage on parallelizable verification*, not "more agents = smarter migrations." Planning, gap-audit approval, and cutover stay careful and human-approved. Framed as "a swarm designs your migration," it's indefensible; framed as "a swarm parallelizes the breadth of verification," it's exactly right.

---

## 4. Research → real failure → mechanism

Every idea maps to a concrete failure mode in our system — nothing is adopted for novelty. **Implemented** = the paper's mechanism is built into Flock; **validates design** = the paper justifies a choice we made.

| Research idea | The real failure it addresses | Mechanism in Flock | Status |
|---|---|---|---|
| **Datafold DMA / data-diff** | Migration silently diverges data (row loss, wrong values) | **Hierarchical checksum diff** (§6.1): checksum coarse row-ranges *across the two databases*, recurse only where they diverge — each recursion spawning finer child checks back into the queue | **Implemented** — core verifier algorithm |
| **ExpeL / A-MEM** (2024–25) | Failures are forgotten, so lessons don't compound | **Reflection step** (§7): after every run, distill the trajectory into reusable rules keyed by engine + feature, embedded and linked to related memories; the planner and gap audit retrieve incidents *and* rules | **Implemented** — core memory |
| **D-Bot / DBAIOps** (VLDB 2024–26) | Failures get rolled back but never explained; the agent repeats them | **Diagnosis agent** (§6.4): on no-go, a strong model reads the failed check's trace + recalled incidents and writes a root-cause postmortem into memory | **Implemented** — P1 |
| **Illusion of Multi-Agent Advantage / AI Agents That Matter** | "Multi-agent" theater; benchmarks that ignore cost | **Cost-controlled baseline**: the same checks run serially by one agent — same verdict, ~same tokens, N× the wall-clock — reported on the metrics panel | **Implemented** — demo evidence |
| **OTel GenAI conventions + AgentOps (2411.05285), AgentTrace (2602.10133), "Agent Traces to Trust" (2606.04990)** | Black-box agents you can't debug, audit, or trust | The observability layer in §9 — standardized traces, live dashboard, queryable provenance | **Implemented** — core |
| **CrackSQL** (SIGMOD 2025) | LLM freely rewrites SQL/DDL and introduces subtle breakage | Constrain the LLM to *planning + auditing + verification*; deterministic tools (CockroachDB's engine, conversion/load tooling) perform the mutation. (Its deeper mechanism — LLM+rule-based dialect translation of dependent queries — is a P2 stretch that fits the replatform mode naturally) | Validates design |
| **"Data Agents: Hype?"** survey (2025) | Over-trust in autonomous agents | A *safety envelope*: human approval gates, source untouched until cutover, verify-before-commit | Validates design |
| **Online schema change** (F1, VLDB 2013) | Blocking DDL causes downtime | Non-blocking online schema changes, native to CockroachDB (day-2 mode) | Validates design |
| **Project Sid / PIANO, AgentSociety** | Large agent populations corrupt shared state | One strongly-consistent shared memory; one coherent decision bottleneck (the quorum) | Validates design |

---

## 5. The gap audit (learn the schema, audit the risks, suggest what's newly possible)

The step that makes Flock a *copilot* rather than a pipeline — and, more importantly, **the surface where memory becomes visible**: the audit is what looks different between a cold-memory run and a warm-memory run (§13). It is not an expert system; ten curated, provenance-linked quirks beat fifty generic ones. Before anything executes:

1. **Introspect the source.** Schema, types, sequences, defaults, triggers, procedures, extensions, constraints, collations — plus the *workload*: representative queries from the query log or statement statistics, because dialect gaps live in queries as much as in DDL.
2. **Audit against the quirk knowledge base.** Every source feature is checked against what CockroachDB supports, producing a **risk register**: one entry per gap with severity, *why it breaks*, the **mitigation** (how to get equivalent behaviour on CockroachDB or absorb it app-side), and estimated effort.
3. **Suggest opportunities.** The inverse audit: CockroachDB features that fit the *observed* use cases — not a brochure, but "your schema does X the hard way; the target does it natively."
4. **Render one artifact.** The **Migration Risk & Opportunity Report** — human-readable, provenance-linked (every claim cites its rule or the past migration that taught it), and approved by a human before any execution.

Illustrative quirk entries (exact CockroachDB support levels verified in Phase 0 — they move fast):

| Source pattern | Risk on CockroachDB | Mitigation / opportunity |
|---|---|---|
| `SERIAL` / `AUTO_INCREMENT` sequential keys | Write hotspots on ranges | Migrate to UUID keys or hash-sharded indexes |
| `LISTEN/NOTIFY`, trigger-based outbox | Not supported / limited | **Changefeeds** — native CDC, no outbox table |
| Cron purge jobs / soft-delete sweepers | Carries over as toil | **Row-level TTL** does it declaratively |
| Stored procedures / triggers | Partial support, dialect differences | Verify support level per feature; move logic app-side or to changefeed consumers where unsupported |
| Full-text search | No native FTS | Trigram indexes for light cases; external search for heavy ones — flagged with effort estimate |
| Read-replica lag workarounds | Unnecessary | **Follower reads** / global tables — consistent by default |

5. **The learned layer (the compounding part).** Every migration's outcomes — which mitigations worked, which type mappings bit back, what the diff caught — are distilled by the reflection step into rules keyed `(source engine, feature, context)`. The seeded rulebook makes the first migration competent; the learned rules make the fiftieth one sharp. Rules carry provenance (which migration taught them) and are retrieved by both the gap audit and the planner. **This is the memory layer that gets better with every migration run through it.**

---

## 6. Where the swarm shows up

### 6.1 Parallel cross-database verification (the core)
After schema conversion and data load, the planner decomposes verification into **one check per affected object** (table, row-range, index, or dependent query). Verifier agents claim checks atomically via `FOR UPDATE SKIP LOCKED` — no two agents verify the same object — and run the parity diff **across the two databases**. The diff itself is a **hierarchical checksum comparison** (data-diff lineage): checksum coarse row-ranges on source and target, and only where they diverge, recurse — spawning finer-grained child checks back into the same queue for any verifier to claim. Cheap when nothing changed, precise where something did, and the recursion is visible live on the coverage heatmap. And because a checksum comparison is pure SQL, **parity verifiers burn zero LLM tokens** — Bedrock enters only where language does: the audit, the plan, the diagnosis. On a ~300-table schema this turns an hours-long serial pass into a concurrent one. *(Day-2 mode: the same machinery diffs old-vs-new within CockroachDB using `AS OF SYSTEM TIME`.)*

### 6.2 Parallel dependency analysis
Before executing, the swarm crawls the schema dependency graph and the workload's queries in parallel — which queries touch objects whose semantics change, what breaks on the target dialect — each agent claiming a subgraph. This is how "the migration silently breaks a downstream query" gets caught before cutover instead of after. *(P2 — valuable, but outside the demo's blast radius.)*

### 6.3 Quorum cutover gate
Verifiers vote per-object results into a `verify_results` table. The migration is eligible for cutover **only when every affected object has a passing result** — evaluated in a serializable transaction, so there's no split-brain. One flagged mismatch ⇒ **no-go: cutover is aborted, and the source — still untouched and still the system of record — keeps serving.** That's the replatform safety story: before cutover, aborting is free. *(Day-2 mode, where the change is already applied: automatic rollback from the pre-change backup.)*

### 6.4 Diagnosis on failure
Abort isn't the end. On no-go, a **diagnosis agent** (strong tier, D-Bot lineage) reads the failed check's trace and the recalled incidents, and writes a structured root-cause postmortem — what broke, why, what to check next time — into memory, where the reflection step distills it into a rule. Re-plan the same migration and the copilot cites the lesson.

---

## 7. The copilot loop

```
connect ──▶ introspect ──▶ recall ──▶ gap audit ──▶ plan ──▶ [human approve]
   ──▶ convert schema ──▶ load data ──▶ swarm verify ──▶ [cutover gate] ──▶ reflect
```

- **Introspect.** Map the source: schema objects, types, workload queries (§5).
- **Recall.** Every past migration — plan, outcome, failures, incident notes — is embedded and stored in CockroachDB's vector index, alongside the **distilled rules** those runs taught us, keyed by source engine and feature. Before auditing, the copilot retrieves both: *have we migrated from this engine before? what bit us? what rule did it teach us?*
- **Gap audit.** The risk register + opportunity report (§5), human-approved.
- **Plan.** A strong model drafts the migration plan — conversion DDL, type mappings, load strategy, per-object verification plan, cutover plan — grounded in the audit and recalled history. The LLM plans; **deterministic tools execute** (conversion tooling, the bulk loader, CockroachDB's engine). A human approves.
- **Convert + load.** Schema conversion, then bulk data movement via existing deterministic tooling driven by the agent (MOLT Fetch / `IMPORT INTO` / DMS — architecture picks one). The LLM never hand-copies rows.
- **Verify.** The swarm (§6).
- **Cutover gate.** Quorum all-pass **and** human confirmation ⇒ cutover. Any failure ⇒ abort; the source keeps serving, unharmed.
- **Reflect.** After every run — pass or fail — a reflection step (ExpeL-style) distills the trajectory into structured, reusable rules ("MySQL `TINYINT(1)` used as boolean: map explicitly or comparisons silently change"), embedded and linked to related memories A-MEM-style, keyed by engine + feature. On failure, the diagnosis agent's postmortem (§6.4) feeds it. This is what makes the memory *compound*: the copilot doesn't just remember what happened — it remembers what it learned.

**Day-2 mode** runs the same loop pointed inward: source = the live CockroachDB cluster, snapshot = pre-change backup (ccloud) + pinned timestamp, execute = online schema change, verify = old-vs-new `AS OF SYSTEM TIME` diffs, rollback = `RESTORE`.

---

## 8. One substrate, five jobs

Every serious swarm needs coordination services *and* a telemetry story. The default stack wires them as separate systems with no consistency *between* them — the dual-write races between queue, state store, bus, vector DB, and audit log are where swarms silently corrupt. Flock collapses all five into CockroachDB — **the destination database is also the migration's brain** — so claiming a check, writing its verdict, updating the quorum, and recording the audit row happen in **one atomic transaction**:

| Need | Default stack | Flock |
|---|---|---|
| Task queue (exactly-once claims) | SQS | `verify_tasks` + `FOR UPDATE SKIP LOCKED` |
| Shared state / blackboard | Postgres | `verify_results`, `migrations`, `gap_findings`, serializable txns |
| Pub/sub (wake agents on change) | Redis / Kafka | **Changefeeds** → webhook sink → Lambda |
| Vector memory (semantic recall) | Pinecone | **Distributed vector index**: incidents + distilled rules, same rows as operational data |
| Observability / audit backend | Datadog + a separate audit store | `agent_trace` ledger — SQL-queryable, **time-travelable** with `AS OF SYSTEM TIME` |

## 9. Architecture & observability

```
  source DB (Postgres on RDS)
        │ introspect + workload queries
        ▼
  recall ──▶ gap audit ──▶ plan ──▶ [human approve] ──▶ convert schema ──▶ load data
  (rules,     (risk register +                              (deterministic tooling)
   incidents)  opportunity report)                                  │
                                                                    ▼
                                                        decompose verification
                                                      (per table × checksum range)
                                                                    │
            ┌──────────────────  CockroachDB (target)  ────────────┴────────┐
            │ verify_tasks │ verify_results │ gap_findings │ rules │ vectors │
            │                agent_trace (audit ledger)                     │
            └───────┬───────────────────────────────┬───────────────────────┘
        claim (SKIP LOCKED)               changefeed (webhook sink)
                │                                   │
                ▼                         ┌─────────┴──────────┐
      ┌─────────────────────┐             ▼                    ▼
      │  verifier agents     │     ┌──────────────────┐  ┌───────────────┐
      │  checksum source ↔   │────▶│ aggregator/quorum │  │ live dashboard │
      │  target, per range   │     │ all pass ⇒ cutover│  │  (real-time)   │
      └─────────────────────┘     │ gate; fail ⇒ abort │  └───────────────┘
                ▲                  └─────────┬─────────┘
                └────────── self-heal ◀──────┘
               (expired leases return checks to the pool)
```

Stateless verifier agents (Lambda for bursty fan-out, or ECS for sustained runs); all state lives in the shared memory; **no central orchestrator** — coordination is emergent from the blackboard + changefeeds.

### 9.1 Why observability is non-negotiable

There's a well-worn production war story: an agent quietly served stale data, every health check was green, error rate was zero, and it took six hours of log-grepping to find — because tool executions weren't traced as first-class spans. **A silent migration break is exactly this failure**, and a *swarm* multiplies the opacity: without tracing you can't tell which agent verified what, whether work was duplicated, or why a verdict was reached. Observability is how we turn "trust me" into "look." It's also the single biggest thing standing between an agent demo and a production-credible system — which is precisely where the judging weight sits.

### 9.2 What we instrument (OpenTelemetry GenAI conventions)

We adopt the **OTel GenAI semantic conventions** so telemetry is vendor-neutral and comparable. We capture the agent's *decision graph*, not just its I/O boundary — every step is a connected span:

```
invoke_agent  (gen_ai.agent.name = "verifier")
├─ memory.retrieve      -- recall similar past incidents + rules (vector index)
├─ llm.chat             -- gen_ai.request.model, gen_ai.usage.input/output_tokens
├─ db.execute           -- the cross-DB checksum diff (direct SQL, hot path)
└─ event: verification.verdict { object, passed, detail }
```

- **Span kinds:** agent spans, LLM client spans, DB/tool spans, memory-retrieval spans, and per-object verification spans. The whole migration is one trace: `introspect → audit → plan → approve → convert → load → fan-out → per-object verdicts → quorum → cutover / abort → reflect`.
- **Content in span *events*, not attributes** — avoids PII exposure and attribute size limits; events can be dropped at the Collector.
- **Overhead is negligible** (<1%; async batch export) — LLM calls already dominate latency.

### 9.3 CockroachDB *is* the observability backend (the elegant part)

We don't bolt on a separate telemetry store. The same shared memory that coordinates the swarm also holds its trace and audit data:

- **Durable audit ledger** — every agent action, every gap-audit decision (*why did we map this type this way?*), and every verdict is an append-only `agent_trace` / `gap_findings` row: *queryable with SQL*, *time-travelable with `AS OF SYSTEM TIME`* ("what did the swarm believe at any past instant?").
- **Live demo dashboard via changefeed** — a changefeed on the coordination tables streams events (webhook sink → Lambda) to the dashboard in real time. No polling, no separate bus.
- **Standardized deep traces** — OTel spans can additionally export to a standard viewer (Grafana Tempo / Langfuse / OpenObserve) for drill-down into any agent's reasoning chain. P2 garnish: the CockroachDB-backed ledger + dashboard are the core observability story.
- **MCP as the operator's window** — the managed MCP server (read-only, audit-logged) lets an operator in Claude Code interrogate live swarm state, the risk register, and the audit ledger in natural language.

### 9.4 Three views for the demo

1. **Live swarm dashboard** (changefeed-fed): agents online, checks claimed / passed / failed, a **duplicate-claim counter pinned at 0**, a coverage heatmap over all affected objects filling in real time, throughput (checks/sec).
2. **Per-migration trace** (OTel): the full decision graph as a connected trace; click any span to see the recall, the model call + token cost, the diff query, and the verdict.
3. **Audit / provenance ledger** (SQL + time-travel): who did what, when, and why — including why each gap was mitigated the way it was — reconstructable at any past instant.

### 9.5 Metrics (SLOs for a migration)

Verification coverage %, checks/sec, **duplicate-claim rate (must be 0)**, breaking-change catch rate, mean abort/rollback time, and token cost per model tier — the same reliability standards you hold for APIs and databases, applied to every inference.

---

## 10. CockroachDB usage — relational *and* distributed

`(*)` = one of the hackathon's four named tools (minimum required: two; Flock uses three).

**The copilot's relational features:**

| Feature | Role |
|---|---|
| CockroachDB as migration *target* | The product story: Flock is the agentic on-ramp to CockroachDB (MOLT / Schema Conversion Tool ecosystem) |
| Online schema changes | Day-2 mode: execute schema changes without blocking the app |
| `AS OF SYSTEM TIME` | Day-2 verification diffs; time-travel over the audit ledger |
| Backups via **ccloud CLI** `(*)` | Day-2 pre-change snapshot + rollback path; cluster provisioning/ops scripts |
| **Distributed vector index** `(*)` | Memory of every migration, failure, and distilled rule; recall before auditing and planning |
| **Managed MCP Server** `(*)` | Read-only, audit-logged operator window into live swarm state, risk register, and provenance |

**The swarm's distributed-concurrency features:**

| Feature | Role |
|---|---|
| Serializable txns + `FOR UPDATE SKIP LOCKED` | Exactly-once claiming of checks across a concurrent swarm |
| Changefeeds (CDC) | Wake quorum agents as results land; stream the live dashboard — no separate bus |
| Row-level TTL + leases | A dead verifier's check returns to the pool; the pass self-heals |
| Survivability | Kill half the swarm mid-pass; verdict comes out identical |

## 11. AWS usage

| Service | Role |
|---|---|
| **Amazon Bedrock** | The reasoning where language lives: gap audit, planning, diagnosis, reflection; embeddings for migration memory. Parity verifiers are pure SQL and call no model |
| **Amazon RDS (PostgreSQL)** | The demo's *source* database — the thing being migrated |
| **AWS Lambda** | Changefeed consumers (quorum + dashboard push) and bursty verifier fan-out |
| **Amazon S3** | Migration artifacts, risk reports, plans |

No SQS, Redis, external vector DB, or separate telemetry store — CockroachDB provides the queue, the bus, the vector memory, and the audit backend.

---

## 12. Design decisions (settled now, so architecture doesn't churn)

1. **Coordination bus = changefeed webhook (HTTPS) sink fronting Lambda.** There is **no native Lambda changefeed sink**. Delivery is at-least-once ⇒ **every consumer must be idempotent** — including the dashboard push. If our Cloud tier gates changefeeds (Phase 0 spike verifies), the fallback is indexed polling behind the same consumer interface — a config change, not a redesign.
2. **Exactly-once *claiming*, at-least-once *execution*, idempotent effects.** A dead verifier's check re-runs; verdict writes are keyed so re-runs can't double-count in the quorum.
3. **Scale target:** demo source schema ~300 tables, ~1,000+ per-object checks (tables × ranges), ~300 concurrent verifiers (all config-driven). Concrete numbers, not "thousands."
4. **Single-region cluster is the core deployment.** Chaos story = SIGKILL half the verifiers; multi-region/region-loss is stretch, attempted only after the core demo is frozen.
5. **Human gates:** gap-audit/plan approval before execution, cutover confirmation after a passing verdict. Failure needs no human — abort (replatform) or auto-rollback (day-2) is automatic.
6. **No LLM where none is needed.** Checksum parity verifiers are pure SQL — zero tokens at any scale. Bedrock is reserved for where language actually lives: the gap audit, planning, diagnosis, and reflection (strong tier; the volume is low). A full verification pass costs cents, not dollars.
7. **The dashboard, traces, risk report, and chaos controls are core product**, not demo garnish — they are the functional demo URL judges use.
8. **Snapshot discipline.** Replatform: the parity reference is the frozen source snapshot (freeze-window cutover, see #14). Day-2: `AS OF SYSTEM TIME` diffs only reach back within the GC TTL — raise `gc.ttlseconds` on affected tables for the run so a long pass never loses its reference point.
9. **OTel GenAI conventions, defensively adopted.** The conventions are still stabilizing — use dual-emission (`OTEL_SEMCONV_STABILITY_OPT_IN`) so version bumps don't break us. Prompt/result content goes in span *events* (droppable at the Collector), never attributes; we log checksums and verdicts, never full data rows.
10. **Agents hit the databases directly on the hot path** (claims, diffs, verdicts — direct SQL). The MCP server is the *operator's* interface: read-only, audit-logged, natural-language. MCP is not a data plane for 300 concurrent workers.
11. **Verifier algorithm = hierarchical checksum diff.** Coarse range checksums first; only diverging ranges recurse into finer child checks, spawned as new tasks in the queue. Row-level comparison only at the leaves; recursion depth and fan-out are capped by config.
12. **Direction: sources → CockroachDB only** for the hackathon. Quirk rules are keyed `(source engine, target engine, feature)` so bidirectional is future work, not a rewrite.
13. **Sources: PostgreSQL is P0; everything else — MySQL first — is P2 stretch** via pluggable quirk packs. "Any database" is an architecture property, not a hackathon promise.
14. **Demo cutover = freeze-window.** The source is quiesced during final verification and cutover, so the parity reference is unambiguous. Live CDC-based zero-downtime cutover is stretch — honest scope, not a hidden assumption.
15. **Quirk KB = seeded rulebook + learned rules, both with provenance.** Seeded entries are curated against current CockroachDB docs in Phase 0 (feature support moves fast); learned rules cite the migration that taught them. The risk report is a human-approved artifact before anything executes.
16. **Bulk movement is deterministic tooling driven by the agent** — MOLT Fetch / `IMPORT INTO` / DMS (architecture picks one in Phase 0). The LLM plans and audits; it never hand-copies data.

---

## 13. The demo — two runs, one lesson

A two-act arc: migrate a ~300-table PostgreSQL database (on RDS) to CockroachDB, **twice**. Act one shows working memory at swarm scale (pillar 1); act two proves the experience memory compounds (pillar 2).

**Run 1 — cold memory:**

1. **Introspect + audit.** The schema map fills in; the **risk register** renders from the seeded rulebook — the `SERIAL` hotspot flagged with a hash-sharded-index mitigation, the trigger-based outbox flagged with a changefeed alternative, the purge cron flagged as a row-level-TTL opportunity — each entry citing its rule. The human approves.
2. **Convert + load.** Schema lands on CockroachDB; data loads via deterministic tooling; the coverage heatmap lights up with hundreds of pending checks; Lambda verifiers spin up.
3. **Zero double-work.** The duplicate-claim counter stays pinned at **0** as agents claim checks (`SKIP LOCKED`). Toggle to a naive read-then-update claim (quarantined pool) and watch it climb — the money shot.
4. **Survive chaos.** SIGKILL half the verifiers mid-pass — the dashboard shows leases expiring, checks returning to the pool, the pass continuing with zero lost work. Ephemeral agents, durable memory.
5. **Catch the silent divergence.** One injected lossy type mapping turns a range checksum **red** — the mismatch **recurses live**, the red cell fanning out into finer child checks until the diverging rows are cornered. No-go: **cutover aborts, and the untouched source keeps serving.** The diagnosis agent posts its root-cause postmortem; the reflection step distills the rule into the vector index.

**Run 2 — warm memory (the payoff):**

6. **The same migration, replanned.** The gap audit now flags the lossy mapping **before execution**, citing the rule learned one act ago; the plan routes around it; verification goes green end-to-end; the human cuts over. Ask Claude Code, via the managed MCP server: *"what did you learn from run 1?"* — it answers from the ledger and the rules table. **The agent got better because the memory improved, not the model.**
7. **Close on the metrics panel.** 100% coverage, 0 duplicates, caught → learned → prevented, mean abort time, token cost (parity checks: zero tokens) — next to the **serial baseline**: one agent, same checks, same verdict, N× the wall-clock. The swarm buys speed, not theater.

All coordinated through the shared memory, no orchestrator in the loop. Every beat reproducible by a judge at the demo URL, unnarrated; the video (<3 min) follows the same arc.

---

## 14. Honest caveats

- **Breadth, not brains.** The swarm parallelizes verification coverage; it does not make the plan smarter. Planning, audit approval, and cutover stay human-gated.
- **Wrong advice is worse than no advice.** The quirk KB's seeded rules must be curated against current docs, carry provenance, and be verified in Phase 0 — a confident but stale mitigation recommendation is the most dangerous output this system can produce.
- **"Any database" discipline.** The architecture is pluggable; the hackathon claim is Postgres (and MySQL if P1 lands). Overpromising engine coverage is how the core demo dies.
- **Contention.** Naive claiming thrashes hot rows. Mitigations: `SKIP LOCKED`, sharded `verify_tasks` if load tests demand it, changefeed fan-out instead of polling, short TTL leases.
- **Cost.** One agent per check adds up. Mitigations: verify only *affected* objects (from dependency analysis), tier models, cache/dedupe repeated checks.
- **Observability ≠ correctness.** A green dashboard with a weak diff is false comfort — diff on multiple signals (counts, checksums, sampled rows) against real data, not synthetic.
- **Trace content is sensitive.** Prompts/results live in span events and are filterable at the Collector; the ledger stores checksums and verdicts, never full data rows.
- **Bedrock quotas.** Mostly defused by design — parity verifiers call no model, so 300 concurrent workers ≠ 300 concurrent LLM calls. The audit/diagnosis bursts still get quota increases filed in Phase 0 and a token bucket + jittered backoff in the harness.

---

## 15. Judging alignment

| Criterion | How Flock scores |
|---|---|
| **Agentic Memory Design** | Memory is the coordination substrate (queue + blackboard + bus + vectors) *and* the copilot's cross-migration memory *and* the audit/observability store — one store, many roles. And the memory **compounds**: every run is distilled into engine-keyed, reusable rules the next audit and plan retrieve — proven on camera by the two-run arc: run 2 beats run 1 because the memory improved, not the model |
| **Technical Implementation** | Serializable `SKIP LOCKED` claiming, changefeed coordination, cross-DB hierarchical checksum diffs, transactional quorum, OTel GenAI instrumentation — correct, current, non-trivial |
| **Real-World Impact** | Migrating *onto* CockroachDB is the exact journey Cockroach Labs built MOLT for — Flock is the agentic layer that makes it audited, verified, and cumulative; and every data team fears this workflow |
| **Production Readiness** | The big one: human approval gates, source-untouched-until-cutover, self-healing leases, chaos-tested, standardized traces + queryable audit + SLOs — directly answering the trust/governance gap that sinks ~95% of agent pilots |
| **Creativity & Originality** | The destination database doubles as the migration's coordination brain **and** observability backend — including time-travel over the audit trail itself; memory that compounds across migrations |

---

## 16. Phases

Sequential; a phase starts only when the previous phase's exit criteria pass.

| Phase | Focus | Exit criteria |
|---|---|---|
| **Phase 0** | Spikes + decisions | Verified on the real Cloud tier: changefeed webhook sink, vector index, `SKIP LOCKED` micro-benchmark, MCP connect; OTel GenAI dual-emission smoke test. RDS Postgres source provisioned; **MOLT Fetch/Verify + Schema Conversion Tool evaluated** (drive vs replace — decide); cross-DB checksum diff recipe proven on one table; **seeded quirk KB curated against current CockroachDB feature-support docs**. Bedrock quota increases filed. Architecture doc drafted from §17 |
| **Phase 1** | Pillar 2 end-to-end (serial) | The full **two-run arc** on a small schema with one serial verifier: introspect → gap audit (seeded KB) → plan → approve → convert → load → verify → abort on injected divergence → diagnose → distill rule → **re-run where the audit flags the problem before execution, citing the rule**. Exit bar: run 2 provably differs from run 1 because of a stored memory; instrumented from the first span |
| **Phase 2** | Pillar 1 at scale | Swarm working memory: concurrent claiming (duplicates = 0 at 100+ verifiers) → **cross-DB hierarchical checksum diff with recursive child checks (pure SQL, zero tokens)** → leases/self-heal under SIGKILL → changefeed-driven quorum; `agent_trace` ledger populated by every agent; load test the claim path |
| **Phase 3** | The two-run demo surface | Dashboard (heatmap, counters, SLO panel, risk-register view) + naive toggle + chaos control at full scale (~300 verifiers); the full §13 arc runs unnarrated end-to-end, including the MCP *"what did you learn from run 1?"* moment; **serial-vs-swarm baseline captured**; ccloud scripts |
| **Phase 4** | Freeze & polish | All demo beats pass ≥3 consecutive full runs; README + diagram complete; video recorded; demo URL soak-tested. Stretch work (multi-region, MySQL pack, CDC cutover, day-2 mode) only after this passes |
| **Phase 5** | Submit | Repo public (Apache-2.0 already in place), submission filed with ≥24 h buffer before the deadline |

## 17. Open questions (agenda for the architecture discussion)

- **⚠ Resolve first: reflection prompt + rule schema** — how a trajectory becomes a rule, the engine/feature keying, and how rules are linked/merged when they overlap. Cheap to build, easy to build badly; everything in §5.5 depends on it.
- **MOLT relationship:** drive MOLT Fetch / Schema Conversion Tool / `molt verify` as agent tools, or implement native conversion + load? (Wrapping them is the stronger ecosystem story *if* they're scriptable enough — Phase 0 spike decides.) Where does `molt verify` fit relative to our swarm diff — one more signal, or replaced?
- **Load path:** MOLT Fetch vs `IMPORT INTO` vs AWS DMS.
- **Source introspection depth:** catalogs only, or catalogs + query log / statement statistics for workload-aware auditing?
- **Demo source schema:** which realistic ~300-table Postgres schema — needs juicy quirks (sequences, triggers, an outbox, a purge cron) and believable data volume.
- Language/stack for the copilot + verifiers (Python vs TypeScript; Bedrock SDK ergonomics).
- Primary verifier substrate: Lambda (bursty, natural webhook consumer) vs ECS (no 15-min ceiling) — one primary, harness stays portable.
- CockroachDB Cloud tier, given Phase 0 findings on changefeeds/vector/MCP availability.
- Range sizing, checksum function (must be computable identically on both engines), and recursion caps for the hierarchical diff; pass thresholds per check kind.
- Whether to attempt CrackSQL-style dialect translation / auto-repair of dependent queries (P2 stretch, only after Phase 4).
- Embedding model on Bedrock (Titan vs Cohere) + index parameters for migration memory.
- OTel viewer (Grafana Tempo vs Langfuse vs OpenObserve) and Collector deployment (Lambda extension vs sidecar).
- Dashboard stack, transport (SSE off the bus vs polling), and a read strategy that never degrades the claim path.

---

## Appendix A — Swarm-verification SQL patterns (illustrative)

> Illustrative of the required semantics; exact syntax (vector operators, changefeed options) is fixed against current CockroachDB docs after the Phase 0 spikes.

**Decompose a migration into one verification check per affected object:**
```sql
INSERT INTO verify_tasks (migration_id, object_type, object_name, check_kind, status)
SELECT $mig, o.object_type, o.object_name, 'parity', 'pending'
  FROM migration_touches t
  JOIN schema_objects o ON o.id = t.object_id
 WHERE t.migration_id = $mig;
```

**Atomic claim of a check — no two agents verify the same object:**
```sql
UPDATE verify_tasks
   SET status = 'claimed', owner = $agent_id,
       lease_expires = now() + INTERVAL '60 seconds'
 WHERE id = (
       SELECT id FROM verify_tasks
        WHERE migration_id = $mig AND status = 'pending'
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1)
RETURNING id, object_type, object_name, check_kind;
```

**Record a per-object verdict (worker checksums the range on both databases, then writes the result):**
```sql
INSERT INTO verify_results (task_id, migration_id, object_name, passed, detail)
VALUES ($task, $mig, $obj, $passed, $detail);
```

**Hierarchical diff — a diverging range spawns finer child checks back into the queue:**
```sql
-- the range's source/target checksums diverged ⇒ split and re-queue
INSERT INTO verify_tasks (migration_id, object_name, check_kind, bounds, parent_task, status)
SELECT $mig, $obj, 'checksum_range', r.bounds, $task_id, 'pending'
  FROM split_bounds($lo, $hi, 8) AS r(bounds);   -- illustrative splitter; depth capped by config
```

**Coordination bus — quorum agents and the dashboard wake as results land (webhook sink; at-least-once ⇒ idempotent consumers):**
```sql
CREATE CHANGEFEED FOR TABLE verify_results, agent_trace
  INTO 'webhook-https://<bus-endpoint>'
  WITH updated, resolved;
```

**Quorum cutover gate — eligible only if every affected object passed:**
```sql
UPDATE migrations
   SET status = CASE
     WHEN NOT EXISTS (
            SELECT 1 FROM verify_results WHERE migration_id = $mig AND passed = false)
      AND (SELECT count(*) FROM verify_results WHERE migration_id = $mig)
        = (SELECT count(*) FROM verify_tasks   WHERE migration_id = $mig)
     THEN 'ready_for_cutover'   -- human confirms cutover
     ELSE 'aborted'             -- source untouched, still system of record
   END
 WHERE id = $mig;
-- day-2 mode: 'aborted' becomes 'rolled_back' — RESTORE from the pre-change backup
```

**Self-heal — return a dead verifier's check to the pool:**
```sql
UPDATE verify_tasks SET status = 'pending', owner = NULL
 WHERE status = 'claimed' AND lease_expires < now();
```

## Appendix B — Observability schema & queries (illustrative)

**Append-only audit / trace ledger (in the shared memory):**
```sql
CREATE TABLE agent_trace (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trace_id     STRING NOT NULL,          -- OTel trace id (ties to the viewer)
  span_id      STRING NOT NULL,
  migration_id UUID,
  agent_id     STRING,
  kind         STRING,                    -- invoke_agent|llm.chat|db.execute|memory.retrieve|verdict
  object_name  STRING,                    -- for per-object verification spans
  passed       BOOL,                      -- for verdict spans
  tokens_in    INT, tokens_out INT, cost_usd DECIMAL,
  detail       JSONB,
  ts           TIMESTAMPTZ DEFAULT now()
);
```

**Debug a migration from its trace — every failed check with its reasoning:**
```sql
SELECT object_name, detail->>'reason' AS why, agent_id, ts
  FROM agent_trace
 WHERE migration_id = $mig AND kind = 'verdict' AND passed = false
 ORDER BY ts;
```

**Time-travel the audit — what had the swarm verified as of a past instant?**
```sql
SELECT count(*) FILTER (WHERE passed) AS passed,
       count(*) FILTER (WHERE NOT passed) AS failed
  FROM agent_trace AS OF SYSTEM TIME '-10m'
 WHERE migration_id = $mig AND kind = 'verdict';
```

**Live SLO panel — coverage, failures, cost:**
```sql
SELECT
  count(DISTINCT object_name)                              AS objects_checked,
  count(*) FILTER (WHERE kind='verdict' AND NOT passed)    AS failures,
  sum(cost_usd)                                            AS run_cost
  FROM agent_trace
 WHERE migration_id = $mig;
```

## Appendix C — Compounding memory & the gap audit (illustrative)

**Distilled rules, linked A-MEM style, keyed by engine + feature — what the reflection step writes:**
```sql
CREATE TABLE memory_rules (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_engine    STRING NOT NULL,  -- 'postgresql' | 'mysql' | ...
  target_engine    STRING NOT NULL,  -- 'cockroachdb' (direction-agnostic by design)
  feature          STRING,           -- 'sequences' | 'triggers' | 'type:tinyint' | ...
  rule             TEXT NOT NULL,    -- "MySQL TINYINT(1) used as boolean: map explicitly or comparisons silently change"
  source_migration UUID,             -- provenance: the run that taught us this (NULL = seeded rulebook)
  links            UUID[],           -- related rules/incidents
  embedding        VECTOR(1536),     -- dimension per chosen embedding model
  created_at       TIMESTAMPTZ DEFAULT now()
);
```

**Gap-audit recall — the rules that apply to *this* source engine, ranked by relevance:**
```sql
SELECT rule, feature, source_migration
  FROM memory_rules
 WHERE source_engine = $src AND target_engine = 'cockroachdb'
 ORDER BY embedding <-> $schema_embedding
 LIMIT 10;
```

**Risk register — the human-approved audit artifact:**
```sql
CREATE TABLE gap_findings (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  migration_id UUID NOT NULL,
  feature      STRING NOT NULL,      -- what was found in the source
  severity     STRING NOT NULL,      -- 'blocker' | 'risk' | 'opportunity'
  risk         TEXT,                 -- why it breaks / what it costs
  mitigation   TEXT,                 -- how to get equivalent behaviour on the target
  opportunity  TEXT,                 -- what the target newly offers for this use case
  rule_id      UUID,                 -- provenance: the memory rule behind this finding
  approved     BOOL DEFAULT false,   -- human sign-off, per finding
  created_at   TIMESTAMPTZ DEFAULT now()
);
```
