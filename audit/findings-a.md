# AUDIT A — the data layer

**Verdict:** SUSPECT

The transactional core is real and better than the pitch claims. The *demo path*
is not: it currently runs entirely on an in-memory fake, two of the four pitched
memory tables have no code touching them at all, and the vector index — required
CockroachDB tool #2 — does not execute on the default path.

Scope note: no cluster was reachable during this audit (Docker daemon down,
`COLONY_DSN` unreadable per `.claude/settings.json:8`). Everything below is
static analysis with line citations. Items requiring a live run are listed under
**Could not verify** and are not asserted either way.

---

## A.1 — Table read/write matrix

| table | written_by | read_by | reads that affect behavior |
|---|---|---|---|
| `observations` | `report_observation` `client.py:116-131`, merge `client.py:106-113` | `get_beliefs` `client.py:198-215`; `find_similar` `client.py:156-191` | **Yes** — `beliefs.load` `beliefs.py:72` → `BeliefMap.cost` `beliefs.py:50-58` → A* cost `worker.py:530`; victim targets `worker.py:455` |
| `tasks` | `create_task` `client.py:299-313`, `claim_task` `client.py:332-343`, `complete_task` `client.py:391-406`, `renew_leases` `client.py:351-357`, `release_task` `client.py:365-370` | `open_tasks` `client.py:416-425` | **Yes** — `worker.py:264`, `worker.py:448`, `scout.py:444` |
| `events` | `log_event` `client.py:531-534` | `events` `client.py:536-541` | **No** — metrics/display only (`mission.py:150`, `server.py:233`) |
| `plans` | `log_plan` `client.py:498-510` | `plans_for` `client.py:512-520` | **No** — console + UI only (`server.py:252`) |
| `robots` | `register_robot` `client.py:461-465`, `heartbeat` `client.py:443-455` | `stale_robots` `client.py:475-479` | **No** — by design; `schema/v1_1.sql:33-34` and `client.py:467-473` both state recovery never reads it |
| `victims` | `register_victim` `client.py:260-264` | idempotency check inside its own writer `client.py:242`; one console question `console/questions.py:130` | **No** — see A.2 |
| `hazards` | **nothing** | **nothing** | **empty** — see A.2 |
| `mission_memories` | **nothing** | **nothing** | **empty** — see A.2 |

### Findings

| # | Severity | File:line | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| A-1 | FATAL | `schema/v1_1.sql:74-81` | `hazards` table has no writer and no reader anywhere in the repo | Grep for `from hazards\|into hazards` returns only the DDL. Hazard beliefs actually live in `observations` with `kind='hazard'` (`beliefs.py:72`). The four-memory pitch names `hazards` as WORKING memory; it is an empty table | Either write hazards through it, or delete it from the schema and the pitch |
| A-2 | FATAL | `schema/v1_1.sql:148-154` | `mission_memories` has no writer and no reader | Only references are the DDL, the table-name list in `tests/test_schema.py:22`, and a grant in `infra/credentials.py:37`. **SEMANTIC memory — one of the four pitched memory systems — is entirely unimplemented.** Judging criterion #1 is Agentic Memory Design | Implement the post-mission summarizer, or cut SEMANTIC from the four-memory claim |
| A-3 | MAJOR | `client.py:242`, `client.py:261` | `victims` is written but the only reads are its own idempotency guard and one console question | Agents get victim positions from `observations` (`worker.py:455`), never from `victims`. The row is written and then effectively never consulted by the fleet | Wire victim state into a decision, or document it as console-only |

### Checked and clean
- `observations` and `tasks` both have genuine closed loops: written, read, and the read demonstrably changes routing and task selection. Cited above.
- `robots.heartbeat_at` being read only for UI is **correct, not a defect** — `client.py:467-473` and `schema/v1_1.sql:33-34` both say so explicitly, and `orchestrator/lost.py` never releases tasks. This is the design working as documented.

---

## A.2 — In-memory structures shadowing the store

