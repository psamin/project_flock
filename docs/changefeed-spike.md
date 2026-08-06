# Changefeed spike — waking on unblocks instead of polling

The P1 half of §4.4's handoff triggers, and the last unchecked item on lane 1's
TODO:

> MVP polls open tasks at 1 Hz. P1 swaps in a CRDB changefeed on `tasks` →
> orchestrator/agents wake instantly. Same contract, faster push.

**Verdict: it works and it is worth doing — but it stays P1.** The poll is not
broken, and nothing on the recovery path depends on either mechanism.

Code: `colony/fleetmem/changefeed.py`. Tests: `colony/tests/test_changefeed.py`.

## What was built

A **core** changefeed (`EXPERIMENTAL CHANGEFEED FOR tasks`), which streams rows
back to the SQL session rather than to an external sink. That choice is the
whole reason this is demoable: sink changefeeds are a CCL feature needing an
enterprise licence and somewhere to put the messages. A core changefeed runs on
the free single-node dev cluster and on the Cloud free tier alike.

`TaskFeed` runs the statement on its own connection in a background thread and
hands changes to a queue. `poll()` is what an orchestrator would call where it
currently calls `open_tasks()` — that is what §4.4 means by "same contract".

## Measurements

An unblock is `complete_task` flipping a dependent `blocked → open` inside the
same transaction that finishes its last dependency (FR-3). Timed from the write
returning to a listener seeing the change:

| | latency |
|---|---|
| changefeed | **0.094s, 0.107s, 0.112s** |
| 1 Hz poll (by construction) | 0.5s average, 1.0s worst case |

Roughly 5x on the mean and 10x on the tail. Reproduce with
`pytest tests/test_changefeed.py -s`, which prints the numbers rather than
asserting a hoped-for one.

## Three findings that cost the spike its afternoon

**1. `kv.rangefeed.enabled` is off by default.** The changefeed statement fails
outright without it. Now set by `make dev`'s `schema` target and by the CI
workflow, so this is reproducible rather than true only on the laptop where
somebody once ran it by hand.

**2. A changefeed backfills the whole table first.** The default initial scan
replays every existing row before reaching anything live. On a cluster the test
suite has run against a few times, `tasks` holds thousands of rows — so a
listener waiting for one unblock waits behind the entire table's history. This
is the failure mode that *looks* like success: the feed starts, rows arrive, and
none of them are current. `no_initial_scan` is not an optimisation here, it is
the difference between working and not.

**3. A feed carries every write to a row, not just the interesting one.** A task
created `blocked` and later unblocked arrives twice. A listener that acts on the
first change it sees for a given task id wakes on the *creation* and concludes
the task is not claimable — the exact opposite of the handoff it was waiting
for. Filter on the transition, never on the id. This one was caught by a failing
test rather than by reading the docs.

## What this does *not* change

Recovery stays lease-native (§4.4, FR-5). An expired lease produces **no row
write**, so it produces no changefeed message — there is nothing to wake on.
That is not a gap in the feed; it is why the claiming query checks the clock
instead of waiting to be told, and why killing a robot still self-heals with
this module absent entirely.

So the feed accelerates the *handoff* path (a dependency completing, a task
being released after an aftershock) and has nothing to say about the *recovery*
path. Worth stating plainly in the writeup, because "changefeeds make it
resilient" would be the wrong claim: the leases make it resilient, and the
changefeed makes it prompt.

## Why it stays P1

Adopting it means every agent grows a second connection and a background thread
for a saving of about 0.4s on a 4 Hz sim — under two ticks. The demo's
bottleneck is robots walking, not robots noticing. The spike's value is that the
answer is now measured instead of assumed, and the module is ready if a later
scenario makes handoff latency visible.
