# Spike 01 Results — Cross-Engine Canonical Checksum (PostgreSQL ↔ CockroachDB)

**Verdict: the design holds.** Cross-engine checksum equality is achievable for all ten
types in scope, computed entirely in-engine, with no rows crossing the wire. Two types
needed real canonicalization work (`numeric`, `float8`); the other eight were proven
correct as-is. Nothing was downgraded to a degraded check.

Measured against PostgreSQL 16 and CockroachDB (`cockroachdb/cockroach:latest`,
`start-single-node --insecure`), driven by one psycopg harness — CockroachDB speaks
pgwire, so both engines take the identical query text.

This document is the deliverable: it should be enough to re-implement the canonical
layer without reading the code.

---

## 1. The recipe

**Per row** — canonicalize each column to text, hash each to a fixed-width digest,
concatenate, hash again:

```sql
md5(
  coalesce(md5(<canonical(col1)>), 'NULL_') ||
  coalesce(md5(<canonical(col2)>), 'NULL_') || ...
)
```

Fixed-width digests are what make delimiters unnecessary: every column contributes
exactly 32 characters, so `('ab','c')` and `('a','bc')` cannot produce the same byte
string. `'NULL_'` is five characters — deliberately not 32 — so a NULL can never collide
with a real digest. There is a test for each of these properties.

**Per range** — slice the row hash, parse to an integer, sum as `NUMERIC`:

```sql
SELECT sum(('x' || substr(<row hash>, 1, 16))::bit(64)::int8::numeric), count(*)
  FROM <table>
 WHERE <pk> >= $1 AND <pk> < $2
```

`sum()` is order-independent, so scan order and collation drop out entirely.
`NUMERIC` cannot overflow. `count(*)` rides along as a second, independent signal —
it catches the degenerate case of deleted rows whose hashes happen to sum to zero.

Column order is taken from `information_schema.columns ORDER BY column_name`, not from
declaration order, so a table declared with columns in a different order on the target
still checksums identically.

**Empty range convention:** `(NULL, 0)` on both engines.

### The hex → int idiom that won

`('x' || <hex>)::bit(32)::int` is a Postgres idiom and the spike's named suspicion.
**CockroachDB accepts it unchanged**, and also accepts the wider `::bit(64)::int8`,
so the recipe takes 64 bits rather than 32 — double the entropy per row at no cost.

Losers, for the record: `strtol()` exists on neither engine. `fnv64` is CockroachDB-only
and was never a candidate.

---

## 2. Canonical cast per type

Only two types need a non-default cast. The other eight use `{col}::text`, and that is a
*measured* result, not an assumption.

| Type | `data_type` | Canonical cast | Status |
|---|---|---|---|
| `int8`/`int4`/`int2` | `bigint` | `{col}::text` | verified |
| `text`/`varchar` | `text` | `{col}::text` | verified |
| `bool` | `boolean` | `{col}::text` | verified |
| `uuid` | `uuid` | `{col}::text` | verified |
| `date` | `date` | `{col}::text` | verified |
| `timestamptz` | `timestamp with time zone` | `{col}::text` | verified — **requires the UTC session pin** |
| `bytea` | `bytea` | `{col}::text` | verified |
| `jsonb` | `jsonb` | `{col}::text` | verified |
| `numeric` | `numeric` | guarded `to_char` — §3.1 | verified within a stated range |
| `float8` | `double precision` | text + residual — §3.2 | verified — **requires `extra_float_digits = 0`** |

**Hypotheses from the brief that did not reproduce**, each worth knowing because each one
would have been wasted work:

- `bool` — the brief warned that `t/f` vs `true/false` rendering would diverge. Both
  engines render `true`/`false`. No `::int` cast needed.
- `uuid` — no `lower()` needed; both engines normalize UUIDs to lowercase on storage,
  including uppercase input literals.
- `timestamptz` — no epoch extraction needed; `::text` agrees to microsecond precision,
  including sub-second trailing zeros, midnight boundaries and non-UTC input offsets.
- `jsonb` — both engines normalize key order and whitespace identically, and agree on
  number rendering inside the document (`1.0`, `1`, `1.10`).
- `bytea` — `encode()` exists on both, but plain `::text` already agrees, so the recipe
  uses the simpler form.
- `numeric` trailing zeros — `1.50` vs `1.5`, called out in the brief as the classic
  fight, was **not** where numeric broke.

### Types explicitly deferred

`arrays`, `geo`, `enums` — out of scope for this spike, unproven, and therefore
**degraded check** (counts + aggregates) until someone does the work. Do not assume they
inherit `::text`.

