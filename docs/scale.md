# Scale evidence

Criterion 1 asks whether CockroachDB is doing production-grade work "at real
scale". Our largest dataset before this was ~1000 observations, which is demo
scale and does not answer the question. This does.

**Method.** 50,000 observations with real 512-dim unit vectors in one mission,
plus 5,000 lessons in `mission_memories`. `ANALYZE` on both tables, then 25
timed runs per query on a single-node v26.2.5 (Docker, laptop). Harness:
[`audit/experiment_scale.py`](../audit/experiment_scale.py).

## Results

| query | rows | plan | p50 | p95 |
|---|---|---|---|---|
| Tactical recall — no prefix, ranks every lesson | 5,000 | **`vector search`** | **36.0 ms** | 57.6 ms |
| Mission-scoped observations — prefix constrained | **50,000** | **`vector search`** | **21.3 ms** | 25.0 ms |
| Reconcile gate — the deliberate full scan | 50,000 | `FULL SCAN` | **426.0 ms** | 577.5 ms |

Ingest: **50,000 embedded observations in 473 s (~106 rows/s)** with the vector
index maintained on every insert. 5,000 lessons took 7.3 s.

## What this settles

**The vector index is real and it scales.** 50k rows, prefix-constrained, still
`vector search` at a **21 ms p50** — and *faster* than the 5k-row unprefixed
recall, which is the prefix doing its job. The earlier note that the index was
"never exercised at demo scale" (D-3) is now answered with a number instead of
an argument.

**The reconcile gate's full scan is 20× slower, exactly as predicted.** 426 ms
against 21 ms on the same table. That is the price of the correctness trade
documented in the README, and it is now measured rather than asserted.

## What this does *not* settle — stated plainly

**426 ms would not be acceptable in production.** The gate runs inside the
insert transaction on every observation, so at 50k rows per mission it would
dominate the write path. At demo scale — ~1000 observations in a full mission —
it is a few milliseconds and the exactness is worth far more than the speed.

So the honest claim is scoped: *the gate's full scan is the right trade at
mission scale, and is the first thing that would need reworking if a mission
carried 50× more beliefs.* The rework is a prefix redesign (making `kind` a
prefix column so it stays index-served), not moving the filter after the top-k
— that reintroduces the duplicate-merge bug the gate exists to prevent.

**~106 inserts/s is not a throughput claim.** That is a single node on a laptop
with an index update per row, measured to size the dataset rather than to
benchmark ingest. A real cluster and batched writes would look nothing like it,
and we are not claiming otherwise.

**One mission, one node.** This measures query behaviour against data volume,
not concurrent load. Contention is covered separately by the
`ThreadPoolExecutor` races in `tests/test_claiming.py`, and node failure by
`audit/x5-node-kill.md`.
