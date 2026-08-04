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

from agents import beliefs, logistics, planning
from agents.pathing import find_move_plan
from agents.planning import BEDROCK, RULES
from fleetmem.types import AFTERSHOCK, IDLE_TRIGGER, Task
from sim.protocol import DIRECTIONS, Action
from sim.world import ROLES, World
from world.map_format import DEBRIS, RUBBLE_HEAVY

# Which task kinds each role is eligible for (§3.3 abilities).
ROLE_TASKS = {
    "lifter": {"clear_debris"},
    "medic": {"deliver_kit"},
}
TASK_VERB = {"clear_debris": "clear_debris", "deliver_kit": "stabilize"}

# §3.6 thought bubbles: what the robot is doing, in the words a viewer needs.
_VERB_BUBBLE = {"clear_debris": "🧱 clearing debris", "deliver_kit": "📦 kit en route"}

# §4.4: an agent that has waited this long without an assignment claims for
# itself. The claim transaction makes that exactly as safe as being assigned, so
# the orchestrator is an optimizer rather than a dependency — which is the line
# worth saying out loud during the chaos segment.
SELF_CLAIM_AFTER_TICKS = 20  # 5s at 4 Hz

# How long a robot avoids a task it could not reach. Without this a robot
# releases an unreachable task and immediately re-claims it — it is still the
# highest-scoring one it can see — and starves every reachable victim behind it.
# It is a cooling-off period rather than a permanent blacklist because the world
# changes: a lifter clears a route, the aftershock opens one, and the task
# becomes reachable after all.
RETRY_UNREACHABLE_AFTER_TICKS = 100

# The same cooling-off after a *robot* got in the way, which is a much shorter
# lived problem than a sealed route: the obstruction has legs, and staging is
# already walking it out of the way.
RETRY_BLOCKED_AFTER_TICKS = 20

# How long a robot tolerates another robot standing in its only route before it
# hands the task back. Deliberately longer than STAGE_AFTER_IDLE_TICKS below
# plus the walk out of a doorway: staging clears most jams by itself, and a
# release that fires first turns every passing traffic jam into a dropped task.
BLOCKED_RELEASE_TICKS = 40  # 10s at 4 Hz

# How long a robot waits before repositioning. A robot that has just finished a
# job is usually about to be handed the next one — the scout that found this
# victim is still reporting the ones beside it — and walking away the instant a
# task ends costs more time than staging saves. Measured: staging immediately
# stabilized one fewer victim in the first 40 ticks of the demo map.
STAGE_AFTER_IDLE_TICKS = 20  # 5s at 4 Hz

# §4.3 idle staging. How close to its staging point a robot needs to be before
# it stops moving — a radius rather than a tile so it does not shuffle back and
# forth when something else is standing on the exact spot.
STAGING_RADIUS = 2

# Two pieces of work count as one cluster within this many tiles of each other.
# Roughly a room's width on the demo map (§3.3).
CLUSTER_RADIUS = 4

