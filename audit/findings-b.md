# AUDIT B — is it actually coordinating?

**Verdict:** SUSPECT — the central claim is **true and measurable**, but the
demo's own ON/OFF toggle understates it by ~2.2x, and two supporting claims
(contention, god-mode-free agents) do not hold.

**Headline: coordination works, and X1 proves it.** With every cross-agent read
disabled, the fleet rescues **0 of 9 victims**. With coordination on it rescues
**9 of 9**. That is not a marginal effect; it is the difference between a fleet
and six machines.

All numbers below are from real runs against `FakeFleetMem` (no cluster was
reachable — Docker was down at audit time). The fake mirrors every `client.py`
signature and `tests/test_contract.py` fails if they drift, so behavioural
conclusions carry; anything isolation- or SQL-specific is marked UNVERIFIED.

---

## B.1 — God-mode check

| # | Severity | File:line | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| B-1 | MAJOR | `worker.py:571-580` | `_passable` → `world.passable(...)` — raw terrain | A robot routes through terrain it has never observed, **in both modes**. The docstring defends this as avoiding a duplicated sim rule, which is a real concern, but the effect is omniscient pathing | Route over `visible_to(robot_id)`; treat unobserved tiles as optimistically passable so the robot discovers walls by bumping |
| B-2 | MAJOR | `worker.py:584-593` | `_work_is_done` reads `world.objects[...]` and `world.victim_at(...)` | Ground-truth victim *state*, ungated by observation radius. A robot knows a victim was stabilized without anyone telling it | Read from `observations` / `victims` instead |
| B-3 | MINOR | `worker.py:566` | `_landing` → `world.occupied(...)` | Other robots' live positions, ungated | Defensible as local collision sensing within `speed` tiles; document the radius |

**Inputs to agent decisions, enumerated.** `Worker.step(world)` (`worker.py:133`)
and `Scout.step(world)` (`scout.py:116`) both take the `World` object itself.
Reads reaching ground truth: `world.robots[self.robot_id]` (own state, fine),
`world.map.spawn_points` (`worker.py:466`, static scenario data, fine),
`world.tick` (fine), plus B-1/B-2/B-3 above. Everything victim- and
hazard-related otherwise flows through `self.mem`.

So the god-mode surface is **narrow but real**: terrain and task-effect
verification bypass memory; belief-level knowledge does not.

---

## B.2 — Central scheduler check

**Checked and clean — report as a negative result.**

There is no central allocator. Each robot self-selects: `worker._find_work`
(`worker.py:250-311`) reads `open_tasks`, ranks by `allocation_score`, and claims
for itself; `scout` does the same for sectors (`scout.py:437-453`). The
orchestrator (`orchestrator/lost.py:1-24`) only marks robots `lost` for the UI
and event log, and its docstring explicitly refuses to release tasks or call
`heartbeat()` because either would put a supervisor on the recovery path.

Allocation is genuinely decentralized.

---

## B.3 — The coordination toggle: what it really does

**One sentence:** the toggle gates shared *vision* (`world.py:558-560`), hazard
*beliefs* (`beliefs.py:69-70`), sector seeding (`mission.py:65-68`),
transactional *claiming* (`worker.py:281`), staging (`worker.py:437-438`) and
dependency *unblocking* (`worker.py:614-618`) — but **not** the shared task
queue, which both modes read unconditionally.

| # | Severity | File:line | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| B-4 | **FATAL** | `worker.py:264`, `scout.py:444` | `self.mem.open_tasks(self.mission_id)` is called with no `coordinated` guard | The audit's criterion is "with coordination off, an agent must be unable to see rows written by other agents." A baseline lifter learns debris is at (4,27) from a task row a **scout** wrote. The shipped baseline is not an ablation — it is coordination with the claiming removed | Filter `open_tasks` to own-authored rows in baseline, or give baseline robots private memory (T-11) |

**This is measured, not asserted.** X1 arm B (shipped baseline) rescues
**0.444**; arm C (true isolation) rescues **0.000**. The contamination is worth
44 percentage points — more than the toggle's *entire* visible effect on stage.

Note `worker.py:437-438` *does* guard `open_tasks` for staging, and
`worker.py:614-618` guards completion. So the guard was clearly intended at
`worker.py:264` and is simply missing. This reads as a bug, not a design choice.

**Consequence for the demo:** the ON/OFF toggle currently shows 44% → 98%. The
honest ablation is **0% → 98%**. Fixing B-4 makes the demo more dramatic, not
less.

---

## B.4 — Lease contention

| # | Severity | Evidence | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| B-5 | MAJOR | `audit/experiment_b.py`, seed 0 | **31 claim attempts, 31 wins, 0 contended.** Zero distinct contended tasks | The lease-contention branch — the entire "why a database and not a queue" argument (§3.6) — **never executes in a normal run**. `tests/test_claiming.py` proves it works under 1,000 synthetic races, but nothing in the demo exercises it | Construct the contention scenario (X6) and give the demo a real moment to point at |

