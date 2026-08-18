"""Scale evidence: does the memory layer hold up past demo size?"""
import random, statistics, time, uuid
from fleetmem.client import CockroachFleetMem

random.seed(7)
N_OBS, N_LESS, BATCH = 50_000, 5_000, 500
mem = CockroachFleetMem()
mission = uuid.uuid4()

def vec():
    v = [random.gauss(0, 1) for _ in range(512)]
    n = sum(x * x for x in v) ** 0.5
    return "[" + ",".join(f"{x / n:.5f}" for x in v) + "]"

def bulk(sql, rows):
    t0 = time.time()
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        ph = ",".join(["(" + ",".join(["%s"] * len(chunk[0])) + ")"] * len(chunk))
        mem.conn.execute(sql + ph, [x for r in chunk for x in r])
    return time.time() - t0

print(f"inserting {N_OBS} observations...", flush=True)
rows = [(mission, f"s{i%6}", random.choice(["victim","hazard","debris"]),
         random.randint(0,199), random.randint(0,199), vec()) for i in range(N_OBS)]
el = bulk("INSERT INTO observations (mission_id,robot_id,kind,pos_x,pos_y,embedding) VALUES ", rows)
print(f"  {N_OBS} rows in {el:.1f}s ({N_OBS/el:.0f}/s)", flush=True)

print(f"inserting {N_LESS} lessons...", flush=True)
lrows = [(f"situation {i}", f"lesson {i}", vec()) for i in range(N_LESS)]
el = bulk("INSERT INTO mission_memories (situation,lesson,embedding) VALUES ", lrows)
print(f"  {N_LESS} rows in {el:.1f}s", flush=True)

mem.conn.execute("ANALYZE observations"); mem.conn.execute("ANALYZE mission_memories")

def timed(sql, params, n=25):
    ts = []
    for _ in range(n):
        t0 = time.time(); mem.conn.execute(sql, params).fetchall(); ts.append((time.time()-t0)*1000)
    return statistics.median(ts), sorted(ts)[int(len(ts)*0.95)-1]

q = vec()
print("\n=== tactical recall (mission_memories, no prefix) ===", flush=True)
plan = "\n".join(r["info"] for r in mem.conn.execute(
    "EXPLAIN SELECT id FROM mission_memories ORDER BY embedding <=> %s LIMIT 5", (q,)).fetchall())
print("  plan:", "vector search" if "vector search" in plan else "FULL SCAN")
p50, p95 = timed("SELECT id FROM mission_memories ORDER BY embedding <=> %s LIMIT 5", (q,))
print(f"  rows={N_LESS}  p50={p50:.1f}ms  p95={p95:.1f}ms")

print("\n=== mission-scoped observations (prefix constrained) ===", flush=True)
plan = "\n".join(r["info"] for r in mem.conn.execute(
    "EXPLAIN SELECT id FROM observations WHERE mission_id=%s ORDER BY embedding <=> %s LIMIT 5",
    (mission, q)).fetchall())
print("  plan:", "vector search" if "vector search" in plan else "FULL SCAN")
p50, p95 = timed("SELECT id FROM observations WHERE mission_id=%s ORDER BY embedding <=> %s LIMIT 5", (mission, q))
print(f"  rows={N_OBS}  p50={p50:.1f}ms  p95={p95:.1f}ms")

print("\n=== reconcile gate (the deliberate FULL SCAN) ===", flush=True)
p50, p95 = timed(
    "SELECT id FROM observations WHERE mission_id=%s AND embedding IS NOT NULL AND kind=%s"
    " AND pos_x BETWEEN %s AND %s AND pos_y BETWEEN %s AND %s"
    " ORDER BY embedding <=> %s LIMIT 5", (mission,"victim",10,20,10,20,q), n=10)
print(f"  rows={N_OBS}  p50={p50:.1f}ms  p95={p95:.1f}ms")
mem.close()