---

## 3. The two types that fought back

### 3.1 numeric — scientific notation, and a lossy fix that had to be guarded

**The divergence.** Not trailing zeros. CockroachDB renders values carrying an exponent
in scientific notation where Postgres always renders plain:

| value | PostgreSQL | CockroachDB |
|---|---|---|
| `1e10` | `10000000000` | `1E+10` |
| `1E-10` | `0.0000000001` | `1E-10` |
| `-0.000000000001` | `-0.000000000001` | `-1E-12` |

**The fix.** `to_char` with an `FM` format agrees on both engines, and normalizes display
scale as a bonus (`1.50` → `1.5`), which is what we want: two numerically equal values
must not be reported as corruption.

```sql
case when abs({col}) < 1e20 and {col} = trunc({col}, 30)
     then to_char({col}, 'FM<20 nines>.<30 nines>')
     else {col}::text end
```

**Why the guard is not optional.** `to_char` is a *lossy* renderer, and lossy means
collisions, and a collision between two different values is a **false pass** — a
corrupted migration certified as clean. Two measured failure modes:

1. **CockroachDB's `to_char` raises `invalid operation` above 20 integer digits.**
   Exact at 20, errors at 21. An unguarded call doesn't just mis-render, it takes down
   the entire range check.
2. **Values with more fractional digits than the format collapse together.**
   `0.000…1` (41 dp) and `0.000…2` both render `0.` — on *both* engines. Silent, agreeing,
   and wrong.

Outside the guarded band the cast falls back to `::text`, which is lossless on each engine
but may render differently across them. That direction of error is deliberate: a spurious
divergence gets investigated and costs a recursion; a spurious match is undetectable.
Two out-of-range values can never collide, because the two branches produce strings of
visibly different length and shape.

**Supported range: |v| < 10²⁰ and ≤ 30 fractional digits**, exactly. Covers `int8`-shaped
data, money, and Oracle/SQL Server `NUMBER`/`DECIMAL(38, s)` up to 20 integer digits.
Wider values still verify, just noisily.

**Rejected:** `trim_scale()` (does not exist on CockroachDB), `scale()` (same), fixed-scale
casts like `::numeric(38,18)` (still emits E-notation on CockroachDB *and* rounds away
small values), `format('%s', v)` and `v + 0` (no effect on notation).

### 3.2 float8 — the hard one

Found by Hypothesis, not by curated values. The golden suite's hand-picked float set —
`NaN`, `±Infinity`, `-0.0`, `1e308`, `5e-324` — passed cleanly and proved nothing.

**Two distinct divergences**, at the default `extra_float_digits = 1`:

| value | PostgreSQL | CockroachDB |
|---|---|---|
| `1000000.0` | `1000000` | `1e+06` |
| `123456789.123456` | `123456789.123456` | `1.23456789123456e+08` |
| `2.405830338592357e16` | `2.4058303385923568e+16` | `2.405830338592357e+16` |

The first two are notation. The third is worse: **the same double, spelled with different
digits**. Both are valid shortest-round-trip forms; the engines simply choose differently.

**Setting `extra_float_digits = 0` makes the renderings agree on both engines** — the brief
assumed this knob was PG-only, but CockroachDB honours it. That alone is not a fix: 0 means
15 significant digits and a double carries up to 17, so distinct doubles start colliding.
Trading false alarms for false passes is the wrong direction.

**The fix — 15-digit text plus an exact residual:**

```sql
case when abs({col}) > 1e307
     then ({col}/4)::text || '|' || ({col}/4 - (({col}/4)::text)::float8)::text
     else {col}::text     || '|' || ({col}   - (({col}  )::text)::float8)::text end
```

`({col}::text)::float8` is the 15-digit value parsed back. Subtracting it is **exact** —
the operands are within a factor of two, so Sterbenz applies — and IEEE arithmetic is
deterministic, so both engines compute a bit-identical residual whose own 15-digit
rendering exposes precisely the digits the first term dropped. Two doubles that agree on
the first term must differ in the second.

**The `> 1e307` branch is not decoration.** Near `DBL_MAX`, rounding to 15 digits rounds
*up*: `1.7976931348623151e308` renders as `1.79769313486232e+308`, which is larger than
`DBL_MAX`, and parsing it back raises `out of range` — killing the whole range check, not
just that row. Dividing by four first is exact (power of two, far from underflow), keeps
the round-trip in range, and stays injective. Halving instead of quartering would be
enough for range, but the branch is only reachable for large values, where underflow is
not a concern.

