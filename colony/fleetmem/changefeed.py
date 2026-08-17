"""Changefeed handoffs — the P1 half of §4.4's "handoff triggers".

§4.4: "MVP polls open tasks at 1 Hz. P1 swaps in a CRDB changefeed on `tasks`
→ orchestrator/agents wake instantly. Same contract, faster push."

A **core** changefeed (`EXPERIMENTAL CHANGEFEED FOR`), which streams rows back
to the SQL session rather than to an external sink. That choice matters: sink
changefeeds are a CCL feature needing an enterprise licence and somewhere to
put the messages, and neither is something a hackathon demo should depend on.
A core changefeed runs on the free single-node dev cluster and on the Cloud
free tier alike.

**Task handoffs still do not depend on this.** The 1 Hz poll in `open_tasks`
is the path every agent takes for claiming, and §4.4 is explicit that recovery
is lease-native either way — for tasks this remains a latency optimisation
rather than a mechanism, which is why it is still P1 and still a separate
module rather than a change to the frozen SDK surface (§5.2).

**Operator interventions do** (issue #22). `HazardFeed` is how a disruption an
operator wrote reaches a running mission, and there is no second path: the
console writes a `hazards` row and the fleet finds out the way it finds out
about everything else. The 0.09-0.11s delivery measured below is the difference
between a button that responds and one that looks broken, so here the feed is
the mechanism. `sim.interventions.InterventionWatch` still degrades to polling
when the feed will not start, because a cluster with rangefeeds off should cost
the demo latency rather than the feature.

Three things the spike found, each of which would have cost an afternoon:

1. **`kv.rangefeed.enabled` is off by default.** Without it the statement fails
   outright. `ensure_enabled()` turns it on; `make dev` now does too.
2. **A changefeed backfills the whole table first.** The default initial scan
   replays every row that already exists — thousands, on a cluster that has run
   the suite a few times — before it reaches anything live. `no_initial_scan`
   is not an optimisation here, it is the difference between working and not.
3. **A feed carries every write to a row, not just the interesting one.** A
   task created `blocked` and later unblocked arrives twice. A listener that
   acts on the first change it sees for a task wakes on the *creation* and
   concludes the task is not claimable — the exact opposite of the handoff it
   was waiting for. Filter on the transition, never on the id.

Measured on the dev cluster: an unblock reaches a listener in **0.09–0.11s**,
against a 1 Hz poll's 0.5s average and 1.0s worst case. So the P1 swap is worth
making — roughly 5x on the mean and 10x on the tail — but it is still an
optimisation of a mechanism that already works, which is why it stays P1.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from fleetmem.client import resolve_dsn
from fleetmem.types import INTERVENTION_PREFIX, OPEN

# How often the feed emits a resolved timestamp. Only a liveness signal for us —
# we act on rows, not on resolved marks — so it is long enough not to be noise.
RESOLVED_EVERY = "10s"


@dataclass(frozen=True)
class TaskChange:
    """One task row as the feed delivered it."""

    task_id: UUID
    mission_id: UUID | None
    kind: str
    status: str
    claimed_by: str | None

    @property
    def is_unblock(self) -> bool:
        """Whether this change makes the task claimable.

        `open` covers both halves of §4.4's lifecycle that matter to a waiting
        robot: a `blocked` task whose last dependency just completed, and a task
        explicitly released back to the pool. An expired *lease* is not here —
        nothing writes a row when a lease lapses, which is precisely why the
        claiming query checks the clock instead of waiting to be told.
        """
        return self.status == OPEN


def ensure_enabled(conn: Any) -> None:
    """Turn on rangefeeds, without which a changefeed cannot start.

    Off by default on a fresh self-hosted cluster. Failing here is better than
    failing inside the feed thread, where the only symptom is that no wakeups
    arrive.

    On CockroachDB Cloud the setting is operator-owned and already on, and the
    SET is rejected outright ("only settable by the operator"). That rejection
    says the cluster is managed, not that rangefeeds are off, so it is not a
    reason to fail — the feed still has to start, and that is the real check.
    """
    try:
        conn.execute("SET CLUSTER SETTING kv.rangefeed.enabled = true")
    except psycopg.errors.InsufficientPrivilege:
        pass


@dataclass(frozen=True)
class HazardChange:
    """One hazard row as the feed delivered it (issue #22).

    Operator interventions ride in `hazards` under an `intervention:` kind, so
    this is how a disruption an operator caused reaches the fleet: a row lands,
    the feed carries it, and the listener applies it. There is no other path
    from a person to a running mission.
    """

    hazard_id: UUID
    mission_id: UUID | None
    kind: str
    area: dict[str, Any]
    severity: int
    active: bool

    @property
    def is_intervention(self) -> bool:
        return self.kind.startswith(INTERVENTION_PREFIX)

    @property
    def intervention_kind(self) -> str:
        return self.kind[len(INTERVENTION_PREFIX) :] if self.is_intervention else ""


def _after(row: dict[str, Any]) -> dict[str, Any] | None:
    """The post-image of a feed row, or None if there is not one.

    Resolved-timestamp messages arrive on the same stream with a null table, and
    a delete arrives with `after: null`. Neither is a row becoming interesting.
    """
    if not row.get("table") or not row.get("value"):
        return None
    return json.loads(row["value"]).get("after") or None


def _parse(row: dict[str, Any]) -> TaskChange | None:
    """One feed row -> a TaskChange, or None if it carries no post-image."""
    after = _after(row)
    if after is None:
        return None
    return TaskChange(
        task_id=UUID(after["id"]),
        mission_id=UUID(after["mission_id"]) if after.get("mission_id") else None,
        kind=after.get("kind", ""),
        status=after.get("status", ""),
        claimed_by=after.get("claimed_by"),
    )


def _parse_hazard(row: dict[str, Any]) -> HazardChange | None:
    """One feed row -> a HazardChange, or None if it carries no post-image."""
    after = _after(row)
    if after is None:
        return None
    area = after.get("area") or {}
    if isinstance(area, str):  # JSONB arrives as text on some driver paths
        area = json.loads(area)
    return HazardChange(
        hazard_id=UUID(after["id"]),
        mission_id=UUID(after["mission_id"]) if after.get("mission_id") else None,
        kind=after.get("kind", ""),
        area=area,
        severity=int(after.get("severity") or 1),
        active=bool(after.get("active", True)),
    )


class _RowFeed:
    """Watches one table on its own connection and hands rows to a queue.

    A background thread, because the statement never returns — it is a stream,
    and the caller is a tick loop that cannot block on one. The queue is the
    seam: `poll()` is what an orchestrator would call where it currently calls
    `open_tasks()`, which is what §4.4 means by "same contract".

    Subclasses supply the table and the row parser. Extracted from `TaskFeed`
    when interventions needed the same machinery over `hazards` (issue #22) —
    the thread lifecycle, the `no_initial_scan` rule and the mission filter are
    identical for both, and a second copy of them would be a second place for
    the three lessons in this module's docstring to be forgotten.
    """

    TABLE = ""

    @staticmethod
    def _parse_row(row: dict[str, Any]) -> Any | None:
        raise NotImplementedError

    def __init__(
        self,
        mission_id: UUID | None = None,
        dsn: str | None = None,
        resolved: str = RESOLVED_EVERY,
    ) -> None:
        self.mission_id = mission_id
        self.dsn = resolve_dsn(dsn)
        self.resolved = resolved
        self.queue: queue.Queue[Any] = queue.Queue()
        self.error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = threading.Event()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def start(self, timeout: float = 10.0):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._started.wait(timeout)
        if self.error is not None:
            raise self.error
        return self

    def _run(self) -> None:
        try:
            conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        except Exception as exc:  # noqa: BLE001 - reported through .error
            self.error = exc
            self._started.set()
            return
        try:
            with conn.cursor() as cur:
                self._started.set()
                # `no_initial_scan` is load-bearing: without it the feed replays
                # every existing row before reaching anything live, and on a
                # cluster that has run the suite a few times that is thousands
                # of rows of history ahead of the first real wakeup.
                stream = cur.stream(
                    f"EXPERIMENTAL CHANGEFEED FOR {self.TABLE} "
                    f"WITH no_initial_scan, resolved='{self.resolved}'"
                )
                for row in stream:
                    if self._stop.is_set():
                        return
                    change = self._parse_row(row)
                    if change is None:
                        continue
                    # Core changefeeds carry the whole table; scoping to one
                    # mission is the caller's job. Doing it here keeps a demo
                    # from waking on another mission's tasks.
                    if self.mission_id and change.mission_id != self.mission_id:
                        continue
                    self.queue.put(change)
        except Exception as exc:  # noqa: BLE001 - reported through .error
            if not self._stop.is_set():
                self.error = exc
        finally:
            self._started.set()
            conn.close()

    def poll(self, timeout: float = 0.0) -> list[Any]:
        """Everything waiting, optionally blocking up to `timeout` for the first.

        Returns a list rather than one change so a tick loop drains the queue in
        one call — the alternative is a robot handling one handoff per tick
        while three more sit in a queue behind it.
        """
        changes: list[Any] = []
        try:
            changes.append(
                self.queue.get(timeout=timeout) if timeout else self.queue.get_nowait()
            )
        except queue.Empty:
            return changes
        while True:
            try:
                changes.append(self.queue.get_nowait())
            except queue.Empty:
                return changes

    def stop(self) -> None:
        self._stop.set()

    def __iter__(self) -> Iterator[Any]:
        while not self._stop.is_set():
            yield from self.poll(timeout=1.0)


class TaskFeed(_RowFeed):
    """Watches `tasks` — the §4.4 handoff wakeup."""

    TABLE = "tasks"

    @staticmethod
    def _parse_row(row: dict[str, Any]) -> TaskChange | None:
        return _parse(row)

    def unblocks(self, timeout: float = 0.0) -> list[TaskChange]:
        """Only the changes that make a task claimable — the §4.4 wakeup."""
        return [c for c in self.poll(timeout) if c.is_unblock]


class HazardFeed(_RowFeed):
    """Watches `hazards` — how an operator's intervention reaches the fleet
    (issue #22).

    The third lesson in this module's docstring applies here with more force
    than it did for tasks: a feed carries *every* write to a row, so a hazard
    that is later deactivated arrives a second time. `interventions()` filters
    on `active` for exactly that reason — a listener acting on every row it
    sees would re-apply a disruption at the moment it was cleared.
    """

    TABLE = "hazards"

    @staticmethod
    def _parse_row(row: dict[str, Any]) -> HazardChange | None:
        return _parse_hazard(row)

    def interventions(self, timeout: float = 0.0) -> list[HazardChange]:
        """Only live operator interventions."""
        return [
            c for c in self.poll(timeout) if c.is_intervention and c.active
        ]
