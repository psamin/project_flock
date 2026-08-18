# Judging readiness — criteria and submission checklist

Written against the five published criteria and the "What to Submit" list.
Every claim cites where it can be checked. Where we fall short, it says so
rather than arguing the point.

---

## Part 1 — the five criteria

### 1. Agentic Memory Design — **strong, with one real gap**

> *Does CockroachDB play a meaningful, production-grade role as the agent's
> memory layer? More than toy queries — state, embeddings, context, or
> transactional data at real scale?*

**What holds up.** The schema is the argument, not a place to put rows. Four
memory systems as named tables, and all four are populated in a live mission
(verified on a running server, not asserted):

| Memory | Tables | Live counts |
|---|---|---|
| Working | `robots`, `tasks`, `victims`, `hazards` | tasks 26 · victims 8 |
| Episodic | `observations` (`VECTOR(512)`) | 9 |
| Provenance | `plans`, `events` | 21 · 334 |
| Semantic | `mission_memories` (`VECTOR(512)`) | 3 |

None of these are toy queries:

- **The claim is one statement.** `UPDATE ... WHERE (status='open' OR
  lease_expires_at < now()) RETURNING id`. Under `SERIALIZABLE` exactly one
  robot wins, and a dead robot's work frees itself with nobody on the recovery
  path.
- **The reconcile gate is a vector search inside the insert transaction.**
  Match → merge and bump a sighting count; miss → insert. Two scouts seeing one
  victim produce one victim.
- **`complete_task` unblocks dependents in the same transaction**, so a handoff
  cannot half-happen.
- **`plans.based_on` stores the rows that caused each decision**, which is what
  makes "why did that robot do that" a join instead of a story.

**The gap: "at real scale."** Our largest dataset before this review was ~1000
observations. That is demo scale. Scale evidence now lives in
[`docs/scale.md`](scale.md) — see Part 3.

### 2. Technical Implementation — **two of the three named tools**

> *Integration with CockroachDB tools (distributed vector index, MCP Server,
> ccloud CLI) — quality engineering, used correctly and safely.*

**Distributed vector indexing — real, and honest about where it isn't.**
Two uses that scope in opposite directions:

| | scope | plan |
|---|---|---|
| Tactical recall | across every mission | **`vector search`** ✅ |
| Reconcile gate | within one mission | **`FULL SCAN`**, deliberately |

The gate constrains `kind` and a 5-tile box beside the vector order-by; neither
is a prefix column, so v26.2 declines an approximate top-k it would then have to
filter. Measured at 1047 rows with four EXPLAINs isolating one clause each, so
it is the query shape and not demo scale. **We keep the scan** — an approximate
search that misses a duplicate sends two robots to one victim, which is the bug
the gate exists to prevent. `test_the_reconcile_gate_query_uses_the_index` is a
**strict xfail** holding that line.

Tests assert `EXPLAIN` **plans**, not results, because a wrong vector query
still returns plausible rows.

**Used safely.** `retry_on_serialization_failure` around SQLSTATE 40001;
claiming is exercised under real contention with `ThreadPoolExecutor` races
(`tests/test_claiming.py`); per-robot SQL roles with least privilege, including
`INSERT` **without** `SELECT` on provenance tables so a robot records why it
acted and cannot read the log back (`infra/credentials.py`).

**MCP Server — posture real, transport partial.** `commander` holds `SELECT`
and nothing else, asserted on the Cloud cluster by `credentials.py verify`, so
read-only is a property of the **grant** rather than a setting. `infra/mcp.py
config` emits the client snippet and `check` asserts the posture. **But the
in-app console executes its six audited queries directly as that role, not
through the managed endpoint.** We say so rather than implying otherwise.

**ccloud CLI — not used. Zero files.** Named in this criterion and absent from
the repo. This is the clearest single gap in the submission; see Part 4.

**Agent Skills — not used.** Planned in the PRD, never run.

### 3. Real-World Impact — **meaningful, under-argued**

The use case is real: multi-robot disaster response where the coordinating
insight — a medic acting on a victim it never saw, because a scout wrote the
belief and a lifter's transaction unblocked the task — is the thing that saves
people. Measured effect, 40 generated scenarios, paired:

**+31.3 points rescue rate** (95% CI +23.4 to +39.3), mean victims lost
**2.40 → 0.95**. Scope condition stated up front: the effect requires scenarios
where reaching a victim needs a handoff.

