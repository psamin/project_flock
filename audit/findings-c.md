# AUDIT C — are the stats real?

**Verdict:** FAKE — not fabricated, but **not what the code claims**. Four of the
eight header numbers are read from the live `World` object, not from the event
log, because a dict merge silently overwrites the store-derived values with
ground truth.

`sim/metrics.py:1-8` opens with:

> "From `events`, deliberately — not from live objects. […] Anything computed
> another way would be a second source of truth that could disagree with the one
> on screen."

That promise is defeated one line at a time in `sim/server.py:240`.

---

## C.1 — Metric source table

| metric | displayed | computed | source | denominator | unit | store or UI state |
|---|---|---|---|---|---|---|
| `tick` | `app.js` header | `world.py:635` | **World** | — | ticks | **World** |
| `located` | `app.js:431` | `world.py:637` | **World** — `sum(state != "unknown")` | — | victims | **World** |
| `stabilized` | `app.js:432` | `world.py:638` **overwrites** `metrics.py:80` | **World** | — | victims | **World** |
| `lost` | `app.js:433` | `world.py:639` **overwrites** `metrics.py:81` | **World** | — | victims | **World** |
| `rescue rate` | `app.js:435` | `metrics.py:86` | event log | `victims_total` (from World, `server.py:234`) | % | **mixed** |
| `median` | `app.js:438-441` | `metrics.py:93-97` | event log | censored at `horizon` | **ticks** | store |
| `duplicate effort` | `app.js:436` | `metrics.py:109-125` | event log (`tile_visited`) | total visits | % | store |
| `explored` | header | `world.py:575-584` | **World** | reachable non-wall tiles | % | **World** |

---

## Findings

| # | Severity | File:line | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| C-1 | **FATAL** | `sim/server.py:231`, `sim/server.py:240` | `return {**self._metrics, **self.world.metrics()}` — the World dict is merged **on top of** the store-computed one | `world.metrics()` (`world.py:632-641`) emits colliding keys `victims_total`, `victims_stabilized`, `victims_lost`. **The World value silently wins for every one.** The scoreboard's stabilized and lost counts are ground truth, not memory. X9 and X3 both fail on this | Namespace the live counters (`live_*`) so they cannot collide, or drop them and read everything from the log |
| C-2 | **FATAL** | `world.py:637` | `victims_located` exists **only** in `world.metrics()`; it is not in `Metrics.to_json()` (`metrics.py:36-45`) | The `located` number a judge reads cannot be recomputed from the database at all. It is a count of victims whose *simulator* state is not `unknown` — the fleet's memory is never consulted | Derive from `observations` where `kind='victim'`, which is the number that actually means "the fleet knows about this person" |
| C-3 | MAJOR | `metrics.py:87-97` | `median_time_to_stabilize` censors unrescued victims at the horizon (`limit`), counting them as `mission_length_ticks` | Statistically defensible and well-argued in the comment — but the label says "median" and the UI shows a bare number (`app.js:438-441`). A stranger reads `median 121` as "typical rescue took 121 ticks" when unrescued victims were silently scored as 1200 | Label it `median time-to-stabilize (unrescued censored at 1200 ticks)`, or show n-rescued alongside |
| C-4 | MAJOR | `app.js:435`, `app.js:432` | `rescue rate` (event log) is displayed beside `stabilized` (World) | Two numbers with the same underlying meaning, drawn from two sources. The screenshot's `stabilized 5` / `rescue rate 63%` is exactly this: 5/8 from the log next to 5 from the world. They agree today by luck, not by construction | Same fix as C-1 |
| C-5 | MAJOR | `world.py:575-584`, `world.py:637` | `explored 100%` and `located 8` come from different notions of knowing | `coverage()` counts *tiles revealed*; `victims_located` counts *victims whose simulator state changed*. A map can be fully revealed while a victim has not yet been reported into memory, because revealing a tile and writing an observation are different events | Define both from the store, or label them `tiles seen` and `victims in memory` |
| C-6 | MINOR | `client/app.js:158` | `Math.random()` in the renderer | Screen-shake only — does not touch simulation state, so reproducibility is unaffected. Reported because the audit asked for every unseeded call site | Seed it or leave it; cosmetic |
| C-7 | MINOR | `metrics.py:100-105` | `double_work_incidents` counts tasks claimed by >1 *distinct* actor across the mission | Correct and well-documented, but it is **not displayed anywhere** in `app.js` | Surface it — it is the most direct "coordination is working" number the project has |

---

## C.5 — `duplicate effort` formula, as requested

`metrics.py:109-125`:

```python
redundant / len(visits)
```

