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

---

## Arm D — is omniscient pathing load-bearing? (prerequisite for T-12)

AUDIT B filed B-1 (`worker._passable`, `scout._landing` read raw terrain) and
B-2 (`worker._work_is_done` reads `world.objects` / `world.victim_at`), both
ungated by observation radius. T-12 says remove them. Measure first: if arm A's
0.994 depends on omniscience, removing it is a capability regression wearing a
fix's clothes.

Unobserved ground is treated as **passable** in the blinded arms. Pessimism
would make every unexplored tile a wall and the fleet would never leave home —
that measures the pessimism, not the god-mode.

| arm | condition | rescue rate (n=20) | 95% CI |
|---|---|---|---|
| A | as shipped | 0.994 | [0.984, 1.000] |
| **D** | **blind pathing** | **0.978** | **[0.958, 0.998]** |
| E | blind pathing + blind completion checks | 0.111 | — **retracted, see below** |

### Arm D: conclusive — omniscient pathing is NOT load-bearing

**CIs overlap (A [0.984, 1.000] vs D [0.958, 0.998]).** The fleet performs the
same reading only terrain it has actually seen. **B-1 is a free cleanup**: T-12's
pathing half can be done without arguing about a performance cost, and the
"agents cannot reach ground truth" claim becomes true for pathing at no price.

### Arm E: RETRACTED — it measured the harness, twice

Two successive stand-ins for a belief-driven `_work_is_done` both collapsed to
0.111, and both times the cause was the stand-in rather than the design:

1. First attempt queried `kind="debris"` beliefs. Agents write exactly two
   observation kinds — `hazard` and `victim` (grep of `report_observation` call
   sites under `agents/`). **There is no debris belief.** The query returned
   empty, so every clear looked finished the instant it was claimed.
2. Second attempt kept debris on ground truth and blinded only the victim
   branch, checking `payload.state in ("stabilized","lost")`. Inspected live:
   victim beliefs carry `payload = {'victim_id': 'v1', 'state': 'located',
   'note': 'sighted by scout'}` — **`state` is written once at sighting and
   never updated.**

**The finding is the gap, not the number.** Episodic memory records *what was
seen*, not *what happened next*. No belief row ever says a victim was
stabilized or a tile was cleared; those outcomes live in `world` (ground truth)
and in `events`. So **B-2 cannot be fixed by "read beliefs instead"** — there is
nothing to read. Fixing it means reading `events`/`tasks.status`, or writing
outcome observations, which is a design change and not a refactor.

No claim is made here about whether B-2 is load-bearing. It is unmeasured.

Harness: `audit/experiment_armd.py`. Raw: `audit/armd-raw.json`.

### Arm D, correction: "free cleanup" was wrong — it measured one metric

I built T-12a on arm D's verdict and the suite rejected it. **8 tests failed**,
across four independent properties:

```
tests/test_scout.py::test_a_second_scout_nearly_doubles_coverage        [fake, cockroach]
tests/test_scout.py::test_sector_claims_add_to_what_shared_memory_gives  [fake, cockroach]
tests/test_worker.py::test_an_idle_lifter_does_not_park_in_the_doorway   [fake, cockroach]
tests/test_logistics.py::test_a_returning_robot_gives_its_task_back      [fake, cockroach]
```

The sharpest one:

```
AssertionError: 68% of explored ground was covered twice
assert 0.6764275256222547 < 0.6
```

That test's docstring calls itself "the product claim in miniature" — two scouts
locking together and flying in formation is *the* duplicated effort this project
exists to remove. Blinding pathing reintroduced it.

**Arm D measured `rescue_rate` and nothing else.** Rescue rate is a floor
metric on this map: the fleet still saves everyone because it has 1200 ticks and
9 victims. It is insensitive to *how much waste* that took, which is exactly
what the blinding degraded. An ablation that moves only one number has not shown
the change is free — it has shown that one number did not move.

**Likely root cause: the blinding has no memory of failure.** A robot that walks
into an unseen wall discovers it, bumps, and immediately forgets — nothing is
written, so the next tick it plans the same route again. Real fog-of-war pathing
remembers what it collided with. Without that, robots re-attempt blocked routes
and converge on the same ground, which is precisely the 68% overlap.

**Change reverted. Tests untouched.** Not weakening a threshold that encodes the
product claim to make my own change pass.

**What T-12a actually needs**, and it is more than a gate:
1. Blind `_passable` / `_landing` to `visible_to` — as attempted.
2. **Give robots a bump memory**: record a tile discovered impassable, so a
   failed route is not re-planned. Write it as an observation and it becomes
   shared terrain knowledge, which is on-thesis rather than a workaround.
3. Re-measure against **duplicate-effort index and scout overlap**, not just
   rescue rate.