**What is under-argued:** the generalisation. The claim that survives outside
robotics is *coordination as a property of the data model rather than of a
message bus* — which applies to any fleet of agents that must not lose work when
one dies. The writeup gestures at this; it should state it plainly.

### 4. Production Readiness — **strong on resilience and access, thin on observability**

**Resilience.** Recovery is the *absence* of a recovery path: no sweeper, no
watchdog, no supervisor — the lease predicate is the whole mechanism. Node kill:
**zero tasks lost, no stall, rehearsed 5/5** (`audit/x5-node-kill.md`). All
expiry math uses database `now()`, so clock skew cannot manufacture a takeover.

**Access control.** Per-robot roles, least privilege, append-only provenance by
grant. Commander is `SELECT`-only. The container runs non-root (uid 10001) and
the deployed demo carries **no AWS credential at all** — replay mode plus the
committed cassette, so there is nothing on a month-long public URL to leak.

**Honest limits, stated in `docs/deploy.md` rather than discovered:** one
container / one mission (`--workers 1` is deliberate — mission state is
in-process); every visitor shares one mission; no TLS on the free-tier path.

**Thin:** no metrics endpoint or structured logging beyond `/health` and the
event log. `/health` does report tick, memory backend and Bedrock's effective
mode, which is the right minimum, but there is no scrape target.

### 5. Creativity & Originality — **the strongest criterion**

- **The schema is the thesis.** Memory taxonomy as named tables, so the design
  argument is readable as DDL.
- **No robot-to-robot channel, enforced.** Coordination is what falls out of
  shared memory. X1: with cross-agent reads disabled and every write still
  happening, the fleet rescues **0 of 9**.
- **Recovery as an absence.** The lease-in-the-`WHERE`-clause deletes three
  components most systems would build.
- **Lessons are tactics, never places.** The prompt forbids coordinates
  outright, and the digest is built without them, because a fleet recalling
  victim positions has been handed the answer. This is the sharpest insight in
  the project: what *transfers* across missions is conditions, not coordinates.
- **Audit discipline.** Three findings retracted in-repo rather than quietly
  amended, and a known gap kept as a strict xfail instead of a note.

---

## Part 2 — submission checklist

| Requirement | State |
|---|---|
| Public repo URL | ✅ `PUBLIC` |
| README, deps, setup/run instructions | ✅ README quickstart + `docs/setup-testing.md` |
| Open source licence, visible in About | ✅ GitHub detects `apache-2.0` |
| **Functional demo app URL** | ❌ **not deployed** — `homepageUrl` is empty |
| **Video < 3 min, public on YouTube/Vimeo** | ❌ **not recorded** — script ready at `docs/video-script.md` |
| CockroachDB tools + what the agent did | ✅ `docs/devpost.md` |
| AWS services + how | ✅ `docs/devpost.md` |
| *Optional:* architectural diagram | ⚠️ ASCII in README; no image |
| *Optional:* CockroachDB feedback | ✅ `docs/devpost.md` |

Two hard blockers, both needing Praneeth: **deploy** (Dockerfile built and
smoke-tested; ~20 min on free-tier EC2) and **record the video**.

After deploying, do not skip: `gh repo edit --homepage <url>` — a judge landing
on the repo currently has nothing to click.

---

## Part 3 — scale evidence

See [`docs/scale.md`](scale.md).

---

## Part 4 — what would most improve the score

Ranked by value per hour, honestly.

1. **Deploy + record.** Two of eight checklist rows are unmet and they are the
   two judges cannot work around. Everything else is polish by comparison.
2. **ccloud CLI.** Criterion 2 names three tools; we ship two. Provisioning the
   Cloud cluster and per-robot SQL users through `ccloud` in `infra/` is a
   contained script and turns a stated absence into a third tool. Only worth
   doing *for real* — a committed script nobody ran is worse than the honest gap.
3. **State the generalisation.** One paragraph: this is coordination as a
   property of the data model, applicable to any agent fleet. Criterion 3 is
   scored on impact, and the robotics framing undersells the idea.
4. **An image architecture diagram.** Optional, cheap, and the ASCII one does
   not survive being screenshotted into a judging deck.
5. **A metrics endpoint.** Smallest real gap in criterion 4.