| # | Severity | File:line | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| A-4 | MAJOR | `sim/world.py:126-128` | `World.objects` / `World.victims` are the real source of truth for terrain and victim state; the DB holds only beliefs | Legitimate *as a simulator* — something must adjudicate ground truth. Becomes a problem only where agents read it directly (A-5) | Keep, but enforce the boundary |
| A-5 | MAJOR | `worker.py:571-580`, `worker.py:584-593`, `worker.py:566` | Agents read ground truth directly: `_passable` → `world.passable`, `_work_is_done` → `world.objects[...]` and `world.victim_at`, `_landing` → `world.occupied` | **None of these are gated by observation radius.** A robot routes through terrain it has never observed, in *both* coordination modes. `_passable`'s docstring (`worker.py:572-579`) defends this as avoiding a duplicated rule — a real engineering concern, but the effect is that pathing is omniscient | This is AUDIT B's headline; deferring the verdict to T-02 |
| A-6 | MINOR | `worker.py:543-549` | `_belief_cache` holds beliefs for `BELIEF_REFRESH_TICKS` (4) | Legitimate per-tick working buffer, matches §4.3's ~1s cadence, and it is a cache *of* store reads, not a substitute | None |

---

## A.3 — Silent fallbacks

| # | Severity | File:line | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| A-7 | FATAL | `sim/server.py:68-78` | `_make_memory` catches **every** exception and returns `FakeFleetMem` | This is the exact pattern the audit hunts. It is why the UI reads "running on fake memory". The demo path silently becomes a dictionary if anything at all goes wrong with the cluster — wrong DSN, expired cert, network blip. A judge would see a working demo that touches no database | T-10. Note this contradicts a *deliberate* PRD decision (§5.4, `README.md:35`) — see Conflict below |
| A-8 | MAJOR | `agents/planning.py:207` | `except Exception:` around the planner → falls back to rule-based | Bedrock failures degrade silently. `plans.chosen.source` records which path ran (`README.md:120`), so it is *auditable* — but nothing surfaces it at runtime | Surface the degradation in the event log and UI |
| A-9 | MINOR | `bedrock/adapter.py:307` | `has_credentials()` swallows all exceptions, returns `False` | Defensible — the docstring (`adapter.py:298-302`) argues an env-var check would misreport a Fargate task role. Narrow the catch | Catch `botocore.exceptions` specifically |

### Conflict — flagging rather than resolving, per §4 of the plan

T-10 ("remove every silent fallback, fail loud") **directly contradicts** a
documented design decision: `README.md:35` and PRD §5.4 state that a mission runs
with neither CockroachDB nor AWS on purpose, so lanes 2/3 are never blocked. The
fake is also load-bearing for the test suite — 467 of 629 tests currently run
against it.

Both positions are defensible and they cannot both hold. **Not picking.** This
needs Praneeth. My read: keep the fake for `pytest` and for an explicit
`COLONY_MEMORY=fake`, and make the *implicit* fallback in `_make_memory` fatal —
that removes the demo-day lie without breaking development.

---

## A.4 — Seeded / fixture rows

**Checked and clean.** No result seeding found.

- `seed_sector_tasks` (`mission.py:68`) creates exploration tasks at bootstrap — scenario setup, legitimate.
- `world/build_aftershock.py` generates the map, committed as `world/maps/aftershock.json` and verified against its generator in CI (`.github/workflows/ci.yml:82-87`) — scenario setup, legitimate.
- Grep for pre-written observations, pre-filled metrics or canned event logs returns nothing. Every `observations`, `events` and `plans` row originates from a running mission.

---

## A.5 — Transactions and isolation

**Checked and clean. This is the strongest part of the codebase.**

- Lease acquisition is a **single atomic `UPDATE ... WHERE ... RETURNING`** (`client.py:332-343`). The claimability predicate — `status='open' OR (claimed/in_progress AND lease expired)` — is inside the `WHERE`, so there is no SELECT-then-UPDATE window anywhere.
- Grep confirms **zero** SELECT-then-UPDATE patterns on `tasks`.
- Isolation is documented at `client.py:36-41`: CockroachDB defaults to SERIALIZABLE, and `SerializationFailure` (SQLSTATE 40001) is treated as a replay signal, not an error, via `retry_on_serialization_failure` (`client.py:44-57`).
- Multi-statement work is wrapped explicitly: `report_observation` (`client.py:103`), `register_victim` (`client.py:240`), `complete_task` (`client.py:388`).
- `complete_task` marks done **and** unblocks dependents in the same transaction (`client.py:388-407`), and returns `None` vs `[]` to distinguish "did not apply" from "nothing waiting" (`client.py:373-387`) — precisely the distinction that would otherwise inflate every event-log metric.

