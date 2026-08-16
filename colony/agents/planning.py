"""Bedrock planning inside the agent loop (§4.3, §5.1 lane 2, FR-17).

Three pieces live here:

    role cards      who this robot is, what it can do (§3.3 stat blocks)
    digest builder  the shared beliefs that go in the prompt — and their ids,
                    which become `plans.based_on` (FR-17)
    Planner         when a robot is allowed to ask Bedrock at all

The discipline §4.3 asks for is "plan boundaries only, never per tick", and the
shape that enforces it is `Planner.plan()` returning **None** whenever it has
nothing better to offer than the robot's own rules:

    over the §3.5 rate cap        -> None, use rules
    replay with no cassette entry -> None, use rules
    a live call still in flight   -> None, use rules *this tick* and pick the
                                     answer up when it lands

So the rules are always the floor, never the exception path: a mission runs
identically with no AWS credentials, and `--seeded` replay runs stay
deterministic because a cassette hit is the only thing that changes a decision.
That is also §5.4's "recorded-replay mode for demos; live mode for the deployed
URL with rule fallback", implemented as one code path rather than two.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from bedrock.adapter import LIVE, RECORD, BedrockAdapter, Plan

# §3.5: hard cap of 4 plan calls per robot per minute. Counted in ticks rather
# than seconds so a seeded run cannot drift with wall-clock time.
PLAN_CALLS_PER_MINUTE = 4
TICKS_PER_MINUTE = 240  # 4 Hz (§3.5)

# Prompt budget (§4.3: "≤1.5k tokens"). Beliefs are one short line each, so a
# dozen of them plus the role card and the open-task list lands well inside it.
DIGEST_BELIEFS = 12
DIGEST_RADIUS = 10

# Where a plan came from. Rides in `plans.chosen` so the commander console can
# tell a Bedrock decision from a rule-based one without a second table.
BEDROCK, RULES = "bedrock", "rules"

# §3.3 stat blocks, written as the robot's own job description. Kept short: the
# prompt budget is better spent on beliefs than on prose.
ROLE_CARDS = {
    "scout": (
        "You are {id}, a scout drone in a disaster-response fleet. You fly over"
        " debris, see 6 tiles, and cannot interact with anything. Your job is to"
        " find victims and hazards and report them, sector by sector."
    ),
    "lifter": (
        "You are {id}, a tracked lifter in a disaster-response fleet. You move 1"
        " tile per tick and see 2. You are the only robot that can clear debris,"
        " which takes 3 ticks (6 for rubble). Victims behind rubble stay"
        " unreachable until you get there."
    ),
    "medic": (
        "You are {id}, a medic courier in a disaster-response fleet. You move 2"
        " tiles per tick, see 3, and are the only robot that can stabilize a"
        " victim. You carry {kits} supply kits and restock at base."
    ),
}


def role_card(robot: Any) -> str:
    """The role card for one robot (§4.3 prompt = role card + digest + tasks)."""
    template = ROLE_CARDS.get(robot.role, "You are {id}, a robot in a fleet.")
    return template.format(id=robot.id, kits=getattr(robot, "kits", 0))


@dataclass(frozen=True)
class Digest:
    """The beliefs that went into a prompt, and their ids.

    The ids are the whole point (FR-17): `log_plan(based_on=digest.ids)` is what
    turns "the medic went east" into an answer to "why?", because the commander
    console can join those ids straight back to `observations`.
    """

    text: str
    ids: tuple[UUID, ...] = ()


def build_digest(
    mem: Any,
    mission_id: UUID,
    robot: Any,
    *,
    radius: int = DIGEST_RADIUS,
    limit: int = DIGEST_BELIEFS,
) -> Digest:
    """Summarise what the fleet knows near this robot, and record what was used.

    Local rather than global: a robot deciding what to do next is served by the
    beliefs around it, and a whole-mission digest would blow the §4.3 token
    budget by mid-mission. Ordering is nearest-first with an id tiebreak, so the
    same mission state always produces the same prompt — which is what makes a
    cassette hit possible at all.
    """
    beliefs = mem.get_beliefs(
        mission_id,
        area=(robot.x - radius, robot.y - radius, robot.x + radius, robot.y + radius),
    )
    beliefs = sorted(
        beliefs,
        key=lambda b: (
            abs(b.pos[0] - robot.x) + abs(b.pos[1] - robot.y),
            b.kind,
            str(b.id),
        ),
    )[:limit]
    if not beliefs:
        return Digest(text="- (nothing reported near you yet)")
    lines = [
        f"- {b.kind} at ({b.pos[0]},{b.pos[1]})"
        f" seen {b.sightings}x confidence {b.confidence:.2f}"
        for b in beliefs
    ]
    return Digest(text="\n".join(lines), ids=tuple(b.id for b in beliefs))


def task_lines(tasks: list[Any]) -> list[dict[str, Any]]:
    """Open tasks in the shape `BedrockAdapter.plan` expects."""
    return [
        {
            "id": str(task.id),
            "kind": task.kind,
            "target_x": task.target[0],
            "target_y": task.target[1],
            "priority": task.priority,
        }
        for task in tasks
    ]


@dataclass
class Planner:
    """Rate-capped access to Bedrock, shared by every agent in a mission.

    One planner per mission rather than one per robot: the cap is per robot, but
    the thread pool and the cassette are not, and a pool per robot would put six
    idle threads in the tick loop for nothing.
    """

    adapter: BedrockAdapter = field(default_factory=BedrockAdapter)
    max_workers: int = 4

    # robot_id -> ticks at which it called, most recent last
    _calls: dict[str, list[int]] = field(default_factory=dict, init=False)
    # robot_id -> a live call that has not landed yet
    _pending: dict[str, Future] = field(default_factory=dict, init=False)
    _pool: ThreadPoolExecutor | None = field(default=None, init=False)

    @property
    def live(self) -> bool:
        return self.adapter.mode in (LIVE, RECORD)

    def plan(
        self,
        robot: Any,
        tick: int,
        digest: Digest,
        open_tasks: list[Any],
        tactics: Sequence[str] = (),
    ) -> Plan | None:
        """A plan, or None to mean "use your own rules this tick".

        Never blocks. §3.5 requires a robot to keep acting while a plan is in
        flight, so a live call is submitted to a thread and collected on a later
        tick; a robot that would otherwise stand still for a 3-second round trip
        instead keeps clearing the debris it is already standing next to.
        """
        landed = self._collect(robot.id)
        if landed is not None:
            return landed
        if robot.id in self._pending or not self._within_cap(robot.id, tick):
            return None

        card, tasks = role_card(robot), task_lines(open_tasks)
        if not self.live:
            # Replay: a cassette hit is a real recorded decision and is worth
            # replaying. A miss is not — the adapter would answer from the same
            # rules the agent already has, and pretending that came from Bedrock
            # would put a fabricated rationale in front of a judge.
            if not self.adapter.knows_plan(card, digest.text, tasks, tactics):
                return None
            self._record_call(robot.id, tick)
            return self.adapter.plan(card, digest.text, tasks, tactics)

        self._record_call(robot.id, tick)
        self._pending[robot.id] = self._submit(card, digest.text, tasks, tactics)
        return None

    def _collect(self, robot_id: str) -> Plan | None:
        """Pick up a finished call, if this robot has one waiting."""
        pending = self._pending.get(robot_id)
        if pending is None or not pending.done():
            return None
        del self._pending[robot_id]
        try:
            return pending.result()
        except Exception:  # noqa: BLE001 — any failure means we have no plan
            # A plan that failed after the adapter's own fallbacks is a plan we
            # do not have; the robot's rules carry it. Never let one bad call
            # take down a tick loop that is running a rescue.
            return None

    def _submit(
        self,
        card: str,
        digest: str,
        tasks: list[dict[str, Any]],
        tactics: Sequence[str] = (),
    ) -> Future:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="plan"
            )
        return self._pool.submit(self.adapter.plan, card, digest, tasks, tactics)

    def _within_cap(self, robot_id: str, tick: int) -> bool:
        recent = [
            t for t in self._calls.get(robot_id, []) if tick - t < TICKS_PER_MINUTE
        ]
        self._calls[robot_id] = recent
        return len(recent) < PLAN_CALLS_PER_MINUTE

    def _record_call(self, robot_id: str, tick: int) -> None:
        self._calls.setdefault(robot_id, []).append(tick)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