# Staging reads shared memory, so it is recomputed on the §4.3 cadence for
# belief reads (~1s = 4 ticks at 4 Hz) rather than every tick.
STAGING_REFRESH_TICKS = 4


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

    # Bedrock at plan boundaries (§4.3). None means rules only, which is a
    # complete robot — the planner improves choices, it does not enable them.
    planner: Any = None

    # Whether an orchestrator is pushing assignments (lane 4). While there is
    # none, waiting SELF_CLAIM_AFTER_TICKS for one would leave victims waiting
    # five seconds for a message nobody is sending (§4.4).
    orchestrated: bool = False

    task: Task | None = field(default=None)
    idle_ticks: int = 0
    # Heading for base to recharge or restock (§3.3), and not taking work until
    # it gets there.
    homing: bool = False
    # Escalations this robot has felt. A change it did not cause invalidates the
    # plan it made before the change (FR-7).
    seen_escalations: int = 0
    # Consecutive ticks another robot has stood in the only route to the target.
    blocked_ticks: int = 0
    # task id -> tick when this robot last failed to reach it
    unreachable: dict = field(default_factory=dict)
    # Baseline only: tasks this robot considers done. Without claiming there is
    # no shared record of completion, so it keeps its own and stops re-picking.
    finished: set = field(default_factory=set)
    last_tick: int = 0
    # (tick, point) — the staging point, recomputed on the belief-read cadence.
    _staging: tuple[int, tuple[int, int] | None] | None = field(default=None)
    # (tick, map) — the shared hazard picture routing is done against.
    _belief_cache: tuple[int, Any] | None = field(default=None)

    # --- the loop ---------------------------------------------------------

    def step(self, world: World) -> Action:
        robot = world.robots[self.robot_id]
        here = (robot.x, robot.y)

        # Heartbeat renews the lease on whatever this robot holds (§4.4). Sent
        # every tick: a robot that stops heart-beating mid-clear has its work
        # taken over, which is exactly what we want when it is genuinely dead
        # and a disaster when it is merely busy.
        self.mem.heartbeat(
            self.robot_id, pos=here, battery=robot.battery, status=robot.status
        )

        self.last_tick = world.tick
        if robot.work_left > 0:
            return Action.idle()  # mid-job; the sim drives it to the end

        self._note_escalation(world)

        # Fuel and kits come before work: a robot that strands itself two tiles
        # from the charger takes its task with it (§3.3).
        if self.homing or logistics.needs_base(world, robot, here):
            return self._go_home(world, robot, here)

        if self.task is None:
            self._find_work(world, here)
        if self.task is None:
            return self._stage(world, here)

        target = (self.task.target[0], self.task.target[1])
        if target == (None, None):
            self._abandon()
            return Action.idle()

        # Adjacent to the target: do the work. The sim holds the robot for the
        # duration, so this is submitted once and the job runs itself out.
        if abs(here[0] - target[0]) + abs(here[1] - target[1]) <= 1:
            if self._work_is_done(world, target):
                self._complete()
                return Action.idle()
            self._say(
                world, f"{_VERB_BUBBLE[self.task.kind]} at {target[0]},{target[1]}"
            )
            return Action.act(TASK_VERB[self.task.kind], target)

        return self._advance(world, here, target)

    # --- battery and kits (§3.3) ------------------------------------------

    def _go_home(self, world: World, robot: Any, here: tuple[int, int]) -> Action:
        """Break off, return to base, charge and restock, then go back to work.

        The task goes back to the pool on the way out rather than riding along:
        a robot walking home keeps renewing the lease on work it is not doing,
        and §4.4's explicit release is exactly the "agent gives up" case. If
        nobody else takes it, this robot claims it again on the way back.
        """
        if self.task is not None:
            self._abandon(reason="returning to base")
        if not self.homing:
            self.homing = True
            self.mem.log_event(
                self.mission_id,
                self.robot_id,
                "returning_to_base",
                {"battery": robot.battery, "kits": robot.kits},
            )

        service = logistics.service_action(world, robot)
        if service is not None:
            self._say(
                world,
                "🔌 recharging" if service.verb == "recharge" else "📦 restocking",
            )
            return service
        if logistics.is_serviced(world, robot):
            self.homing = False
            self.idle_ticks = 0
            return Action.idle()

        base = logistics.base_tile(world, self.role)
        if base is None:  # nowhere to go home to; carry on and hope
            self.homing = False
            return Action.idle()
        self._say(world, "🔋 returning to base")
        plan = self._plan_move(world, here, base, avoid_robots=True)
        return Action.move(plan[0]) if plan else Action.idle()

    # --- reacting to the world (FR-7) -------------------------------------

    def _note_escalation(self, world: World) -> None:
        """An aftershock invalidates plans made before it (FR-7).

        The robot does not get told what changed — it re-decides, and the world
        it re-decides against is the one it can now observe. Holding work is the
        risky part: the corridor this task depended on may be gone, so the task
        goes back to the pool and is re-picked on the merits a tick later.
        """
        if world.escalations_fired <= self.seen_escalations:
            return
        self.seen_escalations = world.escalations_fired
        if self.task is None:
            return
        released = self.task
        self._abandon(reason="aftershock")
        self._log_choice(
            None,
            [],
            trigger=AFTERSHOCK,
            rationale=(
                f"aftershock invalidated my route to {released.kind} at "
                f"{released.target[0]},{released.target[1]}; re-deciding"
            ),
            robot=world.robots[self.robot_id],
        )

    # --- finding work -----------------------------------------------------

    def _find_work(self, world: World, here: tuple[int, int]) -> None:
        """Claim the best open task this role can do.

        Ranked by the §4.4 allocation score, then claimed. Losing a race is
        routine — another robot got there first — so we simply try the next one
        rather than treating it as an error.
        """
        self.idle_ticks += 1
        if self.idle_ticks < SELF_CLAIM_AFTER_TICKS and self.orchestrated:
            return

        robot = world.robots[self.robot_id]
        candidates = [
            task
            for task in self.mem.open_tasks(self.mission_id)
            if task.kind in ROLE_TASKS.get(self.role, set())
            and task.id not in self.finished
            and not self._cooling_off(task.id, world.tick)
        ]
        candidates.sort(key=lambda t: -allocation_score(self.role, robot, t))
        if not candidates:
            return

        # The LLM chooses *among* behaviours; the rules rank them and execute
        # (§4.3). A plan that names a task moves it to the front of the queue —
        # it does not bypass the claim, so a Bedrock choice is exactly as safe
        # as a rule-based one, and a stale one simply loses the race.
        plan, digest = self._consult(world, robot, candidates)
        ordered = _plan_first(candidates, plan)

        for task in ordered:
            if self.coordinated and not self.mem.claim_task(task.id, self.robot_id):
                continue  # someone else got there first
            self.task = task
            self.idle_ticks = 0
            self.blocked_ticks = 0
            self.mem.log_event(
                self.mission_id,
                self.robot_id,
                "task_claimed",
                {"task": str(task.id), "kind": task.kind, "target": list(task.target)},
            )
            self._log_choice(
                task,
                candidates,
                trigger=IDLE_TRIGGER,
                rationale=(
                    plan.rationale
                    if plan is not None and plan.rationale
                    else (
                        f"best of {len(candidates)} open {task.kind} tasks by "
                        f"role match, priority and distance"
                    )
                ),
                robot=robot,
                digest=digest,
                source=BEDROCK if plan is not None else RULES,
            )
            self._say(
                world, f"{_VERB_BUBBLE[task.kind]} at {task.target[0]},{task.target[1]}"
            )
            return

    def _consult(
        self, world: World, robot: Any, candidates: list[Task]
    ) -> tuple[Any, Any]:
        """Ask Bedrock which task to take, if it is allowed to be asked (§4.3).

        Returns (plan | None, digest). The digest is built either way: its
        belief ids are `based_on` (FR-17), and a rule-based decision deserves
        the same provenance trail as a Bedrock one — the commander console
        should not go quiet just because the model was rate-capped.
        """
        digest = planning.build_digest(self.mem, self.mission_id, robot)
        if self.planner is None:
            return None, digest
        plan = self.planner.plan(robot, world.tick, digest, candidates)
        return plan, digest

    def _log_choice(
        self,
        chosen: Task | None,
        considered: list[Task],
        *,
        trigger: str,
        rationale: str,
        robot: Any,
        digest: Any = None,
        source: str = RULES,
    ) -> None:
        """Record the decision and the memories behind it (FR-17, §4.0).

        Every decision, not only the Bedrock ones. The question a judge asks —
        "why did L1 go there?" — has an answer either way, and the commander
        console joins `based_on` straight back to `observations` to give it. The
        digest is the honest source list: those rows, and only those, are what
        the robot was looking at when it decided.
        """
        if digest is None:
            digest = planning.build_digest(self.mem, self.mission_id, robot)
        self.mem.log_plan(
            self.mission_id,
            self.robot_id,
            trigger=trigger,
            chosen=(
                {
                    "action": "claim_task",
                    "task_id": str(chosen.id),
                    "kind": chosen.kind,
                    "target": list(chosen.target),
                    "considered": len(considered),
                    "source": source,
                }
                if chosen is not None
                else {"action": "replan", "source": source}
            ),
            rationale=rationale,
            based_on=list(digest.ids),
        )

    def _cooling_off(self, task_id, tick: int) -> bool:
        failure = self.unreachable.get(task_id)
        if failure is None:
            return False
        failed_at, cooldown = failure
        if tick - failed_at >= cooldown:
            del self.unreachable[task_id]  # the world may have opened up
            return False
        return True

    # --- talking to the viewer (§3.6) -------------------------------------

    def _say(self, world: World, text: str) -> None:
        """Set this robot's thought bubble.

        The bubble rides in the state frame the renderer already receives, so
        surfacing what a robot is doing costs no new plumbing and leaves the
        frozen frame shape alone. The deeper "why" — rationale plus the memories
        behind it — lives in `plans`, which is what a bubble click expands.
        """
        world.robots[self.robot_id].bubble = text

    # --- idle staging (§4.3) ----------------------------------------------

    def _stage(self, world: World, here: tuple[int, int]) -> Action:
        """Wait somewhere useful instead of wherever the last job ended.

        §4.3 asks lifters to idle-stage near the densest blocked-victim cluster
        and medics to pre-position between base and reachable victims. Both are
        about being closer to the next job, but the reason this is not polish is
        traffic: a robot that parks where it stands is an obstacle, and on the
        demo map an idle lifter sat in the one-tile corridor to victim 8 while
        the medic holding that victim's delivery task waited behind it. Neither
        ever moved. Both robots were frozen from tick ~150, victim 8 died at its
        deadline with the medic still holding its task, and the victim the
        aftershock reveals was never claimed because the only medic was stuck.

        Staging fixes that at the source: a robot with nothing to do walks to
        where the work is, which is never a corridor it is blocking.
        """
        if self.idle_ticks < STAGE_AFTER_IDLE_TICKS:
            return Action.idle()
        target = self._staging_target(world)
        if target is None or _distance(here, target) <= STAGING_RADIUS:
            return Action.idle()
        plan = self._plan_move(world, here, target, avoid_robots=True)
        return Action.move(plan[0]) if plan else Action.idle()

    def _staging_target(self, world: World) -> tuple[int, int] | None:
        """Where this role waits, cached on the §4.3 belief-read cadence."""
        if self._staging is not None:
            at, point = self._staging
            if world.tick - at < STAGING_REFRESH_TICKS:
                return point
        point = self._compute_staging_target(world)
        self._staging = (world.tick, point)
        return point

    def _compute_staging_target(self, world: World) -> tuple[int, int] | None:
        """The staging point per §4.3, or base when there is nothing to anticipate.

        Baseline mode (§3.3) stages at base and nothing else: a baseline robot
        keeps a private world model, so it has no shared belief map to read, and
        reading one here would leak the coordination layer into the very run the
        ON/OFF toggle exists to compare against.
        """
        base = self._base(world)
        if not self.coordinated:
            return base

        if self.role == "lifter":
            # Open clear_debris work is where blocked victims are: the chain the
            # reconcile gate builds (§4.2 step 3) puts a clear in front of every
            # victim behind rubble, so the debris queue *is* the blocked-victim
            # map, and it needs no belief interpretation to read.
            cluster = _densest(
                [
                    (task.target[0], task.target[1])
                    for task in self.mem.open_tasks(self.mission_id)
                    if task.kind == "clear_debris" and task.target[0] is not None
                ]
            )
            return cluster or base

        cluster = _densest(
            [b.pos for b in self.mem.get_beliefs(self.mission_id, kind="victim")]
        )
        if cluster is None or base is None:
            return cluster or base
        # Between base and the victims (§4.3): close enough to respond, close
        # enough to restock once kit logistics land.
        return ((base[0] + cluster[0]) // 2, (base[1] + cluster[1]) // 2)

    def _base(self, world: World) -> tuple[int, int] | None:
        """This role's spawn — the staging zone (§3.3), and later the recharge
        and restock point."""
        points = world.map.spawn_points.get(self.role)
        return (points[0]["x"], points[0]["y"]) if points else None

    # --- moving -----------------------------------------------------------

    def _advance(
        self, world: World, here: tuple[int, int], target: tuple[int, int]
    ) -> Action:
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
        plan = self._plan_move(world, here, target, avoid_robots=True)
        if plan:
            self.blocked_ticks = 0
            return Action.move(plan[0])

        # No plan with the fleet treated as obstacles. Distinguish "someone is in
        # the way right now" from "there is no way": releasing on the first
        # blocked tick would churn a task between robots every time two of them
        # crossed paths.
        if self._plan_move(world, here, target, avoid_robots=False) is None:
            self.unreachable[self.task.id] = (world.tick, RETRY_UNREACHABLE_AFTER_TICKS)
            self._abandon(reason="unreachable")
            return Action.idle()

        # Someone is in the way. Waiting is right for a robot that is passing
        # through, and wrong for one that has stopped: idle staging keeps robots
        # moving, but a robot doing a long job in a doorway can still outlast the
        # victim behind it. Give the task back rather than hold it to the
        # deadline — under §4.4 an explicit release is exactly what a lease
        # lapse would have done, and any robot with a clear route can take it.
        self.blocked_ticks += 1
        if self.blocked_ticks >= BLOCKED_RELEASE_TICKS:
            self.unreachable[self.task.id] = (world.tick, RETRY_BLOCKED_AFTER_TICKS)
            self._abandon(reason="blocked")
            self.blocked_ticks = 0
        return Action.idle()

    def _plan_move(
        self,
        world: World,
        here: tuple[int, int],
        target: tuple[int, int],
        *,
        avoid_robots: bool,
    ) -> list[str] | None:
        return find_move_plan(
            here,
            target,
            landing=lambda p, d: self._landing(world, p, d, avoid_robots=avoid_robots),
            cost=self._beliefs(world).cost,
            goal_is_adjacent=True,
            speed=ROLES[self.role]["speed"],
        )

    def _beliefs(self, world: World) -> beliefs.BeliefMap:
        """The fleet's belief map, re-read on the §4.3 cadence.

        This is the "A* over the shared belief map" half of §5.1: the search is
        in `agents/pathing.py`, but what it treats as dangerous comes from
        CockroachDB — hazards this robot never saw, reported by robots it never
        talked to. Baseline gets its own eyes only, and walks into things.
        """
        if self._belief_cache is not None:
            at, cached = self._belief_cache
            if world.tick - at < beliefs.BELIEF_REFRESH_TICKS:
                return cached
        current = beliefs.load(self.mem, self.mission_id, coordinated=self.coordinated)
        self._belief_cache = (world.tick, current)
        return current

    def _landing(
        self, world: World, here: tuple[int, int], direction: str, *, avoid_robots: bool
    ) -> tuple[int, int]:
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

        Baseline mode (§3.3) never claims, so ownership was never stamped and
        that check can never pass. Left as-is, a baseline robot finished the work
        in the world, failed to record it, re-picked the same open task next
        tick, and logged claimed/lost forever — 54 pairs in 60 ticks, with the
        task ledger showing nothing completed. Every completion-derived baseline
        metric was computed off that. It records the completion directly instead,
        and deliberately does *not* unblock dependents: §3.3's baseline has "no
        handoff triggers", so an unblock would be the coordination layer leaking
        into the run it is supposed to be compared against.
        """
        if self.coordinated:
            unblocked = self.mem.complete_task(self.task.id, self.robot_id)
        else:
            unblocked = []
            self.finished.add(self.task.id)

        if unblocked is None:
            # Another robot owns it now and will finish it. Cool off rather than
            # re-picking it next tick, which is what produced the claimed/lost
            # loop in the first place.
            self.unreachable[self.task.id] = (
                self.last_tick,
                RETRY_UNREACHABLE_AFTER_TICKS,
            )
            self.mem.log_event(
                self.mission_id,
                self.robot_id,
                "task_lost",
                {"task": str(self.task.id), "kind": self.task.kind},
            )
        else:
            self.mem.log_event(
                self.mission_id,
                self.robot_id,
                "task_completed",
                {
                    "task": str(self.task.id),
                    "kind": self.task.kind,
                    "unblocked": [str(u) for u in unblocked],
                },
            )
        self.task = None

    def _abandon(self, reason: str = "invalid") -> None:
        if self.task is not None:
            self.mem.release_task(self.task.id)
            self.mem.log_event(
                self.mission_id,
                self.robot_id,
                "task_released",
                {"task": str(self.task.id), "reason": reason},
            )
        self.task = None


def _plan_first(candidates: list[Task], plan: Any) -> list[Task]:
    """The rules' ranking, with the planner's choice moved to the front.

    Reordering rather than replacing is what keeps a model answer safe: an id
    that no longer exists, or that another robot claims first, costs one failed
    claim and the robot carries on down a list it would have used anyway.
    """
    if plan is None or plan.action != "claim_task" or not plan.task_id:
        return candidates
    chosen = [t for t in candidates if str(t.id) == str(plan.task_id)]
    return chosen + [t for t in candidates if t not in chosen]


def _distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _densest(points: list[tuple[int, int]]) -> tuple[int, int] | None:
    """The point with the most company within CLUSTER_RADIUS.

    A cluster centroid would land between the rooms it averages, and on a map
    dense with walls that is often somewhere unreachable. An actual member of the
    densest group is always a real place, and it is where the next job is.

    Ties break on coordinates so two robots computing this from the same shared
    memory get the same answer, and so a seeded run stays reproducible.
    """
    if not points:
        return None
    return max(
        points,
        key=lambda p: (
            sum(1 for q in points if _distance(p, q) <= CLUSTER_RADIUS),
            -p[0],
            -p[1],
        ),
    )


def allocation_score(role: str, robot: Any, task: Task) -> float:
    """§4.4's greedy allocation score.

    2.0·role_match + 1.2·priority + 1.0·(1/(1+dist)) + 0.5·battery_norm
    """
    role_match = 1.0 if task.kind in ROLE_TASKS.get(role, set()) else 0.0
    tx, ty = task.target
    distance = (abs(robot.x - tx) + abs(robot.y - ty)) if tx is not None else 0
    battery_norm = robot.battery / max(1, ROLES[role]["battery"])
    return (
        2.0 * role_match
        + 1.2 * task.priority
        + 1.0 * (1 / (1 + distance))
        + 0.5 * battery_norm
    )