Not "impossible by construction" — tasks are not partitioned in advance, and two
robots of the same role *can* target one task. It simply never happens on the
Aftershock map with this fleet size, because the allocation score separates
robots before they collide.

---

## B.5 — Lease expiry

**Checked and clean.**

Expiry is evaluated inside the claiming `UPDATE`'s `WHERE` clause against the
database's `now()` (`client.py:332-343`), not by wall clock, tick count, or a
sweep. There is no separate reclaim path to race: a takeover *is* a claim, and
under SERIALIZABLE exactly one wins.

**The race the prompt asks about is real but bounded and handled:** an original
holder whose lease lapsed can still be mid-action. `_complete` guards it —
`complete_task` applies only while the robot still owns the task, returns `None`
otherwise (`client.py:373-397`), and the caller cools the task off rather than
re-picking it (`worker.py:620-627`). The docstring at `worker.py:605-612`
records that this exact bug was found and fixed.

---

## B.6 — Provenance

**Measured on seed 0, coordinated:**

- plan rows: **34**
- with non-null `based_on`: **31 (91%)**
- max `based_on` width: **12**; mean **2.88**
- decision source: `{'rules': 34}` — **zero Bedrock decisions**

| # | Severity | Evidence | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| B-6 | MINOR | `schema/v1_1.sql:141` | `based_on` is a flat `UUID[]` of observation ids — plan → observations. There is no plan → plan edge | Provenance **depth is always 1**. T-24 promises a "causal graph"; what exists is a one-level fan-out of width ≤12. The data is real and rich, but "chain" overstates its shape | Either render it honestly as a fan-out, or add plan→plan edges |
| B-7 | MAJOR | `audit/experiment_b.py` | Every decision in the run came from rules, none from Bedrock | Corroborates A-10 from the decision side. `plans.chosen.source` is the field that makes "the LLM is driving this" checkable — and it currently reads `rules` for 100% of rows | Record the cassette |

91% coverage with mean width 2.9 is **genuinely good provenance**, not nominal.
Report as a positive finding.

---

## B.7 — Cross-agent dependency: the headline

**FOUND.** Concrete instance from seed 0:

```
observation  id=7656d965-3884-40b2-8f72-161acd3b9f2a
             author=s2   kind=victim   pos=(12,10)   sightings=2

plan         robot=m1   trigger=idle
             chosen={'action':'claim_task', 'kind':'deliver_kit',
                     'target':[12,10], 'source':'rules'}
             rationale='best of 1 open deliver_kit tasks by role match,
                        priority and distance'
```

Medic `m1` delivered a kit to (12,10) **only** because scout `s2` wrote that
observation. `sightings=2` additionally shows the reconcile gate merging two
scouts' sightings into one belief rather than dispatching the fleet twice.

This is the thing the audit said would be the headline finding if it were
missing. It is present, and it is reproducible.

---

## B.8 — Memory taxonomy

| memory type | table | writer | reader | one decision it changed |
|---|---|---|---|---|
| WORKING | `tasks` | `client.py:299` | `client.py:416` | `m1` claims `deliver_kit` (B.7) |
| WORKING | `robots` | `client.py:461` | `client.py:475` | none — UI/events by design |
| WORKING | `victims` | `client.py:261` | console only | **none** (A-3) |
| WORKING | `hazards` | — | — | **table is empty** (A-1) |
| EPISODIC | `observations` | `client.py:116` | `client.py:206` | `m1`'s target above; hazard routing `beliefs.py:72` |
| PROVENANCE | `plans` | `client.py:498` | `client.py:512` | none — console/UI by design |
| PROVENANCE | `events` | `client.py:531` | `client.py:536` | none — metrics by design |
| SEMANTIC | `mission_memories` | — | — | **table is empty** (A-2) |

Two of eight tables are never touched. Of the four *pitched memory systems*,
one (SEMANTIC) has no implementation at all.

---

## Checked and clean
- No central allocator (B.2), verified by reading every call site of `open_tasks`.
- Lease expiry uses DB `now()` with no sweep and no second recovery path (B.5).
- The stale-lease/mid-action race is guarded and regression-tested (`worker.py:605-627`).
- Cross-agent dependency exists and is reproducible (B.7).
- Provenance is real: 91% of plans cite sources (B.6).

## Could not verify
- Whether contention behaves identically under real SERIALIZABLE rather than the fake's lock — needs X6 against a cluster.
- Whether `based_on` ids resolve to rows via SQL join in the console — needs a cluster.
- B-1/B-2 severity under a real run: god-mode pathing may be *why* arm A reaches 98%. Quantifying that needs an arm D (coordinated + blind pathing), which is now the most interesting open experiment.
