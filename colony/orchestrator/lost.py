"""The heartbeat scan: marking robots `lost` (§4.4, §5.1 lane 4, FR-5).

v3.1 took the orchestrator off the recovery path. An expired lease is already
claimable by anyone — that is the whole of §4.4's claiming SQL — so a dead
robot's work returns to the pool whether or not anybody is watching for it.
What is left without another owner is telling the UI and the event log that a
robot has gone quiet, which is exactly what §5.1 scopes this to:
"Robot `lost` marking (UI/events only — recovery is lease-native)".

Two things this deliberately does **not** do, both of which would quietly
undo FR-5:

**It never releases a lost robot's tasks.** That would be a second recovery
path racing the lease, and the v3.1 claim — "robot loss self-heals with no
supervisor on the recovery path" — stops being true the moment a supervisor is
on it. The lease is the mechanism; this is only the notification.

**It never calls `heartbeat()`.** The obvious way to record a robot's status is
the SDK method that writes robot status. But `heartbeat()` also stamps
`heartbeat_at = now()` *and* renews every lease that robot holds. Marking a
dead robot lost through it would make it look alive again on the very next
scan, and would keep pushing out the leases on work nobody is doing — turning
the lost-marker into the cause of the fleet stall FR-11 rules out. Lostness
lives in the event log instead, which is where §5.1 asked for it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

# Two missed renewals at §4.4's 5s cadence. Shorter than the 15s lease on
# purpose: the UI should say a robot is in trouble at about the time its work
# becomes reclaimable, not after. This number only moves a label — no recovery
# timing depends on it.
LOST_AFTER_SECONDS = 10

ROBOT_LOST = "robot_lost"
ROBOT_RECOVERED = "robot_recovered"


@dataclass(frozen=True)
class LostScan:
    """What changed since the previous scan. Both lists are sorted, so a caller
    rendering them gets a stable order rather than set iteration order."""

    newly_lost: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.newly_lost or self.recovered)


class LostWatch:
    """Watches one fleet's heartbeats and logs the transitions.

    Scoped to an explicit roster because `robots` has no `mission_id` (§4.5):
    the table is the fleet, not the mission, so a bare `stale_robots()` also
    returns robots from every other mission that ever ran against this
    database. Without the roster a demo run would open by declaring six robots
    from last week's mission lost.
    """

    def __init__(
        self,
        mem: Any,
        mission_id: UUID,
        robot_ids: Iterable[str],
        *,
        after_seconds: int = LOST_AFTER_SECONDS,
    ) -> None:
        self.mem = mem
        self.mission_id = mission_id
        self.fleet = frozenset(robot_ids)
        self.after_seconds = after_seconds
        self.lost: frozenset[str] = frozenset()

    def scan(self) -> LostScan:
        """One pass. Logs a transition per robot that crossed either way.

        Edge-triggered: a robot that stays silent is logged `robot_lost` once,
        not once per scan. The event log is what the commander console reads
        and what §4.7's metrics are derived from — a verb repeated four times a
        second for the rest of the mission would swamp both.
        """
        silent = frozenset(self.mem.stale_robots(seconds=self.after_seconds))
        silent &= self.fleet

        newly_lost = sorted(silent - self.lost)
        recovered = sorted(self.lost - silent)

        for robot_id in newly_lost:
            self.mem.log_event(
                self.mission_id,
                robot_id,
                ROBOT_LOST,
                {"silent_for_seconds": self.after_seconds},
            )
        for robot_id in recovered:
            self.mem.log_event(self.mission_id, robot_id, ROBOT_RECOVERED, {})

        self.lost = silent
        return LostScan(newly_lost=newly_lost, recovered=recovered)

    def lost_ids(self) -> list[str]:
        """The currently-lost roster, for the state frame (FR-8's robot layer)."""
        return sorted(self.lost)
