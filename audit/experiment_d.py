"""AUDIT D + X9 evidence: run a real mission against CockroachDB, then ask the
console every canned question and recompute every displayed metric from SQL.

Requires a live cluster (make dev). No AWS, no spend: replay adapter.

Run: PYTHONPATH=. uv run python ../audit/experiment_d.py
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from bedrock.adapter import REPLAY, BedrockAdapter
from console import questions as q
from console.reader import ReadOnlyReader
from fleetmem.client import CockroachFleetMem
from sim.mission import run_mission
from world.map_format import load_map

MAP = Path(__file__).resolve().parent.parent / "colony" / "world" / "maps" / "aftershock.json"


def main() -> int:
    mem = CockroachFleetMem()
    print("connected to:", mem.conn.info.dsn)

    run = run_mission(
        load_map(MAP), mem, coordinated=True, seed=0, embedder=BedrockAdapter(mode=REPLAY)
    )
    mid = run.mission_id
    print(f"mission {mid} finished at tick {run.metrics.ticks}\n")

    print("=== D.2 the five canned console questions, against real rows ===")
    reader = ReadOnlyReader()
    for qid in sorted(q.BY_ID):
        question = q.BY_ID[qid]
        try:
            answer = q.answer(reader, qid, mid, robot_id="m1")
            print(f"\n[{qid}]  memory={question.memory}")
            print(f"  prompt: {question.prompt}")
            print(f"  rows:   {len(answer.rows)}")
            print(f"  answer: {answer.summary[:220]}")
        except Exception as exc:  # noqa: BLE001 - the audit wants the failure
            print(f"\n[{qid}] FAILED: {type(exc).__name__}: {str(exc)[:200]}")

    print("\n\n=== X9 metric recomputation from SQL alone ===")
    with psycopg.connect(mem.conn.info.dsn, row_factory=psycopg.rows.dict_row) as c:
        def scalar(sql, *a):
            return c.execute(sql, a).fetchone()["v"]

        stab = scalar(
            "SELECT count(*) AS v FROM events WHERE mission_id=%s AND verb='victim_stabilized'",
            mid,
        )
        lost = scalar(
            "SELECT count(*) AS v FROM events WHERE mission_id=%s AND verb='victim_lost'", mid
        )
        vics = scalar("SELECT count(*) AS v FROM victims WHERE mission_id=%s", mid)
        obs = scalar("SELECT count(*) AS v FROM observations WHERE mission_id=%s", mid)
        obs_vec = scalar(
            "SELECT count(*) AS v FROM observations WHERE mission_id=%s AND embedding IS NOT NULL",
            mid,
        )
        plans_n = scalar("SELECT count(*) AS v FROM plans WHERE mission_id=%s", mid)
        plans_src = scalar(
            "SELECT count(*) AS v FROM plans WHERE mission_id=%s AND based_on IS NOT NULL", mid
        )
        tasks_n = scalar("SELECT count(*) AS v FROM tasks WHERE mission_id=%s", mid)

        m = run.metrics.to_json()
        print(f"{'metric':28s} {'displayed':>10s} {'from SQL':>10s}  match")
        rows = [
            ("victims_stabilized", m["victims_stabilized"], stab),
            ("victims_lost", m["victims_lost"], lost),
            ("victims_total (store)", m["victims_total"], vics),
        ]
        for name, shown, sql_val in rows:
            print(f"{name:28s} {shown:>10} {sql_val:>10}  {'OK' if shown == sql_val else 'MISMATCH'}")

        print(f"\nobservations rows:            {obs}")
        print(f"  with embedding IS NOT NULL: {obs_vec}   <-- vector path exercised?")
        print(f"plans rows:                   {plans_n} ({plans_src} with based_on)")
        print(f"tasks rows:                   {tasks_n}")

        print("\n=== A-10: does the cosine query use the vector index? ===")
        if obs_vec == 0:
            print("SKIPPED — zero embeddings stored, so the <=> branch never runs.")
            print("Confirms A-10: required CockroachDB tool #2 is dead on this path.")
        else:
            plan = c.execute(
                "EXPLAIN SELECT id FROM observations WHERE mission_id=%s"
                " AND embedding IS NOT NULL ORDER BY embedding <=> %s LIMIT 5",
                (mid, "[" + ",".join(["0.01"] * 512) + "]"),
            ).fetchall()
            for line in plan:
                print("  ", list(line.values())[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
