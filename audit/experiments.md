# Experiments (T-06)

Run against `FakeFleetMem` — no cluster was reachable during this pass. The fake
mirrors every `client.py` signature (`tests/test_contract.py` fails on drift), so
behavioural results carry; isolation- and SQL-level results are marked as needing
a cluster.

All runs use `BedrockAdapter(mode=REPLAY)` explicitly: no network, no spend.

| id | pass/fail | measured | expected | notes |
|---|---|---|---|---|
| **X1** | **PASS** | A=0.978±0.020, B=0.444±0.000, C=0.000±0.000 (n=20) | A and C separate, CIs disjoint | The project's central claim holds emphatically. See below |
| **X2** | **FAIL** | behaviourally identical on 2/3 seeds; seed 0 diverged | byte-identical | Root cause found — see below |
| X3 | not run | — | — | Cold reconstruction needs a cluster |
| **X6** | **not run** | contention never occurs naturally (0/31 claims contended) | 100/100 single grant | Scenario must be constructed first; see B-5 |
| **X7** | **PASS** | 902 store calls over 339 ticks = 2.7/tick | scales with ticks/agents, not flat | Store is genuinely in the decision loop |
| X8 | not run | — | — | Import guard not yet written (T-12) |
| X4, X5, X9, X10 | not run | — | — | X5 needs the 3-node rig; X9/X10 follow AUDIT C |

---

## X1 — Ablation (the one that matters)

Three arms, because AUDIT B found the shipped baseline is not a clean ablation:

| arm | condition | rescue rate (n=20) | 95% CI |
|---|---|---|---|
| A | coordinated, as shipped | **0.978** | [0.958, 0.998] |
| B | baseline, as shipped | **0.444** | [0.444, 0.444] |
| C | **true isolation** — per-robot memory, no cross-agent reads | **0.000** | [0.000, 0.000] |

CI overlap A vs B: **no**. A vs C: **no**. **X1 PASSES.**

**Interpretation.** With shared memory, the fleet rescues 9 of 9. With
cross-agent reads disabled but every write still happening, it rescues **0 of 9**
— robots write beliefs nobody reads and never form a task chain. Shared memory is
not decorative; it is the entire mechanism.

**Arm B is the finding.** The shipped "coordination OFF" mode still reads the
shared task table (`worker.py:264`, `scout.py:444` — see B-4), so it inherits
44 percentage points of coordination for free. **The demo's own toggle
understates the real effect by 2.2x.** Fixing B-4 turns the on-stage delta from
44%→98% into 0%→98%.

**Caveat on arm C.** 0.000 is a floor. Isolated robots cannot form the
scout→lifter→medic chain at all, so no victim behind rubble is ever reachable.
This is the correct ablation, but the effect size should be reported as
"coordination is necessary for any rescue at all" rather than as a tunable delta.

Raw data: `audit/x1-raw.json`. Harness: `audit/experiment_x1.py`.

---

## X2 — Determinism: FAIL, with root cause

Two distinct defects, needing different fixes:

**1. Row ids are non-deterministic (all seeds).** Task and belief UUIDs are
freshly generated per run, so a byte-diff of the event log always fails. Masking
ids isolates this from real divergence.

**2. Genuine behavioural non-determinism (intermittent).** Seed 0, same seed,
two runs:

```
index 241:  run1 = ('s2', 'sector_claimed', '{"sector": "C1"}')
            run2 = ('s2', 'sector_claimed', '{"sector": "A3"}')
```

Event counts for the same seed also varied across invocations within this
session (seed 7: 935 then 805; seed 5: 746 then 825).

**Root cause — `scout.py:447-450`:**

```python
key=lambda t: (
    abs((t.target[0] or 0) - robot.x) + abs((t.target[1] or 0) - robot.y),
    str(t.id),          # <-- tiebreak on a RANDOM UUID
),
```

When two sectors are equidistant, the winner is decided by the string form of a
randomly generated UUID. The comment above it (`scout.py:438-440`) explains the
distance sort but not the tiebreak, which appears to have been added for
stability and instead introduced the exact instability it looks like it prevents.