The actual lease SQL, as requested:

```sql
UPDATE tasks
   SET status = 'claimed', claimed_by = %s, claimed_at = now(),
       lease_expires_at = now() + %s::interval
 WHERE id = %s
   AND (status = 'open'
        OR (status IN ('claimed', 'in_progress')
            AND (lease_expires_at IS NULL OR lease_expires_at < now())))
 RETURNING id
```

---

## A.6 — CockroachDB-specific features actually exercised

| feature | cited | runs in default demo path? |
|---|---|---|
| `VECTOR(512)` column | `schema/v1_1.sql:109` | Column exists; **never populated** without Bedrock |
| `VECTOR INDEX ... vector_cosine_ops` | `schema/v1_1.sql:113` | **No** — see A-10 |
| Cosine `<=>` search | `client.py:174-179` | **No** — see A-10 |
| SERIALIZABLE + 40001 replay | `client.py:36-57` | **Yes** |
| `UPSERT` | `client.py:462` | Yes (minor; CRDB syntax) |
| Core changefeed | `fleetmem/changefeed.py` | **No** — deliberately unadopted, `TODO.md:97-101` |
| `AS OF SYSTEM TIME` | — | **Absent.** Grep returns nothing |
| Follower reads | — | **Absent** |
| Multi-region / locality | — | **Absent** |

| # | Severity | File:line | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| A-10 | FATAL | `client.py:155-163` | With no embedding, `find_similar` takes the position-only branch and never issues the `<=>` query | Embeddings require Bedrock (`adapter.py:150`). AWS is currently unconfigured, so **the entire vector path is dead on the demo machine.** Distributed Vector Indexing is required CockroachDB tool #2 (§6.2); right now it is exercised only by tests against a live cluster | Record the cassette so embeddings exist offline, or fix AWS. Either way the demo must run the `<=>` branch |
| A-11 | MINOR | — | No `AS OF SYSTEM TIME` anywhere | Not currently claimed, so not a false claim. But it is the natural implementation for T-40's scrubber, and it is a strong CRDB differentiator sitting unused | Consider for T-40 |

---

## A.7 — Cluster shape at demo time

| # | Severity | File:line | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| A-12 | MAJOR | `client.py:23`, `server.py:71` | Default DSN is `postgresql://root@localhost:26257/colony?sslmode=disable` — **single node**. The server constructs `CockroachFleetMem()` with no DSN | The live demo runs single-node unless `COLONY_DSN` is set. The 3-node rig (`infra/cluster3.sh`) is reachable only from `infra/chaos.py` | Point the demo at the 3-node rig, or state the split on stage |

**Downgraded from FATAL, with reason.** The audit prompt calls this FATAL if the
demo runs single-node while the pitch claims survivability. Here the split is
*disclosed by design*: PRD §6.5 states the chaos segment runs on a self-hosted
3-node cluster precisely because the Cloud free tier hands you no nodes to kill,
and instructs narrating it honestly on camera. That is a defensible position, not
a hidden one. It becomes FATAL only if the node-kill claim is made over the
single-node demo without saying so.

---

## Could not verify

- **A.1 read-affects-behavior for `plans.based_on` chains** — needs a live run and a SQL query (AUDIT B.6). No cluster.
- **Whether `<=>` actually engages the index rather than full-scanning** — needs `EXPLAIN` against a live v26.2 cluster (`docs/setup-testing.md:230-234`).
- **Whether the console question at `questions.py:130` returns rows** — needs a cluster and a populated mission.
- **Cold-start behavior** — AUDIT E, blocked on the same cluster.

To unblock every one of these: start Docker, then `make dev`. Free, local, ~2 minutes.

---

## Summary for T-07 reconciliation

- Two of the four pitched memory systems (`hazards`, `mission_memories`) are **empty tables**. This is the single most damaging finding for judging criterion #1, and it is not in the current TODO at all.
- The vector index — required tool #2 — **does not run** on the demo path.
- The transactional lease core is genuinely correct and needs no work; T-13's acceptance criterion is likely already met and should be verified by X6 rather than rebuilt.
- T-10 conflicts with a documented PRD decision and needs a human ruling before any code moves.
