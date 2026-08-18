# Devpost submission copy

Paste-ready. Every number here is measured and traceable to a file in this repo;
nothing is rounded in our favour, and the two places the system does *less* than
you would assume are stated rather than left to be discovered.

---

## Tagline

**A robot fleet with no radio. The database is the coordination.**

---

## Elevator pitch (Devpost's short field, ~200 chars)

> Six robots run a disaster rescue as one team with no channel between them —
> they coordinate entirely through shared memory in CockroachDB. Kill a robot,
> a route, or a database node: the mission continues.

---

## Inspiration

Robots are already autonomous. They are not yet *teammates*.

A drone that finds a survivor and a lifter that could reach them are, today,
two separate autonomies with a human in the middle. The usual fix is a message
bus — but a bus tells you what someone said, not what the team knows, and when
the sender dies its messages die with it. We wanted to see how far coordination
could go if the *only* shared thing was a database.

So we imposed a hard rule and never broke it: **no robot may talk to another
robot.** Every robot writes what it sees and reads what everyone else wrote.
If that produces teamwork, the teamwork is a property of the data model.

## What it does

Colony runs a magnitude-7.2 aftershock scenario. Nine people are trapped under
rubble; six heterogeneous robots — scouts, lifters, medics — go in.

- **Scouts** explore and write beliefs (victims, hazards, debris).
- **Lifters** clear debris that blocks a route.
- **Medics** deliver kits — but only once a lifter's work has unblocked the path.

That last dependency is the point: a medic acts on a victim it never saw,
because a scout wrote the belief and a lifter's `complete_task` transaction
unblocked the task. No robot ever addressed another one.

You can break it live, three ways, and watch it recover:

| Break | What happens |
|---|---|
| **Kill a robot** mid-task | Its lease stops being renewed. 15 s later the row is claimable and another robot takes the job. Nothing reassigns it. |
| **Drop fire** on a claimed route | A scout observes it, writes it, and planners re-route around it. |
| **Kill a database node** | The fleet does not pause. Zero tasks lost. |

A commander console answers questions about the running mission in read-only
SQL — including *"why did robot m1 do that?"*, which is a join, not a story.

## Why this generalises

Robots are the setting, not the claim. Strip the rubble away and Colony is a
pattern for **any fleet of agents that must not lose work when one of them
dies**: ownership is a lease rather than an assignment, so a worker that
disappears frees its own work with nobody watching; coordination is a property
of the data model rather than of a message bus, so agents that never address
each other still hand off; and every decision stores the rows that caused it,
so "why did that happen" survives the process that decided it.

That shape is the same whether the agents are lifters and medics, a bank of
document-processing workers, or an LLM agent pool where one call finds
something the next needs. The parts most systems build for this — a scheduler,
a heartbeat watchdog, a reassignment sweeper, a message broker — are the parts
we deleted, and the database does their job under `SERIALIZABLE` without a
coordinator. The disaster scenario is what makes the failure modes visible and
the stakes legible; it is not what makes the idea useful.

## How we built it

**The schema is the thesis.** Four memory systems as named tables, because the
question we care about is what a fleet should *remember*, not how it should
message:

| Memory | Tables | Holds |
|---|---|---|
| Working | `robots`, `tasks`, `victims`, `hazards` | what is true now |
| Episodic | `observations` (`VECTOR(512)`) | what we experienced |
| Provenance | `plans`, `events` | why we acted |
| Semantic | `mission_memories` (`VECTOR(512)`) | what we learned across missions |

Three design decisions carried most of the weight.

**Ownership is a lease, not an assignment.** A claim is one statement:

```sql
UPDATE tasks SET status='claimed', claimed_by=$robot,
                 lease_expires_at = now() + INTERVAL '15 seconds'
WHERE id=$task
  AND (status='open'
       OR (status IN ('claimed','in_progress') AND lease_expires_at < now()))
RETURNING id;
```