**Rejected, each for a specific reason:**

- `to_char(float8, …)` — unimplemented on CockroachDB.
- `float8::numeric` — **lossy on Postgres**: `1.0/3.0` converts to 15 digits (CockroachDB
  gives 16). Silent precision loss feeding a checksum is exactly the false-pass path.
- `float8send()` / bit access — unimplemented on CockroachDB; `float → bit` is an invalid
  cast there, so the IEEE-bits approach is closed off entirely.

**`-0.0` and `0.0` canonicalize identically.** They compare equal under IEEE, and flagging
that as data corruption would be a false alarm. Stated here because it is a deliberate
choice, not an oversight.

**Confidence:** 150 Hypothesis examples in the suite, plus a stress run of **4,000 random
64-bit patterns** (NaN, subnormals, ±0, ±Inf) — zero cross-engine mismatches — and 400
distinct doubles producing 400 distinct checksums, zero collisions.

---

## 4. Session settings the recipe depends on

Both are load-bearing, both are honoured by both engines, and both have a test proving the
recipe actually depends on them:

```sql
SET TIME ZONE 'UTC';          -- timestamptz canonicalizes via ::text, which renders
                              -- in the session zone
SET extra_float_digits = 0;   -- the engines only agree on float8 text at 0
```

A missing pin produces a **false divergence**, not a false match — the safe direction —
but it will fire on every timestamp in the table, so it will look like catastrophe rather
than misconfiguration.

**One more trap, dodged rather than fixed:** the canonical-cast lookup is keyed on
`information_schema.columns.data_type`. Both engines report *identical* names for all ten
types (`bigint`, `double precision`, `timestamp with time zone`, …). Had they disagreed,
each engine would have silently applied a *different* canonical cast to the same column
and every comparison would have diverged for a reason nowhere near the actual cause.
Verify this first when adding an engine.

---

## 5. Divergence cornering

Checksum a range on both engines; if they agree the whole subtree is clean; if they differ,
split by `fanout` and descend only into children that differ.

Measured on 100,000 rows with exactly one row mutated on the target (a timestamptz with its
sub-second precision truncated — the PRD's demo quirk):

| | |
|---|---|
| Full-range checksums | diverge, while `count(*)` still matches on both sides |
| Rows cornered to | a **3-row leaf**, containing the mutated row |
| Cost | **41 checksum pairs**, depth 5 — against 100,000 rows |
| Clean data | **1 check**, no descent |

The clean-data case is the one that matters for cost at scale: an unchanged table is one
query per side, and the recursion never starts.

---

## 6. Performance

1,000,000 rows × 5 columns (`int8`, `timestamptz`, `numeric`, `float8`, `text`), full-range
checksum, best of three runs, single node in Docker on a laptop:

| Engine | Time | Throughput |
|---|---|---|
| PostgreSQL 16 | 4.06 s | **~247,000 rows/sec** |
| CockroachDB | 7.30 s | **~137,000 rows/sec** |

Both are single-threaded, single-range numbers with no tuning. They are a floor, not a
ceiling: the verifier swarm's whole design is to run many of these concurrently against
disjoint ranges, and CockroachDB distributes them across nodes.

---

## 7. What this de-risks, and what it does not

**Settled.** Cross-engine checksum equality works, in-engine, for the ten types in scope.
The PRD's Phase 0 exit criterion — *"canonical-cast golden suite green across all supported
types"* — is met, and the recursion mechanic behaves as designed at 100k rows.

**Not settled, and worth naming:**

1. **Only one engine pair is proven.** Every result here is PostgreSQL ↔ CockroachDB. MySQL
   and Oracle sources will each need their own pass, and `bool` is the obvious first
   casualty (MySQL renders `1`/`0`).
2. **Arrays, geo and enums are unproven**, not merely unimplemented.
3. **`numeric` beyond 20 integer digits verifies noisily**, not silently — acceptable, but
   it will generate recursion work on wide-decimal tables.
4. **CockroachDB `latest` was the target.** The `to_char` 20-digit ceiling is an observed
   behaviour of that build, not a documented contract. Pin the version and re-run the
   golden suite on upgrade.
5. **Throughput is a laptop-Docker floor.** Real numbers need real hardware and concurrency.

---

## 8. Reproducing

```bash
make up      # postgres:16 + cockroach single-node, waits for both to be healthy
make test    # 58 tests: M0 infra, M1 primitive, M2 golden suite, M3 adversarial, M4 recursion
make down
```

`tests/test_m2_types.py` is the golden suite — the file the PRD names as the Phase 0 exit
criterion.
