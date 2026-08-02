# Lane 1 — Memory & data layer

Checklist from PRD §5.1, ordered to match the §5.3 day plan: walking-skeleton
items first, then leased claiming, then the reconcile gate, then P1 last.

Each sub-bullet is the machine-verifiable done-condition. An item is only checked
off when a test proves that condition.

## Walking skeleton (Aug 1–3)

- [x] CRDB Cloud cluster (3 nodes) + local `cockroach demo` dev recipe
  - One command brings up a local cluster and applies the schema; a 3-node
    recipe reports all 3 nodes healthy, which is what §6.5's node-kill segment
    runs against.
  - Done: `make dev` (single node) and `make cluster-3` (three nodes, joined,
    schema applied). Verified live — 3/3 nodes reporting, and killing one still
    serves writes. **The CockroachDB Cloud half needs Praneeth**: it requires a
    Cloud account, so only the self-hosted rig — which §6.5 says the node-kill
    segment actually runs on — is built here.
- [ ] Schema v1.1 (lease column, `plans` table, memory-type comments) validated against docs, incl. `VECTOR` + vector index syntax
  - All eight §4.5 tables apply to a live CockroachDB v26.2 instance;
    `observations.embedding` is `VECTOR(512)`; the vector index exists and
    `EXPLAIN` shows a cosine query using it rather than a full scan.
- [ ] `fleetmem` Python SDK: `report_observation, claim_task, complete_task, get_beliefs, heartbeat (status + lease renewal), log_plan, log_event`
  - All seven methods exist on the real client, the in-memory fake mirrors every
    signature, and the same behavioural suite passes against both (§5.2: lanes
    2/4 build against the fake).

## Coordination mechanics (Aug 4–8)

- [ ] Lease claiming txn + concurrency tests
  - 1,000 open-claim races AND expired-lease takeover races — zero
    double-claims.
- [ ] Reconcile gate (embed → search → merge/insert txn) + unit tests
  - Two sightings of one victim within 5 tiles at ≥0.82 cosine produce one
    belief with `sightings=2`, in a single transaction; below the threshold they
    stay separate.

## Resilience & security (Aug 13–14)

- [ ] Node-kill chaos script
  - Killing 1 of 3 CockroachDB nodes mid-mission causes zero task loss and no
    fleet stall (FR-11), rehearsed ≥5 times before recording (§5.4).
- [ ] Per-robot credentials
  - Each robot connects as its own SQL user with least-privilege grants, and the
    commander's MCP role is read-only (§3.5, §6.3).

## P1

- [ ] Changefeed spike (P1)
  - A changefeed on `tasks` wakes the orchestrator/agents on an unblock instead
    of the 1 Hz poll, against the same contract (§4.4).
