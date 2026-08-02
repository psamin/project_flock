"""Lifter and medic: the robots that actually rescue people (§4.3, §5.1 lane 2).

Both run the same loop — claim a task suited to the role, path to it, do the
work, complete it — so the handoff falls out of the data rather than out of any
messaging between them:

    scout finds victim -> clear_debris + deliver_kit(depends_on=[clear])
    L1 claims clear_debris, clears, completes  ->  deliver_kit unblocks
    M1 claims deliver_kit, stabilizes          ->  victim rescued

Nothing here knows about the other robot. The lifter never tells the medic
anything; completing its task is what makes the medic's claimable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from agents.pathing import find_move_plan
from fleetmem.types import Task
from sim.protocol import DIRECTIONS, Action
from sim.world import ROLES, World
from world.map_format import DEBRIS, RUBBLE_HEAVY

# Which task kinds each role is eligible for (§3.3 abilities).
ROLE_TASKS = {
    "lifter": {"clear_debris"},
    "medic": {"deliver_kit"},
}
TASK_VERB = {"clear_debris": "clear_debris", "deliver_kit": "stabilize"}

# §4.4: an agent that has waited this long without an assignment claims for
# itself. The claim transaction makes that exactly as safe as being assigned, so
# the orchestrator is an optimizer rather than a dependency — which is the line
# worth saying out loud during the chaos segment.
SELF_CLAIM_AFTER_TICKS = 20        # 5s at 4 Hz

# How long a robot avoids a task it could not reach. Without this a robot
# releases an unreachable task and immediately re-claims it — it is still the
# highest-scoring one it can see — and starves every reachable victim behind it.
# It is a cooling-off period rather than a permanent blacklist because the world
# changes: a lifter clears a route, the aftershock opens one, and the task
# becomes reachable after all.
RETRY_UNREACHABLE_AFTER_TICKS = 100


@dataclass
class Worker:
    """One lifter or medic."""

    robot_id: str
    role: str
    mission_id: UUID
    mem: Any
    seed: int = 0

    # Coordination OFF (§3.3 baseline mode): the robot still picks the best
    # task it can see, but does not claim it. Two robots then converge on the
    # same victim and one of them wastes the trip — the duplicated effort the
    # ON/OFF toggle exists to show.
    coordinated: bool = True

    task: Task | None = field(default=None)
    idle_ticks: int = 0
    # task id -> tick when this robot last failed to reach it
    unreachable: dict = field(default_factory=dict)

    # --- the loop ---------------------------------------------------------

    def step(self, world: World) -> Action:
        robot = world.robots[self.robot_id]
        here = (robot.x, robot.y)

        # Heartbeat renews the lease on whatever this robot holds (§4.4). Sent
        # every tick: a robot that stops heart-beating mid-clear has its work
        # taken over, which is exactly what we want when it is genuinely dead
        # and a disaster when it is merely busy.
        self.mem.heartbeat(self.robot_id, pos=here, battery=robot.battery,
                           status=robot.status)

        if self.task is None:
            self._find_work(world, here)
        if self.task is None:
            return Action.idle()

        target = (self.task.target[0], self.task.target[1])
        if target == (None, None):
            self._abandon()
            return Action.idle()

        # Adjacent to the target: do the work. The sim holds the robot for the
        # duration, so this is submitted once and the job runs itself out.
        if abs(here[0] - target[0]) + abs(here[1] - target[1]) <= 1:
            if robot.work_left > 0:
                return Action.idle()          # already working; the sim drives it
            if self._work_is_done(world, target):
                self._complete()
                return Action.idle()
            return Action.act(TASK_VERB[self.task.kind], target)

        return self._advance(world, here, target)

    # --- finding work -----------------------------------------------------

    def _find_work(self, world: World, here: tuple[int, int]) -> None:
        """Claim the best open task this role can do.

        Ranked by the §4.4 allocation score, then claimed. Losing a race is
        routine — another robot got there first — so we simply try the next one
        rather than treating it as an error.
        """
        self.idle_ticks += 1
        if self.idle_ticks < SELF_CLAIM_AFTER_TICKS and not self._orchestrator_quiet():
            return

        robot = world.robots[self.robot_id]
        candidates = [
            task for task in self.mem.open_tasks(self.mission_id)
            if task.kind in ROLE_TASKS.get(self.role, set())
            and not self._cooling_off(task.id, world.tick)
        ]
        candidates.sort(key=lambda t: -allocation_score(self.role, robot, t))

        for task in candidates:
            if self.coordinated and not self.mem.claim_task(task.id, self.robot_id):
                continue                    # someone else got there first
            self.task = task
            self.idle_ticks = 0
            self.mem.log_event(self.mission_id, self.robot_id, "task_claimed",
                               {"task": str(task.id), "kind": task.kind,
                                "target": list(task.target)})
            self._log_choice(task, candidates)
            return

    def _log_choice(self, chosen: Task, considered: list[Task]) -> None:
        """Record the decision and the memories behind it (FR-17, §4.0).

        There is no Bedrock call in this loop yet, but the provenance question a
        judge asks — "why did L1 go there?" — has an answer either way, and the
        commander console needs the rows to join against. `based_on` carries the
        beliefs near the chosen target, which is exactly what the allocation
        score weighed.
        """
        nearby = self.mem.get_beliefs(
            self.mission_id,
            area=(chosen.target[0] - 3, chosen.target[1] - 3,
                  chosen.target[0] + 3, chosen.target[1] + 3),
        ) if chosen.target[0] is not None else []
        self.mem.log_plan(
            self.mission_id, self.robot_id,
            trigger="idle",
            chosen={"action": "claim_task", "task_id": str(chosen.id),
                    "kind": chosen.kind, "target": list(chosen.target)},
            rationale=(f"best of {len(considered)} open {chosen.kind} tasks by "
                       f"role match, priority and distance"),
            based_on=[b.id for b in nearby],
        )

    def _cooling_off(self, task_id, tick: int) -> bool:
        failed_at = self.unreachable.get(task_id)
        if failed_at is None:
            return False
        if tick - failed_at >= RETRY_UNREACHABLE_AFTER_TICKS:
            del self.unreachable[task_id]        # the world may have opened up
            return False
        return True

    def _orchestrator_quiet(self) -> bool:
        """There is no orchestrator pushing assignments yet, so every robot is
        in the self-claim path. Kept explicit so wiring one in later is a change
        of one predicate rather than a rewrite."""
        return True

    # --- moving -----------------------------------------------------------

    def _advance(self, world: World, here: tuple[int, int],
                 target: tuple[int, int]) -> Action:
        """Issue the first move of a plan searched over *moves*, not tiles.

        Planning in tiles and walking the result one tile per tick is wrong for
        any robot faster than one tile per move (§3.3): the tile where a route
        turns is not a position it can stop at. A speed-2 medic overshot it,
        replanned from the far side, and either paced between two tiles forever
        or stalled outright because no single step improved its distance — which
        is exactly what stranded it four tiles from a victim on the demo map.

        Searching over moves removes the mismatch instead of compensating for
        it, and it is speed-agnostic: the same code drives a speed-1 lifter and a
        speed-3 scout with no special cases.

        Replanned every tick rather than cached: fire spreads, the aftershock
        re-blocks corridors, and robots move, so a stale plan is a liability. At
        40x30 the search is cheap enough that correctness beats the saving.
        """
        plan = find_move_plan(
            here, target,
            landing=lambda p, d: self._landing(world, p, d, avoid_robots=True),
            goal_is_adjacent=True,
            speed=ROLES[self.role]["speed"],
        )
        if plan:
            return Action.move(plan[0])

        # No plan with the fleet treated as obstacles. Distinguish "someone is in
        # the way right now" from "there is no way": releasing on the first
        # blocked tick would churn a task between robots every time two of them
        # crossed paths.
        if find_move_plan(
            here, target,
            landing=lambda p, d: self._landing(world, p, d, avoid_robots=False),
            goal_is_adjacent=True,
            speed=ROLES[self.role]["speed"],
        ) is None:
            self.unreachable[self.task.id] = world.tick
            self._abandon(reason="unreachable")
        return Action.idle()

    def _landing(self, world: World, here: tuple[int, int], direction: str,
                 *, avoid_robots: bool) -> tuple[int, int]:
        """Where a single `move` in this direction actually leaves the robot.

        Mirrors the sim's rule exactly: advance up to the role's speed, stopping
        at the first tile that is impassable or occupied. The planner searches
        over this, so a plan is executable by construction.
        """
        dx, dy = DIRECTIONS[direction]
        x, y = here
        for _ in range(ROLES[self.role]["speed"]):
            nx, ny = x + dx, y + dy
            if not self._passable(world, (nx, ny)):
                break
            if avoid_robots and world.occupied(nx, ny, ignore=self.robot_id):
                break
            x, y = nx, ny
        return (x, y)

    def _passable(self, world: World, point: tuple[int, int]) -> bool:
        """The sim's rule, asked of the sim.

        Re-implementing it here meant two copies that could drift: add an
        impassable object to the world and the planner would keep routing
        through it, making every plan unexecutable while still looking correct.
        A lifter walks *to* debris to clear it, never through it, which is
        exactly what `flying=False` already means.
        """
        return world.passable(point[0], point[1], flying=False)

    # --- finishing --------------------------------------------------------

    def _work_is_done(self, world: World, target: tuple[int, int]) -> bool:
        """Whether the world already shows this task's effect.

        Checked before acting so a task someone else finished — or that the
        aftershock made moot — is completed rather than re-attempted forever.
        """
        if self.task.kind == "clear_debris":
            return world.objects[target[1]][target[0]] not in (DEBRIS, RUBBLE_HEAVY)
        victim = world.victim_at(*target)
        return victim is not None and victim.state in ("stabilized", "lost")

    def _complete(self) -> None:
        """Report completion — but only if shared memory accepted it.

        `complete_task` applies only while this robot still owns the task. If
        the lease lapsed and someone else took it over, the write matches no row
        and returns None. Logging `task_completed` anyway would inflate the §4.7
        metrics, which are derived entirely from this event stream, and would
        credit a robot for work another one did.
        """
        unblocked = self.mem.complete_task(self.task.id, self.robot_id)
        if unblocked is None:
            self.mem.log_event(self.mission_id, self.robot_id, "task_lost",
                               {"task": str(self.task.id), "kind": self.task.kind})
        else:
            self.mem.log_event(self.mission_id, self.robot_id, "task_completed",
                               {"task": str(self.task.id), "kind": self.task.kind,
                                "unblocked": [str(u) for u in unblocked]})
        self.task = None

    def _abandon(self, reason: str = "invalid") -> None:
        if self.task is not None:
            self.mem.release_task(self.task.id)
            self.mem.log_event(self.mission_id, self.robot_id, "task_released",
                               {"task": str(self.task.id), "reason": reason})
        self.task = None


def allocation_score(role: str, robot: Any, task: Task) -> float:
    """§4.4's greedy allocation score.

    2.0·role_match + 1.2·priority + 1.0·(1/(1+dist)) + 0.5·battery_norm
    """
    role_match = 1.0 if task.kind in ROLE_TASKS.get(role, set()) else 0.0
    tx, ty = task.target
    distance = (abs(robot.x - tx) + abs(robot.y - ty)) if tx is not None else 0
    battery_norm = robot.battery / max(1, ROLES[role]["battery"])
    return (2.0 * role_match
            + 1.2 * task.priority
            + 1.0 * (1 / (1 + distance))
            + 0.5 * battery_norm)


