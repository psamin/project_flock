# Gap analysis — what is missing, not what could be added

Written 2026-08-17, one day before the Aug 18 deadline. Found by running edge
cases against a live server, not by reading code.

The bar for inclusion: **necessary and absent**. Things that would be nice, or
that would make the architecture prettier, are not here. Two candidates were
rejected while writing this and are listed at the bottom with reasons.

---

## Findings, by whether they can lose the submission

### P0 — the submission fails or misleads without these

#### G-1. No deploy exists. At all.

```
find . -iname "Dockerfile*" -o -iname "*.tf" -o -iname "task-definition*" ...
→ (nothing)
gh repo view --json homepageUrl → ""
```

§6.1 requires the demo URL and repo **live, working and free to access through
Sept 15** — a month past submission, not just on the day. There is no
Dockerfile, no ECS task definition, no S3/CloudFront, and the repo's About has
no link.

This is the single largest missing thing and the only one that cannot be
recovered after the deadline. Everything else on this list degrades the
submission; this one can disqualify it at Stage One ("meaningfully integrated,
not just initialized" — AWS is currently Bedrock only, and Bedrock only in
replay).

**Smallest thing that satisfies it:** the frontend is static and the backend is
one Python process. S3 + CloudFront for the client, one small always-on
instance for the server. If the AWS account is <12 months old, `t3.micro` is
free-tier for 750 h/month, which covers the judging window at $0.

#### G-2. The licence contradicts itself — still

`README.md:181` says **"MIT — see LICENSE"**. `LICENSE` is **Apache 2.0**, and
GitHub's About shows `apache-2.0`.

Flagged in AUDIT A on Aug 15 and never fixed. Both are OSI-approved so Stage One
survives, but a judge who clicks through sees the writeup contradicting the
repo on the first factual claim they can check. It costs one line to fix and
nothing to leave, which is exactly why it keeps not being done.

#### G-3. A sabotaged run is recorded as if it were a fair comparison

Reproduced live: killed all four robots, restarted into baseline, and the
previous run was recorded as

```
coordinated: rescue=0.444  finished=False  interference-fields=NONE
```

`0.444` is the baseline's number. Nothing in the recorded run says an operator
killed the fleet or dropped fire on it. The ON/OFF panel currently refuses to
compare unfinished runs — which is what saves this today — but as soon as both
runs finish, a tampered-with coordinated run is compared against a clean
baseline and the headline reads **"coordination gain 0%"**.

Interventions are a headline feature. The one thing they must not do is quietly
poison the measurement the whole project rests on.

**Fix:** record `interventions: n, robots_killed: n` on the run, and have the
comparison panel say "this run was interfered with" instead of publishing a
number. Small, and it protects the only number that matters.

### P1 — a judge will notice within a minute of trying it

#### G-4. Killing the fleet produces 143 seconds of silence

Reproduced: four kills, then

```
tick 127 → 147 over 5s   (still ticking)
down: 4/4
```

The tick loop only exits on `world.finished`, and with no robots that is when
the last victim's deadline passes — **tick 700**. From a wipeout at 127 that is
**573 ticks ≈ 143 seconds** of a running mission where nothing can happen and
nothing on screen says so.

A judge who clicks the kill button four times to see what happens gets two and a
half minutes of dead air and no explanation. The fix is a terminal state: when
every robot is down, say so, stop the clock, and offer restart.

#### G-5. Two of the four "memory is real" surfaces go dark with no cluster

Simulated a judge cloning and running with `COLONY_MEMORY=fake`:

| surface | no cluster |
|---|---|
| coordination feed | ✅ works |
| fleet panel | ✅ works |
| Bedrock decisions | ✅ works — 76 cassette entries, Claude deciding |
| **commander console** | ❌ `available: false` |
| **memory rail** | ❌ `available: false` |

Honest — both genuinely need SQL — and the README's quickstart does say
`make dev` first. But FR-10's console is the headline memory feature and the
rail is the "CockroachDB is doing work" proof, and both are exactly what a judge
poking around without reading the README will find missing.

Not a code fix. It is a **README and video ordering** problem: `make dev` has to
be impossible to skip, and the video must show the console working.

#### G-6. The A/B comparison costs ~2.5 minutes of a 3-minute video

Getting the §4.7 comparison requires running coordinated to completion (~312
ticks ≈ 78 s at 4 Hz), toggling, and running baseline to completion (~560 ticks
≈ 140 s, because baseline fails and runs longer). That is **over three and a
half minutes** for the money shot, in a video that must be under three.

T-20 (two sims side by side, one seed) exists in the plan for this reason and is
still unbuilt. The cheap alternative is to precompute both runs and have the
panel show the finished comparison immediately — the numbers are already
recorded per mode in `last_runs`.

### P2 — real, known, and safely deferrable past submission

- **T-11** — baseline still reads the shared task table (`worker.py`,
  `scout.py`), so "coordination OFF" is worth 0.444 rather than 0.000. Needs
  `ALTER TABLE tasks ADD COLUMN created_by`, a §5.2 contract surface. Fixing it
  makes the demo *more* dramatic (44→98 becomes 0→98), which is why it is
  tempting the day before a deadline, and why it should not be done then.
- **T-12a** — blind pathing needs a shared learned-terrain memory to avoid the
  de-duplication regression measured in `audit/experiments.md`.
- **T-12b** — episodic memory records sightings, never outcomes. No belief row
  ever says a victim was stabilized.
- **T-10b** — a cassette *miss* still falls through to `_offline_embedding`
  silently. The common case is fixed (the cassette now loads); the miss is not.
- **`task_lease(task_id)`** — the fleet panel's lease countdown is estimated
  from the kill time because the SDK cannot read a live lease. Honest today
  (`lease_approx: true`, tilde in the UI), but a five-line read would make it
  exact.

### P0 — non-code, and nobody has started them

- **Video.** Not recorded. Judges may score from the video and description
  alone, which makes this worth more than any remaining code.
- **Devpost writeup.** Not drafted. Needs the tools-used section, the AI-assisted
  development disclosure, and the CRDB feedback §6.2 asks for.
- **Cold-start rehearsal** (T-43) — three run-throughs from a dropped database.
  Never done end to end since the audit.

---

## Deliberately rejected

Two things looked like gaps and are not:

**A fifth memory tier / "procedural memory".** The PRD names four, the schema
has four, and all four are now real. Adding a fifth to match another
submission's architecture diagram is chasing, not scope.

**Free-form NL→SQL in the console.** §5.4 chose canned queries for demo
reliability and the audit agreed. The claim was fixed instead (`6e10a00`). A
model improvising SQL live is the one failure nobody recovers from on camera.

---

## What was checked and is genuinely fine

Reporting negative results, because "we looked" is worth as much as "we found":

- **Intervention validation** — every malformed input returns a structured,
  readable error: out of bounds names the map size, unknown kind lists the known
  ones, bad radius states the range. No crashes, no 500s.
- **Console errors** — an unknown question lists the valid set; a bad parameter
  type surfaces the cluster's own message rather than a bare 400.
- **Restart hygiene** — restarting clears killed robots and disruptions, and
  re-snapshots every viewer.
- **Concurrent viewers** — two clients see the same tick and the same fleet.
- **Both renderers serve** — `/` (twin), `/2d` (Canvas 2D) and the `/sim3d` alias all 200.
- **Determinism, node kill, ablation** — X1, X2, X5, X7, X9, X10 all pass, X5
  re-proven today at 5/5 against current code.