**Fix:** tiebreak on something stable — the sector name, or `(target_y, target_x)`.
One line. Blocks X10 and the "run it again" demo risk until fixed.

Harness: `audit/experiment_x2.py`.

---

## X7 — Query counter: PASS

Seed 0, coordinated, 339 ticks:

| call | count |
|---|---|
| `open_tasks` | 730 |
| `get_beliefs` | 128 |
| `report_observation` | 44 |
| **total** | **902** (2.7/tick) |

Not a flat line — store access scales with ticks and agent activity, confirming
results are not cached out of the decision loop.

**Efficiency note (MINOR):** 730 `open_tasks` calls produced 31 claims. Every
idle robot re-queries the full open-task set every tick. Harmless against the
fake; against Cloud with per-call latency it is the first thing that will hurt.

Harness: `audit/experiment_b.py`.

---

## Second pass — against a live CockroachDB cluster

`make dev`, single node, schema applied fresh. Seed 0, coordinated, replay adapter.

| id | pass/fail | measured | notes |
|---|---|---|---|
| **X9** | **partial PASS** | `victims_stabilized` 9=9, `victims_lost` 0=0, `victims_total` 9=9 recomputed from SQL | The store-derived `Metrics` object is honest. The *server's merged payload* is not — see C-1 |
| **cold start** | **PASS** | fresh schema → 617 passed / 12 skipped → full mission → console answers | AUDIT E |
| **console** | **PASS (4/5)** | `why_did_robot` resolves `based_on` to real observation rows | `what_do_we_know` returns 0 rows on default params (D-2) |
| **vector index** | **not exercised** | `EXPLAIN` → `observations@observations_pkey, spans: FULL SCAN` | Index is correctly declared `VECTOR INDEX … vector_cosine_ops`; the table holds 23 rows, so a full scan is the optimizer being right. Required tool #2 is never exercised at demo scale (D-3) |

**Correction to A-10.** A-10 claimed the vector path was dead for lack of
embeddings. Wrong: 23 of 23 observations carry embeddings. They are produced by
`_offline_embedding` (`adapter.py:139`), not Titan — which is the worse finding
(D-4).

Suite with a cluster: **617 passed, 12 skipped**. Without: 467 passed, 162
skipped. The database-backed tests genuinely execute.

---

## Third pass — after the determinism fixes (T-08, T-08b)

**X2 now PASSES on its stated condition.** Byte-identical event logs, all three
seeds:

```
seed  0  events=  742  byte_identical=True  behaviourally_identical=True
seed  7  events=  805  byte_identical=True
seed 19  events=  641  byte_identical=True
```

Seed sensitivity intact — 4 distinct event logs across 4 different seeds — so
determinism did not come at the cost of the seed meaning anything.

**X1 re-run on the now-deterministic sim (n=20 per arm).** These are the numbers
for the deck:

| arm | rescue rate | 95% CI |
|---|---|---|
| A coordinated | **0.994** | [0.984, 1.000] |
| B baseline, as shipped | 0.444 | [0.444, 0.444] |
| C true isolation | **0.000** | [0.000, 0.000] |

Arm A tightened from 0.978 ± 0.020 to **0.994 ± 0.011** — the earlier spread was
partly the sector-tiebreak nondeterminism T-08 removed, i.e. the harness rather
than the fleet. CIs remain disjoint; **X1 PASSES**.

**Arms B and C still have zero variance**, and this is now a finding about the
*scenario*, not the harness: the shipped baseline rescues exactly 4 of 9 and true
isolation exactly 0 of 9 on every seed. Both outcomes are structurally pinned by
the map — the reachable-without-help victim count is fixed — so `n=20` buys no
confidence for those arms. Reporting them as a mean ± CI would overstate the
statistical work done. Report them as what they are: constants of this map.

**X10 status:** the mean ± CI machinery now works and arm A is a genuine
interval. Before putting intervals on B and C in the deck, the map needs enough
variation for the outcome to move — otherwise the honest statement is "4 of 9,
every run".
