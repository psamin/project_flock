# Spike 01 — Cross-Engine Canonical Checksum (Flock, Phase 0)

**Goal:** a `range_checksum(conn, table, pk_col, lo, hi)` function that returns **identical values on PostgreSQL and CockroachDB for identical data**, proven by a golden test suite — plus divergence detection and range splitting. This is the highest-risk unknown in the Flock PRD (§6.2); if this doesn't work, the verification design changes, so we prove it before building anything else.

**What this is NOT:** no agents, no LLM, no Lambda, no MOLT, no task queue. Pure SQL + a thin Python harness + pytest.

---

## Hard rules (read before writing any code)

1. **The engines compute; the harness compares.** The checksum must be computed *inside* each database by one SQL query. The Python harness only ever compares two integers. Never pull rows into Python to compare — that defeats the entire architecture (in-engine, ship-bytes-only-on-divergence).
2. **Order-independence is mandatory.** The aggregate must not depend on scan order or collation: combine per-row hashes with `sum(...)` (as `NUMERIC` to dodge overflow), never string-aggregation in row order.
3. **One milestone at a time, tests green before the next.** Commit per green milestone.
4. **Every trap gets written down.** When a type's naive cast diverges between engines, the fix (or the "unsupported → degraded check" verdict) goes in `RESULTS.md`. The documented recipe *is* the deliverable — it seeds the PRD's canonical-cast layer.
5. **Pin the session.** Run `SET TIME ZONE 'UTC'` on both connections at startup. Document any other session settings the recipe depends on (e.g. `extra_float_digits` is PG-only — note it).

---

## Environment

```
spike-01-checksum/
├── docker-compose.yml     # postgres:16 + cockroachdb (start-single-node --insecure)
├── Makefile               # make up / make seed / make test / make down
├── harness/
│   ├── db.py              # two connections via psycopg (CRDB speaks pgwire — same driver)
│   ├── checksum.py        # range_checksum(), CANONICAL_CASTS, split_range()
│   └── seed.py            # deterministic data generator, seeds BOTH engines identically
├── tests/
│   ├── test_m1_primitive.py
│   ├── test_m2_types.py   # the golden suite — one test class per type
│   ├── test_m3_adversarial.py
│   └── test_m4_divergence.py
└── RESULTS.md             # the recipe-per-type report (M5)
```

- Python 3.11+, `psycopg[binary]`, `pytest`, `hypothesis`.
- CockroachDB: `cockroachdb/cockroach:latest`, `start-single-node --insecure`, port 26257. Postgres 16 on 5432.
- `seed.py` must be deterministic (fixed RNG seed) and insert the **same rows into both engines in different orders** — that's the built-in order-independence check.

---

## The contract

```python
def range_checksum(conn, table: str, pk_col: str, lo, hi) -> int | None:
    """Checksum of rows where lo <= pk < hi, computed in-engine.
    Identical data => identical value on both engines.
    Empty range => None (and both engines must agree it's empty).
    """

def split_range(lo, hi, fanout: int = 8) -> list[tuple]:
    """Split [lo, hi) into up to `fanout` sub-ranges for recursion."""

CANONICAL_CASTS: dict[str, str]   # type name -> SQL expression template, e.g. "extract(epoch from {col})::numeric(20,6)::text"
```

---

## Starting recipe (verify, don't trust — discovering where this breaks is the spike)

Per-row: canonicalize each column to text, hash each column to a **fixed-width digest**, concatenate digests, hash again. Fixed-width digests kill delimiter-ambiguity problems (no `'a|b' vs 'a','b'` collisions, no escaping):

```sql
md5(
  coalesce(md5(<canonical(col1)>), 'NULL_') ||
  coalesce(md5(<canonical(col2)>), 'NULL_') || ...
)
```

`'NULL_'` is 5 chars — deliberately not 32 — so a NULL can never collide with a real digest. `md5()` exists on both engines; CRDB's `fnv64` does not exist on PG, so don't use it.

Per-range aggregate — take a slice of the hex, parse to a number, sum as NUMERIC:

```sql
SELECT sum(('x' || substr(md5(...row expr...), 1, 8))::bit(32)::int::numeric), count(*)
  FROM {table}
 WHERE {pk_col} >= %s AND {pk_col} < %s
```

**Known suspicion to test first:** the `('x' || hex)::bit(32)::int` trick is a Postgres idiom — verify CRDB accepts it. If not, find the CRDB-portable hex→int path (candidates: `to_hex` inverse via `decode`, arithmetic on `ascii()` of substrings, or summing two 4-hex-char slices). Whatever works on **both** wins; document the loser.

Also return `count(*)` alongside the sum — two mismatching signals are better than one, and count catches the sum-collision edge (deleting rows whose hashes sum to zero).

---

## Canonicalization: starting hypotheses per type

These are hypotheses to falsify, not answers. Add one type at a time (M2); each gets a golden table.

