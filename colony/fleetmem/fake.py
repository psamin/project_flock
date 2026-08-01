"""In-memory fleetmem — no cluster required.

This is what lanes 2 and 4 build against on day 1 (§5.2). It mirrors
CockroachFleetMem method for method; tests/test_contract.py compares the two
signature by signature and fails if they drift, and tests/test_fleetmem.py runs
the same behavioural suite against both.

Not a toy: it enforces the same rules the real one does, including single-winner
claiming under thread contention and unblock-only-when-all-dependencies-done.
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import UUID, uuid4

from fleetmem.types import BLOCKED, CLAIMED, DONE, OPEN, Belief, Match, Task

MERGE_DISTANCE = 0.18
MERGE_RADIUS_TILES = 5


class FakeFleetMem:
    def __init__(self, dsn: str | None = None):
        self._lock = threading.Lock()  # stands in for serializable isolation
        self._obs: dict[UUID, dict[str, Any]] = {}
        self._tasks: dict[UUID, dict[str, Any]] = {}
        self._robots: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []

    def close(self) -> None:
        pass

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
        with self._lock:
            match = self._find_similar_locked(mission_id, kind, pos, embedding)
            if match is not None:
                row = self._obs[match.belief_id]
                row["sightings"] += 1
                row["confidence"] = min(1.0, row["confidence"] + confidence * 0.1)
                row["observed_at"] = _now()
                return match.belief_id

            obs_id = uuid4()
            self._obs[obs_id] = {
                "id": obs_id, "mission_id": mission_id, "robot_id": robot_id,
                "kind": kind, "pos_x": pos[0], "pos_y": pos[1],
                "payload": payload or {}, "embedding": None if embedding is None else list(embedding),
                "confidence": confidence, "sightings": 1, "observed_at": _now(),
            }
            return obs_id

    def find_similar(
        self,
        mission_id: UUID,
        kind: str,
        pos: tuple[int, int],
        embedding: Sequence[float] | None,
        limit: int = 5,
    ) -> Match | None:
        with self._lock:
            return self._find_similar_locked(mission_id, kind, pos, embedding, limit)

    def _find_similar_locked(
        self,
        mission_id: UUID,
        kind: str,
        pos: tuple[int, int],
        embedding: Sequence[float] | None,
        limit: int = 5,
    ) -> Match | None:
        near = [
            r for r in self._obs.values()
            if r["mission_id"] == mission_id and r["kind"] == kind
            and abs(r["pos_x"] - pos[0]) <= MERGE_RADIUS_TILES
            and abs(r["pos_y"] - pos[1]) <= MERGE_RADIUS_TILES
        ]
        if embedding is None:
            if not near:
                return None
            newest = max(near, key=lambda r: r["observed_at"])
            return Match(newest["id"], distance=0.0)

        scored = [
            (_cosine_distance(embedding, r["embedding"]), r)
            for r in near if r["embedding"] is not None
        ]
        scored.sort(key=lambda pair: pair[0])
        for distance, row in scored[:limit]:
            if distance <= MERGE_DISTANCE:
                return Match(row["id"], distance=distance)
        return None

    def get_beliefs(
        self,
        mission_id: UUID,
        area: tuple[int, int, int, int] | None = None,
        kind: str | None = None,
    ) -> list[Belief]:
        with self._lock:
            out = []
            for r in self._obs.values():
                if r["mission_id"] != mission_id:
                    continue
                if kind is not None and r["kind"] != kind:
                    continue
                if area is not None and not (
                    area[0] <= r["pos_x"] <= area[2] and area[1] <= r["pos_y"] <= area[3]
                ):
                    continue
                out.append(Belief(
                    id=r["id"], kind=r["kind"], pos=(r["pos_x"], r["pos_y"]),
                    payload=r["payload"], confidence=r["confidence"],
                    sightings=r["sightings"], robot_id=r["robot_id"],
                    observed_at=r["observed_at"],
                ))
            return out

    # --- tasks ------------------------------------------------------------

    def create_task(
        self,
        mission_id: UUID,
        kind: str,
        target: tuple[int | None, int | None] = (None, None),
        priority: int = 1,
        depends_on: Sequence[UUID] = (),
    ) -> UUID:
        with self._lock:
            task_id = uuid4()
            deps = list(depends_on)
            self._tasks[task_id] = {
                "id": task_id, "mission_id": mission_id, "kind": kind,
                "target_x": target[0], "target_y": target[1], "priority": priority,
                "status": BLOCKED if deps else OPEN, "depends_on": deps,
                "claimed_by": None,
            }
            return task_id

    def claim_task(self, task_id: UUID, robot_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["status"] != OPEN:
                return False
            task["status"] = CLAIMED
            task["claimed_by"] = robot_id
            return True

    def complete_task(self, task_id: UUID, robot_id: str) -> list[UUID]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["claimed_by"] != robot_id or task["status"] == DONE:
                return []
            task["status"] = DONE

            unblocked = []
            for other in self._tasks.values():
                if other["status"] != BLOCKED or task_id not in other["depends_on"]:
                    continue
                if all(self._tasks[d]["status"] == DONE for d in other["depends_on"]):
                    other["status"] = OPEN
                    unblocked.append(other["id"])
            return unblocked

    def open_tasks(self, mission_id: UUID) -> list[Task]:
        with self._lock:
            rows = [
                t for t in self._tasks.values()
                if t["mission_id"] == mission_id and t["status"] == OPEN
            ]
            rows.sort(key=lambda t: t["priority"], reverse=True)
            return [
                Task(
                    id=t["id"], mission_id=t["mission_id"], kind=t["kind"],
                    target=(t["target_x"], t["target_y"]), status=t["status"],
                    priority=t["priority"], depends_on=list(t["depends_on"]),
                    claimed_by=t["claimed_by"],
                )
                for t in rows
            ]

    # --- robots and log ---------------------------------------------------

    def heartbeat(
        self,
        robot_id: str,
        pos: tuple[int, int] | None = None,
        battery: int | None = None,
        status: str | None = None,
    ) -> None:
        with self._lock:
            robot = self._robots.get(robot_id)
            if robot is None:
                return
            robot["heartbeat_at"] = _now()
            if pos is not None:
                robot["pos_x"], robot["pos_y"] = pos
            if battery is not None:
                robot["battery"] = battery
            if status is not None:
                robot["status"] = status

    def register_robot(
        self, robot_id: str, role: str, pos: tuple[int, int], battery: int
    ) -> None:
        with self._lock:
            self._robots[robot_id] = {
                "id": robot_id, "role": role, "pos_x": pos[0], "pos_y": pos[1],
                "battery": battery, "status": "idle", "heartbeat_at": _now(),
            }

    def stale_robots(self, seconds: int = 10) -> list[str]:
        with self._lock:
            cutoff = _now().timestamp() - seconds
            return [
                r["id"] for r in self._robots.values()
                if r["heartbeat_at"].timestamp() < cutoff
            ]

    def log_event(
        self, mission_id: UUID, actor: str, verb: str, detail: dict[str, Any] | None = None
    ) -> None:
        with self._lock:
            self._events.append({
                "mission_id": mission_id, "actor": actor, "verb": verb,
                "detail": detail or {}, "at": _now(),
            })

    def events(self, mission_id: UUID) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {k: v for k, v in e.items() if k != "mission_id"}
                for e in self._events if e["mission_id"] == mission_id
            ]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - dot / (na * nb)
