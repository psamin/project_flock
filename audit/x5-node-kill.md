# X5 — node kill, re-proven against current code

Run 2026-08-17 against the self-hosted 3-node rig (`infra/cluster3.sh`), on the
tree at `6e10a00` — i.e. after the audit fixes, the merge with main's
intervention/3D/semantic-memory work, and the cassette change.

The earlier 5/5 was recorded on Aug 13 against a much older tree. §5.4 asks for
≥5 rehearsals *before recording*, and "it passed two weeks and forty commits
ago" is not that.

## Result: 5/5 survived

```
3/3 nodes up

rehearsal 1/5
  tick 40: killed a node with 3 tasks in flight
  tick 100: node back
  SURVIVED: 3 tasks in flight at the kill, 0 lost,
            1 completions before / 12 after, 9 victims stabilized over 312 ticks
... identical for 2/5 through 5/5 ...

5/5 rehearsals survived
```

## What the two numbers mean

**0 lost** — every task claimed before the kill was still claimed by the same
robot afterwards, or finished. None silently vanished. This is FR-11's first
half and it is measured from **fleet memory**, not from the sim, because the
claim is that the *memory* survived the machine.

**1 completion before the kill, 12 after** — the fleet did not stall. A run that
froze and waited for the node to return would show the completions stopping at
tick 40 and resuming at 100; instead the work continued straight through. That
is FR-11's second half.

## Why every rehearsal is identical

Not a bug and worth stating before someone asks: the sim is deterministic since
`1eb4151` and `221e43c` (X2), so one seed produces one mission byte for byte.
Five identical results mean the *kill* is what is being varied against a fixed
mission, which is the right control — a node dying at tick 40 with three tasks
in flight is the same experiment each time, and it survived it five times.

For variance across *scenarios* rather than across kills, that is X10:
+0.313 [+0.234, +0.393] over 40 generated maps.

## For the video

The beat is narratable in one sentence: **"three nodes, one dies mid-rescue,
and the fleet keeps completing work — one completion before it died, twelve
after."** Cluster badge 3 → 2 on camera; the completions counter never pauses.

Reproduce with:

```bash
bash infra/cluster3.sh up
cd colony && PYTHONPATH=. uv run python ../infra/chaos.py --rehearsals 5
```
