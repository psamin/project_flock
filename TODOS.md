# TODOS

Deferred work, with enough context to be picked up cold. Surfaced by
`/plan-eng-review` on 2026-08-16 while reviewing
[`docs/designs/3d-simulation-view.md`](docs/designs/3d-simulation-view.md).

---

## 1. Consolidate the duplicated websocket transport

**What:** `connect()` and its frame-dispatch logic exist twice — once in
`colony/client/app.js` (now `/2d`), once in the twin's renderer (now `/`).

**Why:** The eng review extracted the renderer-independent UI code
(`TICKER_TEXT`, `TICKER_CLASS`, `setText`, `updateHud`, `formatEvent`, the
commander console) into a shared module, but deliberately left `connect()`
duplicated. `connect()` calls `boot()` and `applyTileChanges()`, which are
renderer-specific, so sharing it requires inverting the dependency — passing the
renderer's handlers in rather than calling them directly.

**Pros:** One reconnect/backoff/error path instead of two. A transport bug fixed
once. Removes the last significant duplication between the two views.

**Cons:** Touches `app.js`, which was kept provably untouched during the
hackathon precisely because it was the recording fallback. That reason expires
after submission, but the refactor still needs both routes re-verified.

**Context:** The shape is a `connect({onSnapshot, onDiff, onClose})` module that
each renderer calls with its own handlers. Roughly 50 lines moving. Do it after
the video is recorded and the submission is in, not before.

**Depends on / blocked by:** Submission complete (Aug 18). Nothing technical.

---

## 2. Real browser tests for the renderers

**What:** A Playwright suite covering both `/` and `/2d` — canvas renders,
robot click opens the provenance panel, the WebGL-absent path shows the fallback
notice, the three fog states are visually distinct, mission restart does not leak.

**Why:** The eng review's coverage audit found 16 of 20 new codepaths are
JavaScript, against a repo with zero JS test infrastructure and 617 Python tests.
The gap was closed for the deadline with a local headless smoke check driven by
an out-of-repo binary — that catches black screens and thrown exceptions, but it
is not committable, not in CI, and not runnable by a teammate.

**Pros:** Brings the client up to the standard the rest of the repo already
holds. Makes the WebGL reversal defensible in writing — `app.js:13-19` rejected
PixiJS partly because it was "impossible to verify in headless QA", and this is
the answer to that objection.

**Cons:** Introduces npm and a `package.json` to a Python repo, plus a browser
download in CI. Meaningful setup cost and a second toolchain to maintain.

**Context:** Rejected during review as option 7C — on timing, not merit. The
decision would likely flip with a week instead of 44 hours. Start with the two
route smoke tests and the WebGL fallback path; those three carry most of the
value. The existing local smoke target is the spec to port.

**Depends on / blocked by:** Submission complete. Item 1 ideally lands first so
the tests target one transport path.

---

## 3. Line-of-sight perception instead of a square

**What:** Robot vision is a square of radius `r.vision`, not a line-of-sight
cone. `colony/sim/world.py:535-538` builds percepts as
`range(max(0, robot.x - radius), min(width, robot.x + radius + 1))` over both
axes, and `colony/client/app.js:138` mirrors that shape for the fog.

**Why:** A robot currently sees through walls. In a map whose whole premise is a
collapsed building with interior rooms and door tiles, that is a real fidelity
gap — and it is the single change that would most improve both the simulation's
realism and every visual built on top of it. The 3D renderer's sensor volumes
had to be derived from the square specifically so the cone would not draw a lie.

**Pros:** Makes the fog mean something sharper: "we have not seen behind that
wall" rather than "we have not been within six tiles." Makes sensor cones in the
3D view honest and much more striking, since they would visibly clip on
geometry. Strengthens the coordination story — two scouts covering a room from
different doors genuinely beats one.

**Cons:** This is a simulation change, not a rendering change. It alters what
robots know, which alters pathing, exploration, victim discovery timing, and
therefore the coordination-gain number in §4.7 that the video ends on. Every
`audit/` finding measured against the current model would need re-running.
Not a small change and not a safe one near a deadline.

**Context:** Implement as shadowcasting or Bresenham rays from the robot tile,
stopping at `wall` ground tiles (doors stay passable to sight). The `passable()`
helper at `colony/world/map_format.py:73` already distinguishes the relevant tile
types. Gate it behind a flag so the old behaviour stays reproducible and the
audit numbers remain comparable.

**Depends on / blocked by:** Submission complete. Requires re-running the
`audit/` experiments before the new model is treated as canonical.
