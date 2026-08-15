# AUDIT E — demo-only code paths

**Verdict:** CLEAN, with two qualifications.

This is the audit I expected to find the most damage in, and it is the one that
came back cleanest. Report as a negative result: **there is no demo-mode
branching, no artificial pacing, no pre-recorded event log being replayed as if
simulated, and no UI element displaying a prop instead of state.**

---

## E.1 — Grep for demo-aware behavior

| pattern | hits | verdict |
|---|---|---|
| `demo`, `presentation`, `judge`, `showcase`, `pitch`, `scripted` | 0 behavioural | Only in docstrings describing *why* code exists |
| `canned` | 12 | All in `console/` — the five fixed questions, honestly named (D-1) |
| `replay` | `bedrock/adapter.py` | A real mode, but see the qualification below |
| `MOCK`, `STUB`, `placeholder`, `hardcoded`, `for_now`, `temporarily` | **0** | — |
| `TODO`, `FIXME`, `XXX`, `HACK` | 0 in source | Only `TODO.md` references |

**Environment variables that change behavior**, all enumerated:
`COLONY_DSN`, `COLONY_MEMORY`, `COLONY_MAP`, `COLONY_BEDROCK_MODE`,
`COLONY_BEDROCK_CASSETTE`, `AWS_REGION`, `CRDB_CLUSTER_ID`, `COLONY_CONSOLE_DSN`.
Each is documented in `docs/setup-testing.md`. None gates a "look better when
watched" path.

---

## E.2 — Artificial delay or easing

**None found.** The tick loop is a plain `for _ in range(limit)` (`mission.py:131`)
with no sleeps. `client/app.js` interpolates between ticks for smoothness — that
is rendering, not event staging, and is exactly what §4.8 specifies.

The one `Math.random()` (`app.js:158`) is screen-shake on the aftershock. It
moves pixels, not state (C-6).

---

## E.3 — Pre-recorded event logs

| # | Severity | File:line | What | Why it's a problem | Fix |
|---|---|---|---|---|---|
| E-1 | MINOR | `bedrock/adapter.py:114-128` | Cassette replay is a genuine record/replay mechanism | This is **not** a faked mission. The cassette stores only *model responses*; the simulation still runs live and every event is produced by the sim. §5.4 explicitly calls for recorded-replay for demo reliability | Keep. But see D-4: with no cassette it silently synthesizes instead of failing |

No fixture run, no golden output, no canned event log is replayed in place of a
simulation. Every `events` row in the database came from a tick that actually
executed.

---

## E.4 — UI elements displaying props rather than state

**Checked and clean, with one exception already filed.** `client/app.js:431-441`
formats server-supplied values only; it computes nothing. The defect is upstream
in the server's dict merge (C-1), not in the renderer.

---

## E.5 — Cold-start run

Performed genuinely cold: Docker daemon started from stopped, `make dev` created
the database and applied `schema/v1_1.sql` fresh, no caches, no fixtures.

| step | result |
|---|---|
| Schema applies to an empty database | **PASS** — all 8 tables |
| Full suite against the fresh cluster | **PASS** — 617 passed, 12 skipped, 75s |
| Full mission, coordinated, seed 0 | **PASS** — 9/9 stabilized, finished tick 339 |
| Rows written | 23 observations, 34 plans, 27 tasks, 9 victims |
| Console `unreached_victims` | **PASS** — 9 rows, substantive answer |
| Console `who_holds_what` | **PASS** — 1 row |
| Console `why_did_robot` | **PASS** — 5 rows, resolves `based_on` to real observations |
| Console `aftershock_response` | **PASS** — no error |
| Console `what_do_we_know` | **EMPTY PANEL** — "nothing has been observed near there" (D-2) |
| Sim server on a port | **PASS** — HTTP 200, `<title>Colony — Aftershock</title>` |

**One empty panel, one cause, one-line fix** (`questions.py:294` defaults x/y to
the origin). Nothing renders as `null`, `undefined`, `NaN`, or a dash.

---

## Qualifications

1. **The `replay` default is the risk, not the mechanism.** `COLONY_BEDROCK_MODE`
   defaults to `REPLAY` (`adapter.py:115`), and with no cassette every embedding
   is locally synthesized (D-4). That is a demo-only code path in effect if not
   in intent: the demo machine produces vectors AWS never saw. It belongs in this
   audit as much as in AUDIT D.

2. **Single-node vs three-node** (A-12). The live demo runs against
   `localhost:26257` single-node; the node-kill segment runs on a separate rig.
   Disclosed by design in §6.5, but it is a difference between what is on screen
   and what the resilience claim rests on.

---

## Checked and clean
- No demo/judge/presentation branching anywhere in source.
- No artificial sleeps or staging delays.
- No pre-recorded mission replayed as live.
- No `MOCK`/`STUB`/`placeholder`/`hardcoded` markers in source.
- Cold start works end to end: fresh schema → full suite → full mission → console answers.
- Every displayed value traces to server state; the renderer computes nothing.