- **Numerator:** a `tile_visited` event for a tile **another** robot has already visited. A robot revisiting its own ground does not count (`metrics.py:113-114`).
- **Denominator:** *total* tile visits, not useful ones.
- **Counts:** re-observation of already-covered ground — **not** re-attempt of a claimed task. That second thing is `double_work_incidents`, a separate metric (C-7).

Measured on seed 0 coordinated: **0.247**. The label "duplicate effort 23%" in
the screenshot is therefore honest about what it computes, but the word
"effort" implies wasted *work* when it measures wasted *walking*.

---

## C.6 — Is `lost 0` falsifiable?

**Yes, mechanically. No, in practice.**

- Loss is reachable: `world.py:433-437` sets `state = "lost"` and emits `victim_lost` once `tick >= victim.vitals_deadline`.
- Earliest deadline on Aftershock: **470**. Mission horizon: **1200**.
- But a coordinated run **finishes at ~339 ticks** (measured, seed 0) because `world.finished` returns true once every victim is stabilized (`world.py:648-651`).

So the fleet always wins before the first deadline can bite. `lost 0` is not
unfalsifiable — it is unreachable *on this map with this fleet*. §3.4 of the
design critique asks for victim deterioration as real pressure; the mechanism
already exists and is simply tuned out of range.

**Cheapest fix in the whole audit:** lower the deadlines (or raise fleet
travel time) so triage becomes a real allocation problem and `lost 0` becomes an
achievement rather than a certainty.

---

## C.8 — Reproducibility

**Seeding discipline is genuinely good** — report as a positive finding.

| call site | seeded? |
|---|---|
| `world.py:123` `self.rng = random.Random(seed …)` | yes |
| `world.py:426` `self.rng.choice(sorted(frontier))` | yes — and sorted first, which is the correct pattern |
| `scout.py:112` `random.Random(self.seed)` | yes |
| `build_aftershock.py:46` `random.Random(SEED)` | yes (map generation, fixed) |
| `client/app.js:158` `Math.random()` | no — cosmetic only (C-6) |

`world.py:11` even carries a docstring promising nothing calls `time.time()` or
an unseeded random, and that promise holds.

**The X2 failure is therefore not an RNG defect.** It is a sort key:
`scout.py:449` tiebreaks equidistant sectors on `str(t.id)` — a random UUID. See
`audit/experiments.md` → X2.

---

## Checked and clean
- No hardcoded constants inside metric computation. `COVERAGE_AT_TICK = 500` (`metrics.py:16`) is a genuine §4.7 scenario constant, cited.
- No metric is computed in the frontend from client state — `app.js:431-441` only formats server-provided values. The defect is on the server (C-1), not the client.
- `coordination_gain` (`metrics.py:153-161`) refuses to divide by a zero baseline and returns 0.0 rather than an infinite win. This is the single most judge-proof line in the file.
- `rescue_rate_delta` (`metrics.py:147-149`) is computed live from both runs in the same session — no stored baseline anywhere. **C.7 passes.**

## Could not verify
- Whether the displayed `explored` and the store's `tile_visited` count agree on a live cluster — needs X9, now unblocked (cluster is up).

---

## C.6 — CORRECTION: `lost` is reachable, and it is the strongest number in the demo

The original C.6 concluded that victim loss was "mechanically reachable, not
reachable in practice", because a coordinated run finishes at ~339 ticks and the
earliest `vitals_deadline` is 470. **That was measured on the coordinated arm
only, which is exactly the arm that never loses anyone.**

Measured across both arms, 6 seeds:

| arm | ticks | stabilized | **lost** | rescue rate |
|---|---|---|---|---|
| coordinated | ~312 | 9 | **0** | 1.000 |
| baseline | 560 | 4 | **5** | 0.444 |

Baseline runs to tick 560 precisely *because* it fails — the mission does not end
early, the deadlines arrive, and **five people die, every run**. Total over 6
baseline runs: 30 victims lost.

**T-19 is not needed. Nothing needs retuning.** The deterioration mechanism
(`world.py:433-437`) works, `victim_lost` fires, and `metrics.py:71` counts it.

**And this is the most powerful number the project has.** The current headline is
a rescue-rate delta — 44% against 98%, a percentage a judge has to think about.
The same runs support:

> Coordination off: **five people die**. Coordination on: **nobody does**.

That is the §4.7 comparison stated in lives rather than percentages, it comes
from the event log, it reproduces on every seed, and it is already computed and
already on the scoreboard — just never contrasted. Recommend surfacing
`victims_lost` in the ON/OFF comparison panel alongside the rescue-rate delta.

**Audit-integrity note.** This is the fourth correction in this audit (after
A-10, arm E, and arm D). The common cause each time was measuring one arm or one
metric and generalising. Recorded rather than quietly amended.
