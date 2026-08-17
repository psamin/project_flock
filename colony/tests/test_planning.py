"""Bedrock inside the agent loop (§4.3, §3.5, FR-17).

Three properties matter more than the plumbing, and each has a test here:

  * a fleet with no AWS credentials makes exactly the same decisions it would
    have made anyway — the planner declines rather than inventing;
  * a robot never waits on a plan (§3.5) and never exceeds 4 calls a minute;
  * every prompt's beliefs are recorded as `based_on`, which is what makes the
    commander console's "why?" answerable (FR-17).
"""

import json
import uuid
from concurrent.futures import Future

import pytest

from agents.planning import (
    PLAN_CALLS_PER_MINUTE,
    TICKS_PER_MINUTE,
    Planner,
    build_digest,
    role_card,
    task_lines,
)
from bedrock.adapter import LIVE, RECORD, REPLAY, BedrockAdapter, Plan
from fleetmem.fake import FakeFleetMem
from fleetmem.types import Task
from sim.world import Robot


def _robot(role="medic", x=5, y=5):
    return Robot(id=f"{role[0]}1", role=role, x=x, y=y, battery=100, kits=2)


def _task(kind="deliver_kit", target=(6, 6), priority=1):
    return Task(
        id=uuid.uuid4(),
        mission_id=uuid.uuid4(),
        kind=kind,
        target=target,
        status="open",
        priority=priority,
    )


# --- role cards and digests --------------------------------------------------


def test_a_role_card_describes_this_robot_not_robots_in_general():
    card = role_card(_robot("lifter"))
    assert "l1" in card and "debris" in card


def test_a_medics_card_carries_its_kit_count():
    """The model cannot choose a delivery it has no kit for, so the number has
    to be in the prompt rather than discovered by being refused (§3.3)."""
    robot = _robot("medic")
    robot.kits = 1
    assert "1 supply kits" in role_card(robot)


def test_the_digest_records_the_beliefs_it_used(fake):
    """FR-17: `based_on` is the prompt's own source list, not a re-query. A
    digest that reported ids it did not use would make the commander console
    confidently wrong."""
    mission = uuid.uuid4()
    near = fake.report_observation(mission, "s1", "victim", (6, 6))
    fake.report_observation(mission, "s1", "victim", (39, 29))  # far away

    digest = build_digest(fake, mission, _robot(), radius=4)

    assert digest.ids == (near,)
    assert "victim at (6,6)" in digest.text


def test_a_digest_with_nothing_to_report_still_says_so(fake):
    """An empty digest must not become an empty prompt section: the model reads
    "(nothing reported near you yet)" as information, and a blank reads as a
    formatting bug."""
    digest = build_digest(fake, uuid.uuid4(), _robot())
    assert digest.ids == () and "nothing reported" in digest.text


