# AUDIT D — are the integrations real?

**Verdict:** SUSPECT — CockroachDB is real and verified end to end against a live
cluster. **AWS Bedrock is not running at all**, and a silent fallback fills the
gap with locally generated vectors that are indistinguishable from Titan output
once they are in the database.

Run against a live single-node cluster (`make dev`), seed 0, replay adapter, $0.

---

## D.1 — The Commander Console

**It is not an LLM. It is five parameterized SQL statements with Python-rendered
summaries.** `console/questions.py:30-46` defines a `Question` dataclass holding
`sql` and a `render` callable; `answer()` (`questions.py:309-332`) binds
parameters, calls `reader.read(sql, values)`, and formats. There is no model call
anywhere in `console/`.

| # | Severity | File:line | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| D-1 | MAJOR | `console/questions.py:1-3`, PRD §6.2 | The code is honest — it says "canned" in six places. **The pitch is not.** §6.2 describes "a human asks natural-language questions and the AI answers by querying live fleet memory" | There is no natural language and no AI. Five buttons run five fixed queries. A judge who reads §6.2 and then clicks a chip will notice | Either say "five audited queries over live fleet memory, read-only" — which is a *better* claim for §5.4 reliability — or put a model in front of it |
| D-2 | MAJOR | `console/questions.py:294` | `DEFAULTS = {"x": 0, "y": 0, ...}` | `what_do_we_know` defaults to the origin, where nothing ever happens. Live output: **"nothing has been observed near there"** | Default to a victim's coordinates, or to the fleet centroid |

**"unavailable — running on fake memory"** is `client/app.js:662`, a status line
driven by `data.memory`. It is honest UI reporting the A-7 fallback, not a stub.
**With a cluster up it goes away** — verified below.

### D.2 — The five chips, run against real rows

All five are implemented. None are dead. Live output, mission `a3a7dec5…`:

| chip | memory | rows | answer |
|---|---|---|---|
| `unreached_victims` | working | 9 | "9 victim(s) not yet stabilized: (28,26) located — waiting on deliver_kit [open], (3,27) located — no task yet, …" |
| `who_holds_what` | working | 1 | "1 task(s) in flight: m1 (medic) holds deliver_kit at 34,16" |
| `why_did_robot` | provenance | 5 | "most recently, on idle, it decided: best of 2 open deliver_kit tasks… (decided by rules) based on 12 memory row(s): hazard at 32,10 (2 sighting(s), confidence 1.00)…" |
| `what_do_we_know` | episodic | **0** | "nothing has been observed near there" — D-2 |
| `aftershock_response` | provenance | — | ran without error |

`why_did_robot` is the strongest thing in the demo. It resolves `plans.based_on`
to actual observation rows with sightings and confidence — a real decision trace,
not a story. **T-16's acceptance criterion is already met** once a cluster is up.

---

## D.3 — Vector search: real query, correct index, wrong scale

**It is genuine cosine vector search, not `LIKE`.** `client.py:174-179`:

```sql
SELECT id, embedding <=> %s AS distance FROM observations
 WHERE mission_id = %s AND embedding IS NOT NULL AND kind = %s
   AND pos_x BETWEEN %s AND %s AND pos_y BETWEEN %s AND %s
 ORDER BY embedding <=> %s LIMIT %s
```

The index is correctly built — `SHOW CREATE TABLE observations` confirms:

```sql
VECTOR INDEX obs_embedding_idx (mission_id, embedding vector_cosine_ops)
```

The schema comment at `v1_1.sql:90-97` warned that a bare index would silently
degrade to a full scan; that warning was heeded and the opclass is right.

| # | Severity | Evidence | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| D-3 | MAJOR | `EXPLAIN`, this run | The optimizer **full-scans** anyway: `table: observations@observations_pkey, spans: FULL SCAN`, and CockroachDB recommends a plain `(mission_id) STORING (embedding)` index instead | Not a defect — the table holds **23 rows**. A full scan is correct at that size. But it means required tool #2 is **never actually exercised** in a demo, and a judge running `EXPLAIN` sees a full scan | Seed enough observations that the index engages, or show the `EXPLAIN` on a larger table and say so |

**Correction to AUDIT A-10.** A-10 claimed the vector path was dead because no
embeddings existed. That was wrong: **23 of 23 observations carry embeddings.**
The `<=>` branch does run. What does not run is the index — and where those
embeddings come from is D-4.

---

## D.4 — AWS: the silent fallback

| # | Severity | File:line | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| D-4 | **FATAL** | `bedrock/adapter.py:139-140` | `return cached if cached is not None else _offline_embedding(text)` | In REPLAY with no cassette — **the default mode, and the current state of this machine** — every embedding is generated locally. The database now holds 23 rows of `VECTOR(512)` that look exactly like Titan V2 output and are not. Nothing at runtime says so | Record the cassette, or make a cassette miss loud in REPLAY (keep the fallback only on the `_is_transient` throttle path at `adapter.py:151-156`, where it is genuinely correct) |
| D-5 | MAJOR | `audit/experiment_b.py` | Every plan row in a full mission reads `source: rules`. Zero Bedrock decisions | The claim "Claude decides at decision boundaries" is currently false on this machine. `plans.chosen.source` makes it *checkable*, which is good design — it currently checks out as `rules` | Record the cassette |

**What is actually running on AWS in the demo path: nothing.** No Bedrock call is
made (replay), no deploy exists (`AUDIT A`: no Dockerfile, no ECS task
definition, no S3/CloudFront). The AWS integration is real code with a real live
path (`adapter.py:149-161`) that has never been executed against AWS from this
checkout.

**Say this plainly on stage rather than letting a judge find it.**

---

## D.5 — Live updates: push, not poll

**Checked and clean.** The UI receives pushed diff frames over a websocket —
`sim/server.py` maintains a per-viewer `asyncio.Queue` (`server.py:89-92`) and
broadcasts each tick. The changefeed module exists (`fleetmem/changefeed.py`) but
is deliberately unadopted and documented as such (`TODO.md:97-101`).

Pitch and implementation match: the UI is push-based over websockets, and the
project does **not** claim CDC-driven UI.

---

## D.6 — Every model call, enumerated

| call | file:line | input | output | affects simulation? | silent fallback? |
|---|---|---|---|---|---|
| `embed` | `adapter.py:132-161` | belief description text | 512 floats | **Yes** — feeds the reconcile gate's `<=>` merge | **Yes — D-4** |
| `plan` | `adapter.py:163-210` | robot, tick, digest, candidate tasks | `Plan` or `None` | **Yes** — reorders candidates (`worker.py:277-278`) | **Yes** — `planning.py:207` catches all, falls to rules (A-8) |

Both fall back silently. Neither surfaces the degradation to the UI or the event
log. `plans.chosen.source` records it after the fact, which is the one thing
making this auditable at all.

---

## Checked and clean
- CockroachDB is genuinely the store: **617 passed, 12 skipped** against the live cluster (up from 467/162 with no cluster). The DB-backed tests really run.
- X9 recomputation from SQL alone: `victims_stabilized` 9 = 9, `victims_lost` 0 = 0, `victims_total` 9 = 9. The store-derived `Metrics` object is honest. (The *server's* merged payload is not — see C-1.)
- Vector search is real cosine over a real `VECTOR(512)` column with a correctly-declared cosine index.
- Console is read-only twice over: an in-code statement guard (`reader.py:42`) plus the `commander` grant.
- The console answered 4 of 5 questions with substantive content from real rows.