The expiry check lives *inside* the claim's `WHERE`, so under `SERIALIZABLE`
exactly one robot wins and a dead robot's work frees itself. There is no sweep,
no watchdog, no supervisor. **Recovery is the absence of a recovery path** —
which is why killing a robot on camera is boring, and boring is the claim. All
expiry math uses database `now()`, never a robot's clock, so skew cannot
manufacture a false takeover.

**Reconcile before broadcast.** A new observation is cosine-searched against
existing beliefs within 5 tiles *in the same transaction that would insert it*.
Match → merge and bump a sighting count. Miss → insert. Two scouts seeing one
victim produce one victim, not two, and the fleet is never dispatched twice.

**Every decision keeps its sources.** `plans.based_on` stores the observation
rows that were in the prompt digest. That is what makes the console's "why"
answerable by a join.

## Built with — CockroachDB

**Distributed vector indexing** (required tool #2) carries weight in two places
that pull in opposite directions, and they behave differently:

| | scope | plan |
|---|---|---|
| **Tactical recall** — "what do we know about a moment like this?" | across *every* mission and map | **`vector search`** ✅ |
| **Reconcile gate** — "is this the victim we already know about?" | within one mission | **`FULL SCAN`, deliberately** |

Tactical recall is where the index is load-bearing. When a mission ends, Claude
reads its figures and derives what would transfer — *"when a robot has cleared
debris to reach a victim and a medic is not yet present, bring the medic rather
than continuing to explore"* — and each lesson is embedded into
`mission_memories`. At the next decision boundary a robot describes what it is
facing, cosine search returns tactics learned in situations like it, and those
ride into the planning prompt. It has **no prefix column at all**, because any
scope would partition exactly the knowledge it exists to generalise — so it
ranks every lesson from every past mission, which is what makes retrieval real
rather than decorative.

Lessons are deliberately **not** where the victims were. The same disaster does
not recur on the same tiles, so a coordinate transfers to nothing — and a fleet
that recalls victim positions has been handed the answer. The lesson prompt
forbids coordinates outright and the digest it reads is built without them.

**The reconcile gate does not use the index, and we would rather say so than
have a judge discover it.** It constrains `kind` and a 5-tile position box
alongside the vector order-by; neither is a prefix column, and v26.2 declines to
serve an approximate top-k it would then have to filter. We measured this at
1047 rows with four EXPLAINs isolating one clause at a time, so it is a property
of the query shape, not of demo scale. **We are keeping the scan.** An
approximate search that misses a duplicate sends two robots to one victim, which
is the exact bug the gate exists to prevent; exactness matters more here than
speed at this size. The test asserting otherwise is kept as a **strict xfail**
so the gap stays visible and cannot silently regress into a pass.

That is also why our tests assert `EXPLAIN` plans rather than results: get a
vector query wrong and it still returns perfectly plausible rows.

**Serializable isolation** is what makes decentralized claiming safe. There is
no allocator: robots rank open work and claim it themselves, and the database
adjudicates. Dependency unblocking happens inside `complete_task`'s
transaction, so a handoff cannot half-happen.

**Managed MCP Server** (required tool #1) is the console's access-control story.
`commander` is granted `SELECT` and nothing else — asserted on the Cloud cluster
by `infra/credentials.py verify`, so read-only is a **property of the grant**
rather than a setting someone could flip. `infra/mcp.py config` emits the client
snippet and `infra/mcp.py check` asserts that posture before it is wired.

*Stated plainly:* the in-app console executes the same six audited queries
directly as that same least-privilege `commander` role. We chose fixed, audited
SQL over free-form NL→SQL deliberately — a model improvising SQL live is the one
component that can fail in a way nobody recovers from on camera, and a judge can
read our statement, run it, and check the answer. The console is six queries,
not an AI, and we do not describe it as one.

**Survival.** Three nodes, kill one mid-rescue: **zero tasks lost, no stall,
rehearsed 5/5** (`audit/x5-node-kill.md`).

## Built with — AWS

**Amazon Bedrock — Claude Haiku 4.5** decides at plan boundaries via a
cross-region inference profile. The prompt is that robot's *local slice* of
memory plus recalled tactics — never global state, because a robot that can read
everything is not solving the problem we set.

**Amazon Bedrock — Titan Text Embeddings V2** at 512 dimensions embeds every
observation and every learned lesson. Both vector paths above are Titan vectors.

Rules are the **floor, not the fallback**: a mission runs identically with no
AWS credentials at all, and `plans.chosen.source` records which decided each
plan — so *"the LLM is driving this"* is checkable in SQL rather than asserted.
In a full mission, 15 of 36 decisions are Bedrock's. The adapter has live /
record / replay modes with committed cassettes, which is why the public demo
shows real Claude output **with no credential on the box** — nothing to leak on
a URL that must stay up for a month.

## Results — what we actually measured

We did not want a demo that works once on a map we tuned. So we generated 40
random disasters and ran each **paired**, with coordination on and off:

| | coordination off | on |
|---|---|---|
| Rescue rate | — | **+31.3 points** (95% CI **+23.4 to +39.3**, n=40 paired) |
| Mean victims lost | 2.4 | **0.95** |
| The scripted demo map | 4/9 rescued, **5 die** | **9/9, nobody dies** |

**The scope condition, stated up front:** this effect requires scenarios where
reaching a victim needs a handoff. On maps where every victim is reachable by a
single robot alone, shared memory makes no measurable difference — we tested
that too, and it is the honest boundary of the claim.

Everything is in `audit/`, including three findings we **retracted** after
measuring more carefully — a coordination "cleanup" that turned out to regress
two behaviours independently, and an ablation arm we scored twice before
realising we were measuring a broken stand-in. We kept the retractions in the
repo rather than quietly amending them.

## Challenges

**A test that passed while the real query full-scanned.** Our EXPLAIN test
exercised a *tidier* query than `find_similar` actually runs. It passed for
weeks. The fix was not to make the test green — it was to write the real query's
test, watch it fail, and mark it a strict xfail with the trade-off documented.

**Non-determinism that made every statistic meaningless.** A tiebreak sorted on
`str(uuid)`, so identical seeds diverged and confidence intervals described
noise. One line. Every number above post-dates that fix.

**Metrics that overwrote themselves.** Simulator state was clobbering store
metrics on merge, meaning "we do not fake stats" would not have survived a
judge. Merge order, one line — but we only found it by auditing rather than by
watching the demo work.

The pattern: everything that bit us returned *plausible* output. That is why the
discipline in this repo is asserting plans over results, retracting in public,
and never making a failing test pass by weakening it.

## What's next

- Flip the reconcile gate to an index-served plan *without* trading exactness —
  likely a prefix redesign, not a filter move.
- Semantic memory currently learns from our own runs; the interesting version
  learns across fleets.
- `victims` is written and not yet read by any decision — an honest loose end.

## Built with (tags)

`cockroachdb` · `distributed-sql` · `vector-search` · `serializable-isolation`
· `aws` · `amazon-bedrock` · `claude` · `titan-embeddings` · `python` ·
`fastapi` · `websockets` · `docker` · `canvas` · `webgl` · `multi-agent-systems`

---

## Required disclosures

**AI-assisted development.** This project was built with substantial AI
assistance — Claude Code was used throughout for implementation, test authoring,
and the audit process that produced `audit/`. All architectural decisions, the
schema design, the experimental methodology, and every retraction recorded in
`audit/` were human-directed. The AI-assisted portions are the ordinary ones:
writing code to a specification, generating test cases, and running measurements
we designed.

**Third-party components.** CockroachDB v26.2.5 (CCL distribution, under Cockroach
Labs' current licensing), FastAPI (MIT), uvicorn (BSD), psycopg 3 (LGPL), and
boto3 (Apache 2.0) — boto3 is an *optional* extra, lazily imported, which is why
the demo runs with no AWS SDK installed at all. The CockroachDB Agent Skills
repo (Apache 2.0) is fetched at a pinned commit rather than vendored; see
[ASSETS.md](../ASSETS.md). Our own code is Apache 2.0. No third-party
trademarks, logos, or copyrighted music appear in the demo video.

**Tools listed in our plan that we did *not* ship.** Our internal plan named the
`ccloud` CLI as an additional integration. It was not delivered, and we are not
claiming it. Nothing in this repository shells out to `ccloud`; the SQL roles it
was going to create are created in SQL by `infra/credentials.py`.

**What "we use these tools" means here, precisely.** Two CockroachDB tools are
load-bearing in the running demo and a third is load-bearing at development
time, and those are different claims:

- **Distributed vector indexing** — `mission_memories.mm_situation_idx` serves
  tactical recall on every plan boundary, and `EXPLAIN` says `vector search`.
  The console will show you that plan live. The *other* vector query, the
  reconcile gate on `observations`, is a deliberate `FULL SCAN` — see the README
  for why exactness beats an approximate top-k there. We would rather state that
  than have a judge run `EXPLAIN` and think they caught us.
- **Managed MCP Server** — the commander console's free-form tier reads the live
  cluster through it. Not a config snippet we printed: `console/mcp_client.py`
  is an OAuth 2.1 client against `cockroachlabs.cloud/mcp`, and every answer in
  that tier arrives via `tools/call`. It is also wired into our editors, which
  is where we first found that the endpoint connects as `managed-mcp` rather
  than as the `commander` role our config claimed.
- **Agent Skills repo** — the same tier routes on the 34 skills' descriptions
  and loads a body when one matches. The console prints which skill it chose;
  asking it to audit privileges loads `hardening-user-privileges`, and asking
  why the cluster is slow loads `triaging-live-sql-activity`. That is
  progressive disclosure as the spec intends, not a skill pasted into a prompt.

The honest caveat on the third: the agent consults a skill when one is relevant
and does not when none is, so a question about which robots are stuck loads
nothing. We think that is the tool working rather than the tool idling, but it
means "used on every question" would be false.

---

## Feedback for CockroachDB

Requested in the submission form. Written from things that actually cost us
time, not from the docs.

**Vector index + non-prefix filters is the sharp edge.** The behaviour is
correct — declining an approximate top-k that would then be filtered is the safe
choice — but it is *silent*. The query returns plausible rows and the only
signal is `EXPLAIN`. We shipped a passing test against a query shape we weren't
running. A planner hint, or an opt-in warning like *"vector index not used:
non-prefix filter on `kind`"*, would have saved us the most expensive
misconception in this build.

**`CREATE VECTOR INDEX` prefix semantics deserve a worked counter-example.** The
docs explain that prefix columns must be constrained to exact values. What is
missing is the case that bit us: an *additional* non-prefix predicate disabling
the index entirely, rather than being applied after the top-k. A three-line
"this plan is a full scan, and here is why" example would carry it.

**Serializable retry was a genuine non-event, and that is worth saying.** We
wrote one `retry_on_serialization_failure` decorator around 40001 and never
thought about contention again, with six agents claiming against shared rows at
4 Hz. Coming from databases where this is a project, it was a day-one
correctness win.

**Lease-in-the-`WHERE`-clause deserves to be a documented pattern.** Our whole
fault-tolerance story is one `UPDATE ... WHERE lease_expires_at < now()
RETURNING id`. We arrived at it ourselves; it is the single highest-leverage
thing CockroachDB let us delete (a watchdog, a sweeper, and a supervisor). We
would have adopted it on day one from a docs page called *"task queues without a
coordinator"*.

**v26.2 restricting `crdb_internal` and `system` broke our node-health check**
with a message that reads like a permissions bug rather than a deliberate
policy. `cockroach node status` was the answer; a pointer in the error text
would have shortened that from an hour to a minute.