### Arm D, bisected: what omniscient pathing is actually load-bearing *for*

Applied the two halves separately.

| change | result |
|---|---|
| scout `_landing` blinded, alone | the **4 scout failures**, worker/logistics green |
| worker `_passable` blinded, alone | the **4 worker/logistics failures** |

Both regress independently, so this is not one bug with a wide blast radius.

**The mechanism, and it is not what "god-mode" suggests.** A scout has vision 6
and speed 3 — it always sees the tile it is about to enter, so blinding cannot
be about bumping into things. It is about *multi-step route planning*.
`find_move_plan` uses `_landing` to predict where each move ends, over routes
extending well past current vision. With ground truth the planner routes around
walls it has not seen; blinded, it assumes unseen ground is open and plans naive
straight lines.

**Walls are what make two scouts' paths diverge.** Remove wall knowledge from
planning and two scouts from neighbouring spawns plan near-identical straight
lines to the same frontier — they fly in formation, and overlap goes from under
60% to 68%. That is precisely the failure
`test_a_second_scout_nearly_doubles_coverage` was written to catch.

So the sharp statement is: **omniscient pathing is load-bearing for
de-duplication, not for rescue rate.** Arm D was right that rescue rate does not
move and wrong that nothing does. `test_sector_claims_add_to_what_shared_memory_already_gives`
failing alongside it says the same thing from the other side — sector claiming's
measured benefit shrinks when routing stops distinguishing robots.

**This makes T-12a a memory feature, and an on-thesis one.** The fleet needs
*shared learned terrain*: a wall discovered by any robot, written as an
observation, read by every planner. Then scouts diverge for the right reason —
because the fleet remembers the map together — instead of the wrong one, which
is that each robot was born knowing it. That is a better demo than the cleanup
was, and it is the same argument the project already makes about victims,
applied to terrain.

Reverted; the four tests pass untouched.

---

## X10 — statistical honesty across generated scenarios: PASS, with a scope condition

X1's zero-variance arms were not an underpowered experiment, they were **one
fixed problem reseeded**. Aftershock's count of victims reachable without a
handoff is a property of that layout; changing the RNG seed never moves it. A
`±` on those arms would have described the harness.

So this varies the *scenario* — size, debris density, victim count, sector size,
fleet composition — using `tests/test_scenarios.py`'s own generator, paired
design (both arms on the identical map).

### First result: delta was exactly 0.000, on every scenario

```
scenario  0  32x18 v=5  coord=1.000 base=1.000 delta=+0.000
scenario  1  24x22 v=5  coord=1.000 base=1.000 delta=+0.000
...
paired delta   +0.000 +/- 0.000      excludes zero: False
```

**Cause, and it is a real scoping finding.** The generator deliberately clears
every victim tile — its own comment says "a victim is never buried". So a medic
can always walk straight to any victim, the `clear_debris -> deliver_kit` chain
never has to form, and **coordination has nothing to coordinate**.

PRD §5.5 already says the demo map is "designed so ≥2 victims are unreachable
without handoffs". That is not incidental map dressing — it is the precondition
for the entire effect.

> **Coordination's measured benefit is not a universal property of the fleet.
> It is a property of scenarios that require handoffs.** On maps where every
> victim is directly reachable, shared memory makes no measurable difference to
> rescue rate.

That is the honest answer to "does this only work on your map?", and it is much
better said first than discovered by a judge.

### Second result: bury half the victims, and the effect is large and robust

Walling each buried victim in behind debris on its orthogonal neighbours — the
same property Aftershock has, reproduced across generated maps — **n=40**:

| arm | rescue rate | 95% CI |
|---|---|---|
| coordinated | **0.787** | ± 0.091 |
| baseline | 0.474 | ± 0.040 |
| **paired delta** | **+0.313** | **[+0.234, +0.393]** |

**Interval excludes zero. X10 PASSES.** 6 and 7 distinct values across the arms,
so these are genuine intervals rather than restated constants.

Mean victims lost: **0.95 coordinated vs 2.40 baseline**.

### What to put in the deck

Not "98% vs 44%", which is one map. This:

> Across **40 randomly generated disaster scenarios** — varying map size, debris
> density, victim count and fleet composition — shared memory raises the rescue
> rate by **31 percentage points (95% CI: 23–39)** and cuts mean victims lost
> from **2.4 to 0.95**. The effect requires scenarios where victims are trapped
> behind debris; where every victim is directly reachable, it is zero.

The scope condition belongs in the claim. It costs nothing — the scenario the
product exists for is the trapped one — and it is the difference between a
result and a marketing number.

Harness: `audit/experiment_x10.py`. Raw: `audit/x10-raw.json`.
