"""Changefeed handoffs — the P1 half of §4.4's "handoff triggers".

§4.4: "MVP polls open tasks at 1 Hz. P1 swaps in a CRDB changefeed on `tasks`
→ orchestrator/agents wake instantly. Same contract, faster push."

A **core** changefeed (`EXPERIMENTAL CHANGEFEED FOR`), which streams rows back
to the SQL session rather than to an external sink. That choice matters: sink
changefeeds are a CCL feature needing an enterprise licence and somewhere to
put the messages, and neither is something a hackathon demo should depend on.
A core changefeed runs on the free single-node dev cluster and on the Cloud
free tier alike.

Nothing in the fleet depends on this module. The 1 Hz poll in `open_tasks` is
still the path every agent takes, and §4.4 is explicit that recovery is
lease-native either way — this is a latency optimisation, not a mechanism.
That is also why it is P1 and why it is a separate module rather than a change
to the frozen SDK surface (§5.2).

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
from fleetmem.types import OPEN

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


def _parse(row: dict[str, Any]) -> TaskChange | None:
    """One feed row -> a TaskChange, or None if it is not a task row.

    Resolved-timestamp messages arrive on the same stream with a null table, and
    a delete arrives with `after: null`. Neither is a task becoming claimable.
    """
    if not row.get("table") or not row.get("value"):
        return None
    after = json.loads(row["value"]).get("after")
    if not after:
        return None
    return TaskChange(
        task_id=UUID(after["id"]),
        mission_id=UUID(after["mission_id"]) if after.get("mission_id") else None,
        kind=after.get("kind", ""),
        status=after.get("status", ""),
        claimed_by=after.get("claimed_by"),
    )


class TaskFeed:
    """Watches `tasks` on its own connection and hands changes to a queue.

    A background thread, because the statement never returns — it is a stream,
    and the caller is a tick loop that cannot block on one. The queue is the
    seam: `poll()` is what an orchestrator would call where it currently calls
    `open_tasks()`, which is what §4.4 means by "same contract".
    """

    def __init__(
        self,
        mission_id: UUID | None = None,
        dsn: str | None = None,
        resolved: str = RESOLVED_EVERY,
    ) -> None:
        self.mission_id = mission_id
        self.dsn = resolve_dsn(dsn)
        self.resolved = resolved
        self.queue: queue.Queue[TaskChange] = queue.Queue()
        self.error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = threading.Event()

    def __enter__(self) -> TaskFeed:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def start(self, timeout: float = 10.0) -> TaskFeed:
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
                    "EXPERIMENTAL CHANGEFEED FOR tasks "
                    f"WITH no_initial_scan, resolved='{self.resolved}'"
                )
                for row in stream:
                    if self._stop.is_set():
                        return
                    change = _parse(row)
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

    def poll(self, timeout: float = 0.0) -> list[TaskChange]:
        """Everything waiting, optionally blocking up to `timeout` for the first.

        Returns a list rather than one change so a tick loop drains the queue in
        one call — the alternative is a robot handling one handoff per tick
        while three more sit in a queue behind it.
        """
        changes: list[TaskChange] = []
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

    def unblocks(self, timeout: float = 0.0) -> list[TaskChange]:
        """Only the changes that make a task claimable — the §4.4 wakeup."""
        return [c for c in self.poll(timeout) if c.is_unblock]

    def stop(self) -> None:
        self._stop.set()

    def __iter__(self) -> Iterator[TaskChange]:
        while not self._stop.is_set():
            yield from self.poll(timeout=1.0)
