"""CockroachDB-backed fleet memory — the shared brain (PRD §4.2, §4.4).

This module is the SDK contract other lanes build against (§5.2 contract 1),
frozen Aug 3. `fleetmem.fake.FakeFleetMem` mirrors every signature here so lanes
2 and 4 can work without a cluster; tests/test_contract.py fails if the two drift
apart.
"""

from __future__ import annotations

import functools
import json
import os
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from fleetmem.types import (
    BLOCKED,
    DONE,
    INTERVENTION_PREFIX,
    OPEN,
    Belief,
    Hazard,
    Match,
    Lesson,
    Plan,
    Task,
)

DEFAULT_DSN = "postgresql://root@localhost:26257/colony?sslmode=disable"


def resolve_dsn(dsn: str | None = None) -> str:
    """Where fleet memory lives: explicit argument, else `COLONY_DSN`, else local.

    One function because every connection in the process has to agree. A second
    opinion is not a second connection to the same cluster, it is a connection to
    a different one — and against the local dev cluster both happen to be right,
    so the disagreement only surfaces on Cloud.
    """
    return dsn or os.environ.get("COLONY_DSN", DEFAULT_DSN)


# Cosine distance below which two observations are the same belief (§4.2 step 3
# states the gate as >=0.82 cosine *similarity*; distance = 1 - similarity).
MERGE_DISTANCE = 0.18
MERGE_RADIUS_TILES = 5

# Lease defaults (§4.4). 15s TTL renewed every 5s means three renewals can be
# missed before a task frees itself; the longest atomic action (a rubble-heavy
# clear, 6 ticks at 4 Hz = 1.5s) sits comfortably inside one lease.
LEASE_SECONDS = 15
RENEW_SECONDS = 5

# CockroachDB runs SERIALIZABLE and aborts transactions that would violate it,
# with SQLSTATE 40001. That is not an error condition, it is the contract: the
# client is expected to replay the transaction. Two scouts reporting the same
# victim at the same instant is exactly the contention that triggers it, so the
# reconcile gate and the chain it creates must both be replayable.
MAX_RETRIES = 5


def retry_on_serialization_failure(method):
    """Replay a transactional method when CockroachDB asks us to."""

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        for attempt in range(MAX_RETRIES):
            try:
                return method(*args, **kwargs)
            except psycopg.errors.SerializationFailure:
                if attempt == MAX_RETRIES - 1:
                    raise
        raise AssertionError("unreachable")

    return wrapper


