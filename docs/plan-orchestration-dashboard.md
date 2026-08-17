# Plan — orchestration dashboard, interventions, and making it agentic

Written 2026-08-17, against `main` as merged today. Scope is deliberately tight:
three days to submission, and the fastest way to lose is to start something that
cannot be finished.

## First: a lot of this already exists

Surveying before planning changed the plan substantially. Shipped on main:

| asked for | status |
|---|---|
| manual interventions / destruction | **built** — `sim/interventions.py`: `collapse`, `heavy rubble`, `fire`, placed in a Manhattan diamond, carried by a changefeed |
| intuitive controls for them | **built** — arm-a-kind then click-a-tile, `Escape` to disarm (`ui-shared.js`) |
| a dashboard | **partly** — HUD, §4.7 comparison, memory rail, commander console, `/sim3d` |
| a scrolling coordination log | **partly** — `#ticker` shows *events*, not *who told whom* |
| LLM-driven rather than rule-driven | **was broken, now fixed** — see below |

So the remaining work is smaller and sharper than "build a dashboard".

## What was actually missing, and is now fixed

**The cassette was never loaded.** `adapter_from_env` read `COLONY_BEDROCK_CASSETTE`
and nothing set it, so replay ran empty and all 34 plans in a full mission read
`source: rules`. The committed golden run had no consumer. Fixed in `e01ccbf`:
**15 of 36 decisions now come from Claude**, with zero network calls.

That single line is worth more to the "agentic use" criterion than any new panel,
because it is the difference between the AWS integration being real on the demo
path and being real only in a script nobody runs.

---

## The three things worth building, in order

### 1. The coordination feed — "who told whom" (highest value)

**The gap.** `#ticker` shows what happened (`claimed deliver_kit`,
`stabilized v3`). It does not show *why*, and the why is the entire product:
`m1` went to (12,10) **because `s2` wrote an observation there**. That causal
link exists in the database — `plans.based_on` holds the exact observation ids —
and `why_did_robot` already renders it on demand. Nothing streams it.

**Build.** A second rail, beside the ticker, that reads plan rows as they land
and renders each as a sentence naming both robots:

```
t312  m1 → claimed deliver_kit at 12,10
      because s2 reported a victim there (2 sightings, confidence 1.00)
      decided by claude
t318  l1 → claimed clear_debris at 11,10
      because the chain m1 is waiting on needs it cleared first
```

**Why this and not a generic log.** The robots do not message each other — there
is no channel to tap. Their only communication is rows, so a feed of rows *is*
the coordination feed, and rendering it as "A acted because B wrote" is the
thesis stated in the demo's own words. It also makes `decided by claude` visible
per line, which is where the agentic claim gets shown rather than asserted.

**Cost.** One endpoint (plans since tick N, joined to their `based_on`
observations) plus a panel. The join already exists in `console/questions.py`.

**Acceptance.** During a live mission the feed shows at least one cross-robot
line per rescue; each line names the author of the belief and the robot that
acted on it; `decided by claude` appears on the Bedrock-sourced ones.

### 2. Intervention → adaptation, made legible

**The gap.** An operator can already drop fire on a route. What they cannot see
is the fleet *noticing*. The adaptation happens — beliefs update, routes
re-cost, tasks get released — but it reads as robots wandering differently.

**Build.** When an intervention lands, mark the plans that cite it. A plan whose
`based_on` includes an observation written after the intervention, within its
radius, gets flagged in the feed:

```
t340  ⚡ operator dropped fire at 18,12
t344  s1 → reported hazard at 18,12
t346  l2 → released clear_debris at 19,12   ← re-planned around the fire
      because s1's hazard report re-priced the route
```

**Why.** This is the strongest thing the demo can show and it needs no new
mechanism — the causality is already in the data. It turns "I broke something
and things moved" into "I broke something, a scout saw it, a lifter changed its
mind, and here is the row that changed it."

**Cost.** Small: the feed from (1), plus tagging plans that follow an
intervention within its radius.

**Acceptance.** Dropping fire on a claimed route produces a visible
report → re-plan chain in the feed within ~10 ticks.

### 3. Dashboard consolidation — one screen, no scrolling

**The gap.** HUD, comparison, memory rail, ticker, console and operator panel
have each been added separately. There is no single arrangement that reads as an
operations console.

**Build.** Group into three columns: **world** (map), **fleet** (per-robot
status: role, task, lease remaining, last decision + source), **memory** (the
rail, the console, the coordination feed). Nothing new is computed — the
per-robot column reads `robots` and `_held_by`, both already present.

**Cost.** Layout only. Deliberately last: it is the item most likely to eat a
day and least likely to change what a judge concludes.

**Acceptance.** A stranger can name what every robot is doing, and why, without
clicking.

---

## Deliberately NOT doing

- **A second LLM surface.** Free-form NL → SQL in the console is the one part
  that can fail unrecoverably on camera; §5.4 chose canned queries for that
  reason and the audit agreed. What is worth fixing is the *claim* — §6.2 still
  describes the console as an AI answering questions, and it is five audited
  queries. Fix the wording, not the console.
- **A "dreaming"/consolidation pipeline** in the style of the Mnemosyne
  submission. Semantic memory already lands via `remember_lesson` after a
  mission; a second distillation layer is a day of work that changes no
  measured number.
- **1024-dim embeddings.** Ours are 512 to match `VECTOR(512)`. Changing it is a
  migration for a number nobody grades.
- **Procedural memory as a fifth tier.** The PRD names four and the schema has
  four. Adding a fifth to match someone else's architecture diagram is scope
  creep with a strong smell of chasing.

## The competitive read

The nearest published submission is a memory *service* — four tiers, vector
search, MCP, a monitoring console. Ours overlaps on all of that. What it does
not have is **many agents coordinating through the memory in real time, with a
measured ablation proving the coordination matters** (X1: 0.994 vs 0.000; X10:
+0.313 [+0.234, +0.393] across 40 generated scenarios).

So the differentiator is not the memory design — it is that the memory is
*load-bearing for a live multi-agent system*, and we can prove it. Every item
above serves making that visible. Nothing above adds a memory tier.

## Order of work

1. ✅ `e01ccbf` — load the cassette, so decisions are Claude's
2. Coordination feed (item 1)
3. Intervention → adaptation tagging (item 2)
4. Dashboard layout (item 3), only if 2 and 3 are done and green