def test_the_digest_is_bounded(fake):
    """§4.3 budgets the prompt at ~1.5k tokens. Beliefs are the part that grows
    without limit as a mission runs."""
    mission = uuid.uuid4()
    # Distinct kinds, because the reconcile gate is doing its job: forty
    # sightings of the same kind within five tiles are one belief, which is the
    # wrong thing to be testing here.
    for i in range(40):
        fake.report_observation(mission, "s1", f"probe{i}", (i % 8, i // 8))

    digest = build_digest(fake, mission, _robot(), limit=12)
    assert len(digest.ids) == 12
    assert len(digest.text.splitlines()) == 12


# --- the rate cap and the fallback -------------------------------------------


def test_replay_without_a_cassette_declines_rather_than_inventing(fake):
    """The whole no-credentials story. The offline path would happily return a
    plan, but it would be the same rules the agent already runs — dressed up as
    a model decision, with a rationale a judge would reasonably read as the
    model's reasoning."""
    planner = Planner(adapter=BedrockAdapter())  # replay, empty cassette
    assert (
        planner.plan(_robot(), 1, build_digest(fake, uuid.uuid4(), _robot()), [])
        is None
    )


def test_a_recorded_decision_is_replayed(fake, tmp_path):
    """§4.3's `--seeded` mode: a golden run replays the decisions it recorded,
    which is what makes the demo reproducible."""
    robot, task = _robot(), _task()
    digest = build_digest(fake, uuid.uuid4(), robot)
    adapter = BedrockAdapter(cassette_path=tmp_path / "c.json")
    key_probe = BedrockAdapter(mode=RECORD, cassette_path=tmp_path / "c.json")
    # Write the cassette the way `record` mode would, without touching AWS.
    key_probe._client = _FakeClient(
        json.dumps(
            {"action": "claim_task", "task_id": str(task.id), "rationale": "closest"}
        )
    )
    key_probe.plan(role_card(robot), digest.text, task_lines([task]))

    adapter = BedrockAdapter(cassette_path=tmp_path / "c.json")
    plan = Planner(adapter=adapter).plan(robot, 1, digest, [task])

    assert plan is not None and plan.task_id == str(task.id)
    assert plan.rationale == "closest"


def test_a_robot_gets_four_plans_a_minute_and_no_more(fake, tmp_path):
    """§3.5's hard cap, counted in ticks so a seeded run cannot drift with the
    wall clock. Over the cap the robot is not blocked — it falls back to rules,
    which is the same code path as having no credentials at all."""
    robot, task = _robot(), _task()
    digest = build_digest(fake, uuid.uuid4(), robot)
    planner = Planner(adapter=_AlwaysAnswers())

    granted = [
        planner.plan(robot, tick, digest, [task]) is not None
        for tick in range(0, TICKS_PER_MINUTE, 10)
    ]
    assert sum(granted) == PLAN_CALLS_PER_MINUTE

    # A minute later the budget is back.
    assert planner.plan(robot, TICKS_PER_MINUTE + 1, digest, [task]) is not None


def test_the_cap_is_per_robot(fake):
    """Six robots sharing one planner must not share one budget — a scout that
    replans twice would silence the medic."""
    digest = build_digest(fake, uuid.uuid4(), _robot())
    planner = Planner(adapter=_AlwaysAnswers())
    scout, medic = _robot("scout"), _robot("medic")

    for tick in range(PLAN_CALLS_PER_MINUTE):
        planner.plan(scout, tick, digest, [_task()])

    assert planner.plan(scout, 10, digest, [_task()]) is None
    assert planner.plan(medic, 10, digest, [_task()]) is not None


# --- never blocking (§3.5) ---------------------------------------------------


def test_a_live_call_does_not_stall_the_robot(fake, monkeypatch):
    """§3.5: "a robot continues its current action while a plan is in flight".
    The tick loop is synchronous, so a planner that waited would stop the whole
    mission — six robots, four seconds each, at 4 Hz."""
    robot, task = _robot(), _task()
    digest = build_digest(fake, uuid.uuid4(), robot)
    planner = Planner(adapter=_AlwaysAnswers(live=True))
    pending: Future = Future()
    monkeypatch.setattr(planner, "_submit", lambda *a: pending)

    assert planner.plan(robot, 1, digest, [task]) is None  # in flight, not blocked

    pending.set_result(Plan(action="claim_task", task_id=str(task.id), rationale="ok"))
    landed = planner.plan(robot, 2, digest, [task])
    assert landed is not None and landed.task_id == str(task.id)


def test_a_failed_call_leaves_the_robot_on_its_rules(fake, monkeypatch):
    """Bedrock failing after its own retries is a plan we do not have — never an
    exception inside a tick that is running a rescue."""
    robot = _robot()
    digest = build_digest(fake, uuid.uuid4(), robot)
    planner = Planner(adapter=_AlwaysAnswers(live=True))
    failed: Future = Future()
    failed.set_exception(RuntimeError("bedrock is having a day"))
    monkeypatch.setattr(planner, "_submit", lambda *a: failed)

    planner.plan(robot, 1, digest, [_task()])
    assert planner.plan(robot, 2, digest, [_task()]) is None


def test_only_one_call_per_robot_is_in_flight(fake, monkeypatch):
    """Four ticks a second times a three-second round trip is twelve overlapping
    calls per robot, all answering a question the first one already covered."""
    robot = _robot()
    digest = build_digest(fake, uuid.uuid4(), robot)
    planner = Planner(adapter=_AlwaysAnswers(live=True))
    submissions = []

    def _submit(*args):
        submissions.append(args)
        return Future()

    monkeypatch.setattr(planner, "_submit", _submit)
    for tick in range(1, 10):
        planner.plan(robot, tick, digest, [_task()])

    assert len(submissions) == 1


# --- doubles -----------------------------------------------------------------


class _FakeClient:
    """Stands in for bedrock-runtime so `record` mode can write a cassette."""

    def __init__(self, text):
        self.text = text

    def invoke_model(self, modelId, body):
        payload = json.dumps({"content": [{"text": self.text}]}).encode()
        return {"body": _Body(payload)}


class _Body:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload


class _AlwaysAnswers:
    """An adapter that always has an answer, so a test can isolate the rate cap
    and the concurrency rules from whether a cassette happens to hit."""

    def __init__(self, live=False):
        # LIVE rather than RECORD: the two used to be interchangeable here, and
        # are not any more. RECORD now calls the adapter synchronously so the
        # cassette traces the trajectory replay will retrace; LIVE is the async
        # path these tests are about.
        self.mode = LIVE if live else REPLAY

    def knows_plan(self, *args, **kwargs):
        return True

    def plan(self, *args, **kwargs):
        return Plan(action="explore", rationale="always")


@pytest.fixture
def fake():
    return FakeFleetMem()
