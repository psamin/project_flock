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
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from uuid import UUID

from fleetmem.types import (
    BLOCKED,
    CLAIMED,
    DONE,
    IN_PROGRESS,
    OPEN,
    Belief,
    Match,
    MissionMemory,
    Plan,
    Task,
)

MERGE_DISTANCE = 0.18
MERGE_RADIUS_TILES = 5
LEASE_SECONDS = 15  # mirrors fleetmem.client (§4.4)


class FakeFleetMem:
    # Row ids are drawn from a per-instance seeded generator rather than
    # `uuid4()`. The cluster uses `gen_random_uuid()` and always will; the fake
    # is a test double, and a test double that produces different ids on every
    # run makes X2's condition — byte-identical event logs for one seed —
    # impossible to meet even when the fleet behaved identically. Two runs of
    # the same program now produce the same ids.
    #
    # The generator is seeded from a class-level instance counter, not a
    # constant, so two stores alive at once (compare_modes runs two, the X1
    # ablation runs one per robot) never hand out the same id.
    _instances = 0

    def __init__(self, dsn: str | None = None):
        self._lock = threading.Lock()  # stands in for serializable isolation
        self._obs: dict[UUID, dict[str, Any]] = {}
        self._tasks: dict[UUID, dict[str, Any]] = {}
        self._robots: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._plans: list[dict[str, Any]] = []
        self._victims: dict[UUID, dict[str, Any]] = {}
        # Semantic memory. Per-instance, deliberately: the sim builds one store
        # and reuses it across missions, so keying by map is all cross-mission
        # recall needs. A class-level store would instead leak between
        # compare_modes' two runs, which take a factory precisely to stop that.
        self._memories: list[dict[str, Any]] = []
        FakeFleetMem._instances += 1
        self._id_rng = random.Random(FakeFleetMem._instances)

    @classmethod
    def reset_ids(cls) -> None:
        """Restart id allocation, for a test that wants two runs to match.

        A process comparing two runs has to begin each from the same point, or
        the second run's stores are numbered after the first's and every id
        differs for that reason alone.
        """
        cls._instances = 0

    def _new_id(self) -> UUID:
        return UUID(int=self._id_rng.getrandbits(128))

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

            obs_id = self._new_id()
            self._obs[obs_id] = {
                "id": obs_id,
                "mission_id": mission_id,
                "robot_id": robot_id,
                "kind": kind,
                "pos_x": pos[0],
                "pos_y": pos[1],
                "payload": payload or {},
                "embedding": None if embedding is None else list(embedding),
                "confidence": confidence,
                "sightings": 1,
                "observed_at": _now(),
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
            r
            for r in self._obs.values()
            if r["mission_id"] == mission_id
            and r["kind"] == kind
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
            for r in near
            if r["embedding"] is not None
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
                    area[0] <= r["pos_x"] <= area[2]
                    and area[1] <= r["pos_y"] <= area[3]
                ):
                    continue
                out.append(
                    Belief(
                        id=r["id"],
                        kind=r["kind"],
                        pos=(r["pos_x"], r["pos_y"]),
                        payload=r["payload"],
                        confidence=r["confidence"],
                        sightings=r["sightings"],
                        robot_id=r["robot_id"],
                        observed_at=r["observed_at"],
                    )
                )
            return out

    # --- semantic memory --------------------------------------------------

    def remember_mission(
        self,
        mission_id: UUID,
        map_key: str,
        summary: str,
        embedding: Sequence[float] | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> UUID | None:
        with self._lock:
            # Mirrors the client's WHERE NOT EXISTS guard. The contract test
            # only compares signatures, so this parity is on us.
            if any(m["mission_id"] == mission_id for m in self._memories):
                return None
            row = {
                "id": self._new_id(),
                "mission_id": mission_id,
                "map_key": map_key,
                "summary": summary,
                "embedding": list(embedding) if embedding is not None else None,
                "outcome": outcome or {},
                "created_at": _now(),
            }
            self._memories.append(row)
            return row["id"]

    def recall_missions(
        self,
        map_key: str,
        embedding: Sequence[float] | None,
        limit: int = 3,
    ) -> list[MissionMemory]:
        with self._lock:
            rows = [m for m in self._memories if m["map_key"] == map_key]
            if embedding is None:
                ranked = [
                    (0.0, m)
                    for m in sorted(rows, key=lambda m: m["created_at"], reverse=True)
                ]
            else:
                scored = [
                    (_cosine_distance(embedding, m["embedding"]), m)
                    for m in rows
                    if m["embedding"] is not None
                ]
                # Ties break on creation order, never on the row id: a seeded id
                # is still arbitrary, and two equidistant memories decided by it
                # reorder between runs.
                ranked = sorted(scored, key=lambda pair: (pair[0], pair[1]["created_at"]))
            return [
                MissionMemory(
                    id=m["id"],
                    mission_id=m["mission_id"],
                    map_key=m["map_key"],
                    summary=m["summary"],
                    outcome=m["outcome"],
                    distance=dist,
                    created_at=m["created_at"],
                )
                for dist, m in ranked[:limit]
            ]

    # --- tasks ------------------------------------------------------------

    def register_victim(
        self,
        mission_id: UUID,
        pos: tuple[int, int],
        reported_by: str,
        blocked_by: Sequence[tuple[int, int]] = (),
        vitals_deadline: int | None = None,
        priority: int = 5,
    ) -> tuple[UUID, list[UUID]]:
        with self._lock:
            existing = next(
                (
                    v
                    for v in self._victims.values()
                    if v["mission_id"] == mission_id and (v["pos_x"], v["pos_y"]) == pos
                ),
                None,
            )
            if existing is not None:
                # Via the delivery task and out through its dependencies; see
                # the note in CockroachFleetMem.register_victim.
                deliver = next(
                    (
                        t
                        for t in self._tasks.values()
                        if t["mission_id"] == mission_id
                        and t["kind"] == "deliver_kit"
                        and (t["target_x"], t["target_y"]) == pos
                    ),
                    None,
                )
                if deliver is None:
                    return existing["id"], []
                return existing["id"], [*deliver["depends_on"], deliver["id"]]

            victim_id = self._new_id()
            self._victims[victim_id] = {
                "id": victim_id,
                "mission_id": mission_id,
                "pos_x": pos[0],
                "pos_y": pos[1],
                "state": "located",
                "vitals_deadline": vitals_deadline,
                "reported_by": reported_by,
            }
            # The chain is built while the lock is still held. Releasing it here
            # opened a window where a concurrent sighting of the same victim saw
            # the row but not yet its tasks, took the "already registered"
            # branch, and was handed an empty chain — reporting that a victim
            # needs no rescue at all. `_create_task_locked` exists because
            # threading.Lock is not reentrant and create_task would deadlock.
            clears = [
                self._create_task_locked(
                    mission_id, "clear_debris", tile, priority=priority
                )
                for tile in blocked_by
            ]
            deliver = self._create_task_locked(
                mission_id, "deliver_kit", pos, priority=priority, depends_on=clears
            )
            return victim_id, [*clears, deliver]

    def create_task(
        self,
        mission_id: UUID,
        kind: str,
        target: tuple[int | None, int | None] = (None, None),
        priority: int = 1,
        depends_on: Sequence[UUID] = (),
    ) -> UUID:
        with self._lock:
            return self._create_task_locked(
                mission_id, kind, target, priority, depends_on
            )

    def _create_task_locked(
        self,
        mission_id: UUID,
        kind: str,
        target: tuple[int | None, int | None] = (None, None),
        priority: int = 1,
        depends_on: Sequence[UUID] = (),
    ) -> UUID:
        """Caller must already hold `self._lock`."""
        task_id = self._new_id()
        deps = list(depends_on)
        unknown = [d for d in deps if d not in self._tasks]
        if unknown:
            # Matches CockroachFleetMem: see the note on its create_task.
            raise ValueError(f"depends_on refers to unknown task ids: {unknown}")
        self._tasks[task_id] = {
            "id": task_id,
            "mission_id": mission_id,
            "kind": kind,
            "target_x": target[0],
            "target_y": target[1],
            "priority": priority,
            "status": BLOCKED if deps else OPEN,
            "depends_on": deps,
            "claimed_by": None,
            "lease_expires_at": None,
        }
        return task_id

    def claim_task(
        self, task_id: UUID, robot_id: str, lease_seconds: int = LEASE_SECONDS
    ) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or not self._claimable(task):
                return False
            task["status"] = CLAIMED
            task["claimed_by"] = robot_id
            task["lease_expires_at"] = _now() + timedelta(seconds=lease_seconds)
            return True

    @staticmethod
    def _claimable(task: dict[str, Any]) -> bool:
        """Open, or held under a lease that has already lapsed (§4.4)."""
        if task["status"] == OPEN:
            return True
        if task["status"] not in (CLAIMED, IN_PROGRESS):
            return False
        expires = task.get("lease_expires_at")
        # A missing lease counts as expired, matching the client: rows claimed
        # before the v1.1 migration carry no lease, and without this they would
        # stay owned forever with nothing to repair them.
        return expires is None or expires < _now()

    def renew_leases(self, robot_id: str, lease_seconds: int = LEASE_SECONDS) -> int:
        with self._lock:
            renewed = 0
            for task in self._tasks.values():
                if task["claimed_by"] == robot_id and task["status"] in (
                    CLAIMED,
                    IN_PROGRESS,
                ):
                    task["lease_expires_at"] = _now() + timedelta(seconds=lease_seconds)
                    renewed += 1
            return renewed

    def release_task(self, task_id: UUID) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["status"] not in (CLAIMED, IN_PROGRESS):
                return
            task["status"] = OPEN
            task["claimed_by"] = None
            task["lease_expires_at"] = None

    def complete_task(self, task_id: UUID, robot_id: str) -> list[UUID] | None:
        """None when the completion did not apply; see CockroachFleetMem."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["claimed_by"] != robot_id or task["status"] == DONE:
                return None
            task["status"] = DONE
            task["lease_expires_at"] = None  # a finished task is not abandoned work

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
            # Expired leases count as available — recovery needs no separate step.
            rows = [
                t
                for t in self._tasks.values()
                if t["mission_id"] == mission_id and self._claimable(t)
            ]
            rows.sort(key=lambda t: t["priority"], reverse=True)
            return [
                Task(
                    id=t["id"],
                    mission_id=t["mission_id"],
                    kind=t["kind"],
                    target=(t["target_x"], t["target_y"]),
                    status=t["status"],
                    priority=t["priority"],
                    depends_on=list(t["depends_on"]),
                    claimed_by=t["claimed_by"],
                    lease_expires_at=t["lease_expires_at"],
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
        lease_seconds: int = LEASE_SECONDS,
        renew: bool = True,
    ) -> None:
        with self._lock:
            robot = self._robots.get(robot_id)
            if robot is not None:
                robot["heartbeat_at"] = _now()
                if pos is not None:
                    robot["pos_x"], robot["pos_y"] = pos
                if battery is not None:
                    robot["battery"] = battery
                if status is not None:
                    robot["status"] = status
        # Renewal happens even for an unregistered robot, matching the client:
        # the two statements there are independent, and a robot holding tasks
        # must not lose them because its row is missing.
        if renew:
            self.renew_leases(robot_id, lease_seconds)

    def register_robot(
        self, robot_id: str, role: str, pos: tuple[int, int], battery: int
    ) -> None:
        with self._lock:
            self._robots[robot_id] = {
                "id": robot_id,
                "role": role,
                "pos_x": pos[0],
                "pos_y": pos[1],
                "battery": battery,
                "status": "idle",
                "heartbeat_at": _now(),
            }

    def stale_robots(self, seconds: int = 10) -> list[str]:
        """Lost-marking for the UI only — never a recovery path. See the client."""
        with self._lock:
            cutoff = _now().timestamp() - seconds
            return [
                r["id"]
                for r in self._robots.values()
                # `<=`, because a strict comparison makes this flaky at
                # `seconds=0`: two `datetime.now()` calls in the same clock tick
                # come back equal and a robot that has said nothing since this
                # instant reads as fresh. CockroachDB never hits it — statement
                # timestamps advance — so the fake was the only one failing, at
                # random, on whichever machine happened to be quick.
                if r["heartbeat_at"].timestamp() <= cutoff
            ]

    def log_plan(
        self,
        mission_id: UUID,
        robot_id: str,
        trigger: str,
        chosen: dict[str, Any],
        rationale: str,
        based_on: Sequence[UUID] = (),
    ) -> UUID:
        with self._lock:
            plan_id = self._new_id()
            self._plans.append(
                {
                    "id": plan_id,
                    "mission_id": mission_id,
                    "robot_id": robot_id,
                    "trigger": trigger,
                    "chosen": chosen,
                    "rationale": rationale,
                    "based_on": list(based_on),
                    "at": _now(),
                }
            )
            return plan_id

    def plans_for(self, mission_id: UUID, robot_id: str | None = None) -> list[Plan]:
        with self._lock:
            return [
                Plan(
                    id=p["id"],
                    mission_id=p["mission_id"],
                    robot_id=p["robot_id"],
                    trigger=p["trigger"],
                    chosen=p["chosen"],
                    rationale=p["rationale"],
                    based_on=list(p["based_on"]),
                    at=p["at"],
                )
                for p in self._plans
                if p["mission_id"] == mission_id
                and (robot_id is None or p["robot_id"] == robot_id)
            ]

    def log_event(
        self,
        mission_id: UUID,
        actor: str,
        verb: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.log_events(mission_id, [(actor, verb, detail)])

    def log_events(
        self,
        mission_id: UUID,
        rows: Sequence[tuple[str, str, dict[str, Any] | None]],
    ) -> None:
        with self._lock:
            for actor, verb, detail in rows:
                self._events.append(
                    {
                        "mission_id": mission_id,
                        "actor": actor,
                        "verb": verb,
                        "detail": detail or {},
                        "at": _now(),
                    }
                )

    def events(self, mission_id: UUID) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {k: v for k, v in e.items() if k != "mission_id"}
                for e in self._events
                if e["mission_id"] == mission_id
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