| Type | Candidate canonical cast | Known traps to test for |
|---|---|---|
| `int2/int4/int8` | `{col}::int8::text` | none expected — do this one first |
| `text/varchar` | `{col}` (identity) | collation only affects *order*, and we're order-independent — but test unicode, emoji, empty string vs NULL |
| `bool` | `({col}::int)::text` | text rendering differs (`t/f` vs `true/false`) — never checksum the raw text form |
| `uuid` | `lower({col}::text)` | case normalization |
| `date` | `{col}::text` | ISO on both — verify |
| `timestamptz` | `extract(epoch from {col})::numeric(20,6)::text` | precision rendering, session timezone (pinned to UTC), sub-second trailing zeros |
| `timestamp` | same via epoch | implicit-timezone semantics differ — document carefully |
| `numeric/decimal` | `{col}::text` then, if trailing zeros diverge: normalize scale | `1.50` vs `1.5` is the classic — this is likely your first real fight |
| `float4/float8` | start with `{col}::text` | shortest-round-trip formatting differs between engines; `NaN`, `+Inf`, `-Inf`, `-0.0`. If text diverges, try casting through a fixed representation — and if nothing portable works, verdict = "degraded check" and move on |
| `bytea` | `encode({col}, 'hex')` | verify `encode` exists on CRDB |
| `jsonb` | `{col}::text` | key ordering + whitespace normalization (both engines normalize jsonb, but *verify*), number rendering inside the JSON |
| arrays, geo, enums | **explicitly deferred** | record as "deferred — degraded check" in RESULTS.md |

---

## Milestones

### M0 — Infra (should take an hour, not a day)
- `make up` starts both engines; harness connects to both via psycopg; `SET TIME ZONE 'UTC'` on connect.
- `seed.py` creates `t_smoke (id int8 primary key, v text)` on both, inserts 1,000 identical rows **in different insertion orders**.
- **Exit test:** `count(*)` matches across engines.

### M1 — Hash primitive
- Implement the row-hash + sum aggregate on `t_smoke` (int8 + text only, no canonicalization yet).
- **Exit tests:** (a) checksums equal across engines; (b) equal despite different insertion order; (c) NULLs in `v` handled — a NULL row and a `'NULL_'`-string row produce *different* checksums; (d) empty range returns the empty convention on both.

### M2 — The golden suite (the heart of the spike)
- One golden table per type from the canonicalization table: `t_int8`, `t_bool`, `t_numeric`, `t_timestamptz`, `t_float8`, `t_uuid`, `t_bytea`, `t_jsonb`, `t_date`, `t_text`.
- Each table: ~200 rows of normal values + boundary values + NULLs, seeded identically on both engines.
- Work **one type per commit**. When a type diverges: fix the canonical cast, or document it as degraded. Update `CANONICAL_CASTS` as you go.
- **Exit test:** parametrized pytest — every golden table's checksum matches across engines. This test file becomes the PRD's named Phase 0 exit criterion.

### M3 — Adversarial values
- Hypothesis property tests: random rows per type, seeded into both engines, checksums must match.
- Curated nasties, one test each: `NaN`/`+Inf`/`-Inf`/`-0.0` (float), `1.50` vs `1.5` (numeric), timestamptz at exactly 6-digit sub-second precision and at midnight boundaries, emoji + multi-byte unicode, strings containing `|`, `'NULL_'`, and md5-looking hex, max/min int8.
- **Exit test:** all green, or the failure is documented as a degraded type in RESULTS.md.

### M4 — Divergence + recursion (the demo's money mechanic, proven small)
- Seed a 100k-row table identically; mutate exactly **one row on the target** (e.g. truncate a timestamptz's sub-second precision — the PRD's chosen demo quirk, §13.17).
- **Exit tests:** (a) full-range checksums now differ; (b) `split_range` + a recursive loop corners the mutated row to a leaf range of ≤16 rows within a bounded depth; (c) row count of checks executed is logged — it should be O(fanout × depth), nowhere near 100k.

### M5 — RESULTS.md
- The recipe per type (final `CANONICAL_CASTS`), every trap found and its workaround, deferred/degraded types, the portable hex→int idiom that won, session settings the recipe depends on, and a rough perf number (rows/sec checksummed on a 1M-row table, both engines).
- **Exit:** someone who hasn't seen this repo could re-implement the canonical layer from RESULTS.md alone.

---

## Definition of done

`pytest` green on M1–M4 against live containers of both engines, plus RESULTS.md. That satisfies the PRD Phase 0 exit criterion *"canonical-cast golden suite green across all supported types"* and de-risks §6.2. Total honest estimate: 2 days — of which numeric/float canonicalization is most of the fighting.

## Prompt to hand Claude Code

> Read spike-01-checksum.md in full. Build milestone by milestone starting at M0; do not start a milestone until the previous one's exit tests pass; commit per green milestone. Obey the hard rules section — especially rule 1 (engines compute, harness compares) and rule 4 (every trap goes in RESULTS.md). When a canonical cast diverges between engines, show me the two raw outputs before choosing a fix. The starting recipe is a hypothesis: verify the bit-cast idiom on CockroachDB before building on it.
