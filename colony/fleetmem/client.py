"""CockroachDB-backed fleet memory — the shared brain (PRD §4.2, §4.4).

This module is the SDK contract other lanes build against (§5.2 contract 1),
frozen Aug 3. `fleetmem.fake.FakeFleetMem` mirrors every signature here so lanes
2 and 4 can work without a cluster; tests/test_contract.py fails if the two drift
apart.
"""

from __future__ import annotations

import json
from typing import Any, Sequence
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from fleetmem.types import BLOCKED, DONE, OPEN, Belief, Match, Task

DEFAULT_DSN = "postgresql://root@localhost:26257/colony?sslmode=disable"

# Cosine distance below which two observations are the same belief (§4.2 step 3
# states the gate as >=0.82 cosine *similarity*; distance = 1 - similarity).
MERGE_DISTANCE = 0.18
MERGE_RADIUS_TILES = 5


class CockroachFleetMem:
    def __init__(self, dsn: str = DEFAULT_DSN):
        self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    def close(self) -> None:
        self.conn.close()

    # --- observations -----------------------------------------------------

    def report_observation(
        self,
        mission_id: UUID,
        robot_id: str,
        kind: str,
        pos: tuple[int, int],
        payload: dict[str, Any] | None = None,
        embedding: Sequence[float] | None = None,
        confidence: float = 1.0,
    ) -> UUID:
        """Write what a robot just sensed; returns the id of the resulting belief.

        This is the **reconcile gate** (§4.2 step 3): one transaction that looks
        for an existing belief within `MERGE_RADIUS_TILES` whose embedding is
        within `MERGE_DISTANCE`, and either merges into it — bumping confidence
        and sighting count — or inserts a new one. Merging is what stops two
        scouts seeing the same victim from creating two victims.

        Called without an embedding, the gate degrades to position+kind matching,
        which is what runs when Bedrock is unavailable.
        """
        with self.conn.transaction():
            match = self.find_similar(mission_id, kind, pos, embedding)
            if match is not None:
                self.conn.execute(
                    "UPDATE observations"
                    "   SET sightings = sightings + 1,"
                    "       confidence = least(1.0, confidence + %s * 0.1),"
                    "       observed_at = now()"
                    " WHERE id = %s",
                    (confidence, match.belief_id),
                )
                return match.belief_id

            row = self.conn.execute(
                "INSERT INTO observations"
                " (mission_id, robot_id, kind, pos_x, pos_y, payload, embedding, confidence)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    mission_id, robot_id, kind, pos[0], pos[1],
                    json.dumps(payload or {}),
                    _vec(embedding),
                    confidence,
                ),
            ).fetchone()
            return row["id"]

    def find_similar(
        self,
        mission_id: UUID,
        kind: str,
        pos: tuple[int, int],
        embedding: Sequence[float] | None,
        limit: int = 5,
    ) -> Match | None:
        """Nearest existing belief to this observation, or None.

        The vector half is CockroachDB's Distributed Vector Indexing doing the
        work: cosine top-k against `obs_embedding_idx`, prefix-scoped to the
        mission so the index actually engages.
        """
        x, y = pos
        box = (x - MERGE_RADIUS_TILES, y - MERGE_RADIUS_TILES,
               x + MERGE_RADIUS_TILES, y + MERGE_RADIUS_TILES)

        if embedding is None:
            row = self.conn.execute(
                "SELECT id FROM observations"
                " WHERE mission_id = %s AND kind = %s"
                "   AND pos_x BETWEEN %s AND %s AND pos_y BETWEEN %s AND %s"
                " ORDER BY observed_at DESC LIMIT 1",
                (mission_id, kind, box[0], box[2], box[1], box[3]),
            ).fetchone()
            return None if row is None else Match(row["id"], distance=0.0)

        # kind and the position box are constrained IN THE QUERY, not filtered
        # afterwards. Filtering a top-k result set in Python silently misses real
        # duplicates: if `limit` nearer-but-irrelevant observations exist anywhere
        # in the mission, the actual match never makes it into the rows, and
        # report_observation inserts a second belief for one victim.
        #
        # ORDER BY <=> with the mission_id prefix constrained is what lets the
        # cosine index serve this; see the index comment in schema/v0.sql.
        rows = self.conn.execute(
            "SELECT id, embedding <=> %s AS distance"
            "  FROM observations"
            " WHERE mission_id = %s AND embedding IS NOT NULL"
            "   AND kind = %s"
            "   AND pos_x BETWEEN %s AND %s AND pos_y BETWEEN %s AND %s"
            " ORDER BY embedding <=> %s LIMIT %s",
            (_vec(embedding), mission_id, kind,
             box[0], box[2], box[1], box[3],
             _vec(embedding), limit),
        ).fetchall()

        for r in rows:
            if r["distance"] <= MERGE_DISTANCE:
                return Match(r["id"], distance=float(r["distance"]))
        return None

    def get_beliefs(
        self,
        mission_id: UUID,
        area: tuple[int, int, int, int] | None = None,
        kind: str | None = None,
    ) -> list[Belief]:
        """Shared world state, optionally limited to a bounding box
        (min_x, min_y, max_x, max_y) inclusive. Agents cache this ~1s (§4.3)."""
        sql = ["SELECT * FROM observations WHERE mission_id = %s"]
        params: list[Any] = [mission_id]
        if area is not None:
            sql.append("AND pos_x BETWEEN %s AND %s AND pos_y BETWEEN %s AND %s")
            params += [area[0], area[2], area[1], area[3]]
        if kind is not None:
            sql.append("AND kind = %s")
            params.append(kind)
        rows = self.conn.execute(" ".join(sql), params).fetchall()
        return [_belief(r) for r in rows]

    # --- tasks ------------------------------------------------------------

    def create_task(
        self,
        mission_id: UUID,
        kind: str,
        target: tuple[int | None, int | None] = (None, None),
        priority: int = 1,
        depends_on: Sequence[UUID] = (),
    ) -> UUID:
        """Create a task. Starts `open` with no dependencies, `blocked` with.

        Unknown dependency ids are rejected. Left unchecked the two
        implementations disagree: the client's `NOT EXISTS` unblock subquery
        finds no row for a missing dependency and so opens the task immediately,
        while the fake raises. A task that silently unblocks is the worse of the
        two — it dispatches a medic to a victim still behind rubble — so both
        refuse the bad input up front instead.
        """
        deps = list(depends_on)
        if deps:
            known = self.conn.execute(
                "SELECT count(*) AS n FROM tasks WHERE id = ANY(%s)", (deps,)
            ).fetchone()["n"]
            if known != len(set(deps)):
                raise ValueError(f"depends_on refers to unknown task ids: {deps}")
        row = self.conn.execute(
            "INSERT INTO tasks"
            " (mission_id, kind, target_x, target_y, priority, status, depends_on)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (mission_id, kind, target[0], target[1], priority,
             BLOCKED if deps else OPEN, deps or None),
        ).fetchone()
        return row["id"]

    def claim_task(self, task_id: UUID, robot_id: str) -> bool:
        """Attempt to claim an open task. True iff this robot won it.

        The judge-friendly line of SQL (§4.4). Under serializable isolation the
        `status='open'` predicate makes exactly one concurrent caller win; losers
        get zero rows back rather than an error, so no retry logic is needed.
        """
        row = self.conn.execute(
            "UPDATE tasks SET status = 'claimed', claimed_by = %s, claimed_at = now()"
            " WHERE id = %s AND status = 'open' RETURNING id",
            (robot_id, task_id),
        ).fetchone()
        return row is not None

    def complete_task(self, task_id: UUID, robot_id: str) -> list[UUID]:
        """Mark a task done and unblock its dependents in the SAME transaction
        (§4.4). Returns the ids of tasks that became open as a result.

        A blocked task opens only when *every* dependency is done, so this checks
        the whole `depends_on` array rather than just the task that finished —
        the scout→lifter→lifter→medic chain in §3.3 has a task with two.
        """
        with self.conn.transaction():
            done = self.conn.execute(
                "UPDATE tasks SET status = %s, done_at = now()"
                " WHERE id = %s AND claimed_by = %s AND status != %s RETURNING id",
                (DONE, task_id, robot_id, DONE),
            ).fetchone()
            if done is None:
                return []
            unblocked = self.conn.execute(
                "UPDATE tasks SET status = %s"
                " WHERE status = %s AND %s = ANY(depends_on)"
                "   AND NOT EXISTS ("
                "     SELECT 1 FROM tasks dep"
                "      WHERE dep.id = ANY(tasks.depends_on) AND dep.status != %s)"
                " RETURNING id",
                (OPEN, BLOCKED, task_id, DONE),
            ).fetchall()
        return [r["id"] for r in unblocked]

    def open_tasks(self, mission_id: UUID) -> list[Task]:
        """Claimable tasks, for the orchestrator's allocation pass (§4.4)."""
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE mission_id = %s AND status = %s"
            " ORDER BY priority DESC",
            (mission_id, OPEN),
        ).fetchall()
        return [_task(r) for r in rows]

    # --- robots and log ---------------------------------------------------

    def heartbeat(
        self,
        robot_id: str,
        pos: tuple[int, int] | None = None,
        battery: int | None = None,
        status: str | None = None,
    ) -> None:
        """Prove liveness. The orchestrator releases claims held by a robot whose
        heartbeat is >10s stale (§4.4) — robot attrition as a 20-line query."""
        self.conn.execute(
            "UPDATE robots SET heartbeat_at = now(),"
            "   pos_x = coalesce(%s, pos_x), pos_y = coalesce(%s, pos_y),"
            "   battery = coalesce(%s, battery), status = coalesce(%s, status)"
            " WHERE id = %s",
            (None if pos is None else pos[0], None if pos is None else pos[1],
             battery, status, robot_id),
        )

    def register_robot(
        self, robot_id: str, role: str, pos: tuple[int, int], battery: int
    ) -> None:
        self.conn.execute(
            "UPSERT INTO robots (id, role, pos_x, pos_y, battery, status, heartbeat_at)"
            " VALUES (%s, %s, %s, %s, %s, 'idle', now())",
            (robot_id, role, pos[0], pos[1], battery),
        )

    def stale_robots(self, seconds: int = 10) -> list[str]:
        """Robots whose heartbeat has lapsed — their claims should be released."""
        rows = self.conn.execute(
            "SELECT id FROM robots WHERE heartbeat_at < now() - %s::interval",
            (f"{seconds} seconds",),
        ).fetchall()
        return [r["id"] for r in rows]

    def log_event(
        self, mission_id: UUID, actor: str, verb: str, detail: dict[str, Any] | None = None
    ) -> None:
        """Append to the mission log. Every §4.7 metric is derived from these, so
        log the transition, not the intention."""
        self.conn.execute(
            "INSERT INTO events (mission_id, actor, verb, detail) VALUES (%s, %s, %s, %s)",
            (mission_id, actor, verb, json.dumps(detail or {})),
        )

    def events(self, mission_id: UUID) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT actor, verb, detail, at FROM events WHERE mission_id = %s ORDER BY at",
            (mission_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _vec(embedding: Sequence[float] | None) -> str | None:
    """CockroachDB takes VECTOR literals as '[a,b,c]' text."""
    return None if embedding is None else "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _belief(r: dict[str, Any]) -> Belief:
    return Belief(
        id=r["id"], kind=r["kind"], pos=(r["pos_x"], r["pos_y"]),
        payload=r["payload"] or {}, confidence=r["confidence"],
        sightings=r["sightings"], robot_id=r["robot_id"], observed_at=r["observed_at"],
    )


def _task(r: dict[str, Any]) -> Task:
    return Task(
        id=r["id"], mission_id=r["mission_id"], kind=r["kind"],
        target=(r["target_x"], r["target_y"]), status=r["status"],
        priority=r["priority"], depends_on=list(r["depends_on"] or []),
        claimed_by=r["claimed_by"],
    )