class CockroachFleetMem:
    def __init__(self, dsn: str | None = None):
        """Connect to fleet memory.

        `COLONY_DSN` overrides the local dev cluster, which is how the same code
        runs against CockroachDB Cloud (§4.6: the primary fleet memory is Cloud;
        the local recipe and the 3-node chaos rig are for development and the
        node-kill segment). Explicit `dsn=` still wins, so per-robot credential
        tests can open one connection per identity.
        """
        self.conn = psycopg.connect(
            resolve_dsn(dsn),
            autocommit=True,
            row_factory=dict_row,
        )

    def close(self) -> None:
        self.conn.close()

    # --- observations -----------------------------------------------------

    @retry_on_serialization_failure
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
                    mission_id,
                    robot_id,
                    kind,
                    pos[0],
                    pos[1],
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
        box = (
            x - MERGE_RADIUS_TILES,
            y - MERGE_RADIUS_TILES,
            x + MERGE_RADIUS_TILES,
            y + MERGE_RADIUS_TILES,
        )

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
        # That correctness costs the index, and the comment that used to sit here
        # claimed the opposite. Measured on 1047 rows: this plan is a FULL SCAN.
        # `kind` and the pos box are not prefix columns of obs_embedding_idx, and
        # v26.2 will not serve an approximate top-k it then has to filter — it
        # declines the index outright rather than risk dropping a real match.
        # `embedding IS NOT NULL` disables it a second, independent time.
        #
        # The scan is the correct trade here, not a bug to tune away: exact
        # results on a 5-tile box beat an approximate scan that merges two
        # victims into one. `test_the_reconcile_gate_query_uses_the_index` is a
        # strict xfail holding that line. Tactical recall (`recall_lessons`) is
        # where the vector index is genuinely load-bearing.
        rows = self.conn.execute(
            "SELECT id, embedding <=> %s AS distance"
            "  FROM observations"
            " WHERE mission_id = %s AND embedding IS NOT NULL"
            "   AND kind = %s"
            "   AND pos_x BETWEEN %s AND %s AND pos_y BETWEEN %s AND %s"
            " ORDER BY embedding <=> %s LIMIT %s",
            (
                _vec(embedding),
                mission_id,
                kind,
                box[0],
                box[2],
                box[1],
                box[3],
                _vec(embedding),
                limit,
            ),
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

    # --- semantic memory --------------------------------------------------

    def remember_lesson(
        self,
        mission_id: UUID,
        situation: str,
        lesson: str,
        embedding: Sequence[float] | None = None,
        evidence: dict[str, Any] | None = None,
        confidence: float = 0.5,
    ) -> UUID:
        """Write down a tactic this mission learned.

        `situation` is what gets embedded — retrieval asks what the current
        moment resembles, so the vector has to describe conditions rather than
        the advice. Storing the lesson text unembedded also means a lesson can
        be reworded without moving in the index.
        """
        row = self.conn.execute(
            "INSERT INTO mission_memories"
            "  (mission_id, situation, lesson, embedding, evidence, confidence)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                mission_id,
                situation,
                lesson,
                _vec(embedding),
                json.dumps(evidence or {}),
                confidence,
            ),
        ).fetchone()
        return row["id"]

    def recall_lessons(
        self,
        embedding: Sequence[float] | None,
        limit: int = 3,
    ) -> list[Lesson]:
        """The tactics most like the situation described by `embedding`.

        The second place CockroachDB's vector indexing carries real weight, and
        the only search in the SDK that crosses missions *and* maps. There is no
        scope argument on purpose: a lesson learned clearing rubble on one map
        is exactly what should surface while clearing rubble on another, and any
        filter here would partition the knowledge this is built to generalise.

        Without an embedding it degrades to the most recently learned. Recall
        still works with no Bedrock credentials; it just stops being semantic.
        """
        if embedding is None:
            rows = self.conn.execute(
                "SELECT *, 0.0 AS distance FROM mission_memories"
                " ORDER BY created_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
        else:
            vec = _vec(embedding)
            # No `WHERE embedding IS NOT NULL`, and that is not an oversight.
            # Measured against v26.2: adding it moves the plan from
            # `vector search` on mm_situation_idx to a FULL SCAN of the primary
            # index. Only filters matching a prefix column keep the vector index
            # engaged, and this one matches nothing — so the guard that looks
            # like hygiene silently costs the capability.
            #
            # It is not needed anyway: `<=>` against a NULL embedding is NULL,
            # and NULLs sort last on ASC, so an unembedded lesson falls off the
            # end of the top-k on its own. `_lesson` still guards the mapper, in
            # case one is the only row there is.
            rows = self.conn.execute(
                "SELECT *, embedding <=> %s AS distance FROM mission_memories"
                " ORDER BY embedding <=> %s LIMIT %s",
                (vec, vec, limit),
            ).fetchall()
        return [_lesson(r) for r in rows if r.get("embedding") is not None]

    def mark_recalled(self, lesson_ids: Sequence[UUID]) -> None:
        """Count a retrieval. A lesson nothing ever retrieves is dead weight,
        and this is what lets the console say which tactics the fleet actually
        leans on rather than merely which ones it wrote down."""
        if not lesson_ids:
            return
        self.conn.execute(
            "UPDATE mission_memories SET times_recalled = times_recalled + 1"
            " WHERE id = ANY(%s)",
            (list(lesson_ids),),
        )

    # --- tasks ------------------------------------------------------------

    @retry_on_serialization_failure
    def register_victim(
        self,
        mission_id: UUID,
        pos: tuple[int, int],
        reported_by: str,
        blocked_by: Sequence[tuple[int, int]] = (),
        vitals_deadline: int | None = None,
        priority: int = 5,
    ) -> tuple[UUID, list[UUID]]:
        """Record a victim and the task chain that reaches them (§4.2 step 3).

        Returns (victim_id, task_ids). One transaction: the victim row, a
        `clear_debris` task per blocking tile, and a `deliver_kit` that
        `depends_on` all of them. That gating is the handoff — the medic's task
        is not claimable until the last lifter finishes, and nobody has to
        message anybody.

        Idempotent per position: a second sighting of the same victim returns the
        existing chain rather than dispatching the fleet twice.
        """
        with self.conn.transaction():
            existing = self.conn.execute(
                "SELECT id FROM victims WHERE mission_id = %s AND pos_x = %s AND pos_y = %s",
                (mission_id, pos[0], pos[1]),
            ).fetchone()
            if existing is not None:
                # Found via the delivery task, then out through its
                # dependencies: the clears target the *blocking* tiles, not the
                # victim's, so looking them up by position finds nothing and the
                # caller would be handed a chain missing its own prerequisites.
                deliver = self.conn.execute(
                    "SELECT id, depends_on FROM tasks"
                    " WHERE mission_id = %s AND kind = 'deliver_kit'"
                    "   AND target_x = %s AND target_y = %s",
                    (mission_id, pos[0], pos[1]),
                ).fetchone()
                if deliver is None:
                    return existing["id"], []
                return existing["id"], [*(deliver["depends_on"] or []), deliver["id"]]

            victim = self.conn.execute(
                "INSERT INTO victims (mission_id, pos_x, pos_y, state, vitals_deadline,"
                "   reported_by) VALUES (%s, %s, %s, 'located', %s, %s) RETURNING id",
                (mission_id, pos[0], pos[1], vitals_deadline, reported_by),
            ).fetchone()["id"]

            clears = [
                self.create_task(mission_id, "clear_debris", tile, priority=priority)
                for tile in blocked_by
            ]
            deliver = self.create_task(
                mission_id, "deliver_kit", pos, priority=priority, depends_on=clears
            )
            return victim, [*clears, deliver]

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
            (
                mission_id,
                kind,
                target[0],
                target[1],
                priority,
                BLOCKED if deps else OPEN,
                deps or None,
            ),
        ).fetchone()
        return row["id"]

    def claim_task(
        self, task_id: UUID, robot_id: str, lease_seconds: int = LEASE_SECONDS
    ) -> bool:
        """Attempt to claim a task. True iff this robot won it.

        The judge-friendly SQL (§4.4). Under serializable isolation exactly one
        concurrent caller wins; losers get zero rows back rather than an error,
        so no retry logic is needed.

        A task is claimable when it is `open` **or** when its lease has expired —
        which is what makes recovery lease-native (FR-5). A robot that dies
        mid-task has its work taken over by the next claim attempt, with no
        sweep, no watchdog and nothing on the recovery path but this query.

        Expiry is compared against the database's now(), never a robot's clock,
        so clock skew cannot manufacture a false takeover.
        """
        row = self.conn.execute(
            "UPDATE tasks"
            "   SET status = 'claimed', claimed_by = %s, claimed_at = now(),"
            "       lease_expires_at = now() + %s::interval"
            " WHERE id = %s"
            "   AND (status = 'open'"
            "        OR (status IN ('claimed', 'in_progress')"
            "            AND (lease_expires_at IS NULL OR lease_expires_at < now())))"
            " RETURNING id",
            (robot_id, f"{lease_seconds} seconds", task_id),
        ).fetchone()
        return row is not None

    def renew_leases(self, robot_id: str, lease_seconds: int = LEASE_SECONDS) -> int:
        """Push out the lease on every task this robot holds; returns the count.

        Called from heartbeat() every RENEW_SECONDS, so three renewals can be
        missed before a lease lapses (§4.4).
        """
        rows = self.conn.execute(
            "UPDATE tasks SET lease_expires_at = now() + %s::interval"
            " WHERE claimed_by = %s AND status IN ('claimed', 'in_progress')"
            " RETURNING id",
            (f"{lease_seconds} seconds", robot_id),
        ).fetchall()
        return len(rows)

    def release_task(self, task_id: UUID) -> None:
        """Hand a task back to the pool: status -> open, lease cleared (§4.4).

        The explicit counterpart to lease expiry — used when an aftershock
        invalidates in-flight work (FR-7) or an agent gives up.
        """
        self.conn.execute(
            "UPDATE tasks SET status = 'open', claimed_by = NULL,"
            "   claimed_at = NULL, lease_expires_at = NULL"
            " WHERE id = %s AND status IN ('claimed', 'in_progress')",
            (task_id,),
        )

    @retry_on_serialization_failure
    def complete_task(self, task_id: UUID, robot_id: str) -> list[UUID] | None:
        """Mark a task done and unblock its dependents in the SAME transaction
        (§4.4). Returns the ids of tasks that became open as a result, or
        **None if the completion did not apply** — the task was already done, or
        its lease lapsed and another robot now owns it.

        The None/[] distinction matters: [] means "completed, nothing was
        waiting on it", and a caller that cannot tell the two apart logs a
        completion for work it did not finish, inflating every §4.7 metric
        derived from the event log.

        A blocked task opens only when *every* dependency is done, so this checks
        the whole `depends_on` array rather than just the task that finished —
        the scout→lifter→lifter→medic chain in §3.3 has a task with two.
        """
        with self.conn.transaction():
            # Clearing the lease on completion keeps a finished task from ever
            # looking like abandoned work to the takeover query.
            done = self.conn.execute(
                "UPDATE tasks SET status = %s, done_at = now(), lease_expires_at = NULL"
                " WHERE id = %s AND claimed_by = %s AND status != %s RETURNING id",
                (DONE, task_id, robot_id, DONE),
            ).fetchone()
            if done is None:
                return None
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
        """Claimable tasks, for the orchestrator's allocation pass (§4.4).

        Includes tasks whose lease has expired: to the allocator, work abandoned
        by a dead robot is simply available again. This is the whole of the
        recovery path — there is no separate reassignment step.
        """
        rows = self.conn.execute(
            "SELECT * FROM tasks"
            " WHERE mission_id = %s"
            "   AND (status = %s"
            "        OR (status IN ('claimed', 'in_progress')"
            "            AND (lease_expires_at IS NULL OR lease_expires_at < now())))"
            " ORDER BY priority DESC",
            (mission_id, OPEN),
        ).fetchall()
        return [_task(r) for r in rows]

    # --- hazards and operator interventions -------------------------------

    def record_intervention(
        self,
        mission_id: UUID,
        kind: str,
        area: dict[str, Any],
        severity: int = 1,
        caused_by: str = "operator",
    ) -> UUID:
        """Write an operator's disruption to fleet memory (issue #22).

        This is the whole of the operator's channel. The console writes a row;
        the fleet finds out the way it finds out about everything else. Nothing
        reaches into the sim, and there is no path from a person to a robot that
        does not go through this table — which is the same claim the project
        makes about robot-to-robot messaging, held to for the operator too.

        Returning before anything has been applied is deliberate: the caller is
        an HTTP handler, and the disruption becomes real when the listener picks
        the row up, not when the request returns.
        """
        row = self.conn.execute(
            "INSERT INTO hazards (mission_id, kind, area, severity, active)"
            " VALUES (%s, %s, %s, %s, true) RETURNING id",
            (
                mission_id,
                f"{INTERVENTION_PREFIX}{kind}",
                json.dumps({**area, "caused_by": caused_by}),
                severity,
            ),
        ).fetchone()
        return row["id"]

    def active_hazards(
        self, mission_id: UUID, interventions_only: bool = False
    ) -> list[Hazard]:
        """Live hazards for this mission, newest last.

        Read by the console panel and by the fake's listener. Agents do **not**
        call this: a robot learns what is in its way by looking at the world,
        and a query that hands it the operator's tile list would be ground truth
        arriving without anyone sensing it (§4.8).
        """
        sql = "SELECT * FROM hazards WHERE mission_id = %s AND active"
        params: list[Any] = [mission_id]
        if interventions_only:
            sql += " AND kind LIKE %s"
            params.append(f"{INTERVENTION_PREFIX}%")
        rows = self.conn.execute(sql, params).fetchall()
        return [_hazard(r) for r in rows]

    # --- robots and log ---------------------------------------------------

    def heartbeat(
        self,
        robot_id: str,
        pos: tuple[int, int] | None = None,
        battery: int | None = None,
        status: str | None = None,
        lease_seconds: int = LEASE_SECONDS,
        renew: bool = True,
    ) -> None:
        """Report status **and renew every lease this robot holds** (§4.3, §4.4).

        The renewal is the load-bearing half. `robots.heartbeat_at` now exists
        only so the orchestrator can mark a robot `lost` for the UI and event
        log; nothing reads it to recover work.

        The two are separate statements — two round trips — and they are wanted
        on different cadences: the status write feeds a 10s staleness window and
        the renewal a 15s lease. `renew=False` sends the status write alone, so
        a caller on the faster cadence does not pay for the slower one. Against
        a Cloud cluster at ~40ms RTT the pair was over half the tick budget.
        """
        self.conn.execute(
            "UPDATE robots SET heartbeat_at = now(),"
            "   pos_x = coalesce(%s, pos_x), pos_y = coalesce(%s, pos_y),"
            "   battery = coalesce(%s, battery), status = coalesce(%s, status)"
            " WHERE id = %s",
            (
                None if pos is None else pos[0],
                None if pos is None else pos[1],
                battery,
                status,
                robot_id,
            ),
        )
        if renew:
            self.renew_leases(robot_id, lease_seconds)

    def register_robot(
        self, robot_id: str, role: str, pos: tuple[int, int], battery: int
    ) -> None:
        self.conn.execute(
            "UPSERT INTO robots (id, role, pos_x, pos_y, battery, status, heartbeat_at)"
            " VALUES (%s, %s, %s, %s, %s, 'idle', now())",
            (robot_id, role, pos[0], pos[1], battery),
        )

    def stale_robots(self, seconds: int = 10) -> list[str]:
        """Robots whose heartbeat has lapsed, for marking them `lost` in the UI
        and event log.

        Deliberately **not** a recovery mechanism (§4.4, v3.1): their tasks are
        already reclaimable the moment the leases expire, whether or not anyone
        runs this query. Do not build release logic on top of it.
        """
        rows = self.conn.execute(
            "SELECT id FROM robots WHERE heartbeat_at < now() - %s::interval",
            (f"{seconds} seconds",),
        ).fetchall()
        return [r["id"] for r in rows]

    def log_plan(
        self,
        mission_id: UUID,
        robot_id: str,
        trigger: str,
        chosen: dict[str, Any],
        rationale: str,
        based_on: Sequence[UUID] = (),
        recalled_from: Sequence[UUID] = (),
    ) -> UUID:
        """Record a decision and the memories that drove it (FR-17, §4.0).

        `based_on` is the ids of the belief rows that were in the prompt digest.
        Storing them is what turns "the robot said it was going to the office"
        into a traceable answer to "why?" — the commander console joins these
        back to `observations`, and clicking a robot in the UI shows rationale
        plus sources.

        `recalled_from` is the same idea for the other kind of memory: the
        tactics retrieved from `mission_memories` that were in the prompt. Two
        columns rather than one because they resolve against two tables, and an
        id in the wrong one resolves to nothing at all — which turns a decision
        trace back into a plausible story, quietly.
        """
        row = self.conn.execute(
            "INSERT INTO plans"
            "  (mission_id, robot_id, trigger, chosen, rationale, based_on, recalled_from)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                mission_id,
                robot_id,
                trigger,
                json.dumps(chosen),
                rationale,
                list(based_on) or None,
                list(recalled_from) or None,
            ),
        ).fetchone()
        return row["id"]

    def plans_for(self, mission_id: UUID, robot_id: str | None = None) -> list[Plan]:
        """Decision history, newest last. The commander console's raw material."""
        sql = "SELECT * FROM plans WHERE mission_id = %s"
        params: list[Any] = [mission_id]
        if robot_id is not None:
            sql += " AND robot_id = %s"
            params.append(robot_id)
        rows = self.conn.execute(sql + " ORDER BY at", params).fetchall()
        return [_plan(r) for r in rows]

    def log_event(
        self,
        mission_id: UUID,
        actor: str,
        verb: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append to the mission log. Every §4.7 metric is derived from these, so
        log the transition, not the intention."""
        self.log_events(mission_id, [(actor, verb, detail)])

    def log_events(
        self,
        mission_id: UUID,
        rows: Sequence[tuple[str, str, dict[str, Any] | None]],
    ) -> None:
        """Append many events in one round trip.

        A tick emits several events and they were inserted one statement at a
        time, which on a Cloud cluster is several times the latency of the
        insert itself. The rows are independent appends to the same log, so
        nothing about the order or the content changes by sending them together.
        """
        if not rows:
            return
        values = ", ".join(["(%s, %s, %s, %s)"] * len(rows))
        params: list[Any] = []
        for actor, verb, detail in rows:
            params += [mission_id, actor, verb, json.dumps(detail or {})]
        self.conn.execute(
            f"INSERT INTO events (mission_id, actor, verb, detail) VALUES {values}",
            params,
        )

    def events(self, mission_id: UUID) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT actor, verb, detail, at FROM events WHERE mission_id = %s ORDER BY at",
            (mission_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _vec(embedding: Sequence[float] | None) -> str | None:
    """CockroachDB takes VECTOR literals as '[a,b,c]' text."""
    return (
        None
        if embedding is None
        else "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    )


def _belief(r: dict[str, Any]) -> Belief:
    return Belief(
        id=r["id"],
        kind=r["kind"],
        pos=(r["pos_x"], r["pos_y"]),
        payload=r["payload"] or {},
        confidence=r["confidence"],
        sightings=r["sightings"],
        robot_id=r["robot_id"],
        observed_at=r["observed_at"],
    )


def _lesson(r: dict[str, Any]) -> Lesson:
    return Lesson(
        id=r["id"],
        mission_id=r.get("mission_id"),
        situation=r["situation"] or "",
        lesson=r["lesson"] or "",
        evidence=r["evidence"] or {},
        confidence=float(r["confidence"] or 0.0),
        times_recalled=int(r["times_recalled"] or 0),
        distance=float(r["distance"] or 0.0),
        created_at=r.get("created_at"),
    )


def _hazard(r: dict[str, Any]) -> Hazard:
    return Hazard(
        id=r["id"],
        mission_id=r.get("mission_id"),
        kind=r["kind"] or "",
        area=r["area"] or {},
        severity=int(r["severity"] or 1),
        active=bool(r["active"]),
    )


def _plan(r: dict[str, Any]) -> Plan:
    return Plan(
        id=r["id"],
        mission_id=r["mission_id"],
        robot_id=r["robot_id"],
        trigger=r["trigger"],
        chosen=r["chosen"] or {},
        rationale=r["rationale"] or "",
        based_on=list(r["based_on"] or []),
        recalled_from=list(r.get("recalled_from") or []),
        at=r["at"],
    )


def _task(r: dict[str, Any]) -> Task:
    return Task(
        id=r["id"],
        mission_id=r["mission_id"],
        kind=r["kind"],
        target=(r["target_x"], r["target_y"]),
        status=r["status"],
        priority=r["priority"],
        depends_on=list(r["depends_on"] or []),
        claimed_by=r["claimed_by"],
        lease_expires_at=r.get("lease_expires_at"),
    )
