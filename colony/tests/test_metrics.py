"""§4.7 metrics, and the coordination ON/OFF comparison the demo ends on."""

import pytest

from fleetmem.fake import FakeFleetMem
from sim import metrics as metrics_mod
from sim.metrics import Comparison, Metrics, duplicate_effort
from sim.mission import compare_modes, run_mission
from world.map_format import load_map
from tests.test_map import MAP_PATH


def _event(actor, verb, tick=0, **detail):
    return {"tick": tick, "actor": actor, "verb": verb, "detail": {**detail, "tick": tick}}


# --- rescue rate and timing --------------------------------------------------


def test_rescue_rate_is_stabilized_over_total():
    m = metrics_mod.compute(
        [_event("m1", "victim_stabilized", tick=10, victim="v1")],
        victims_total=4, horizon=1200,
    )
    assert m.victims_stabilized == 1
    assert m.rescue_rate == 0.25


def test_unrescued_victims_are_censored_at_the_horizon():
    """The metric that mattered most to get right.

    Taking the median over only the rescued makes a run that saved one easy
    victim quickly look better than one that saved seven. Measured on the demo
    map before this fix: baseline median 12, coordinated 70, and §4.7's
    coordination gain came out at **-4.8** while the coordinated fleet rescued
    seven times as many people.
    """
    events = [_event("m1", "victim_stabilized", tick=12, victim="v1")]
    m = metrics_mod.compute(events, victims_total=9, horizon=1200)

    assert m.median_time_to_stabilize == 1200.0, "the eight unrescued were ignored"


def test_a_run_that_rescues_more_scores_a_better_median():
    quick_but_few = metrics_mod.compute(
        [_event("m1", "victim_stabilized", tick=12, victim="v1")],
        victims_total=9, horizon=1200,
    )
    slower_but_many = metrics_mod.compute(
        [_event("m1", "victim_stabilized", tick=t, victim=f"v{i}")
         for i, t in enumerate(range(60, 130, 10))],
        victims_total=9, horizon=1200,
    )
    assert slower_but_many.median_time_to_stabilize < quick_but_few.median_time_to_stabilize


# --- duplicate effort --------------------------------------------------------


def test_duplicate_effort_counts_other_robots_ground_only():
    """Retracing your own steps is how you get anywhere; it is not duplicated
    effort. Only ground another robot already covered counts."""
    own_retread = [("s1", 1, 1), ("s1", 2, 1), ("s1", 1, 1)]
    assert duplicate_effort(own_retread) == 0.0

    overlap = [("s1", 1, 1), ("s2", 1, 1)]
    assert duplicate_effort(overlap) == 0.5


def test_duplicate_effort_is_zero_when_robots_never_overlap():
    assert duplicate_effort([("s1", 1, 1), ("s2", 9, 9)]) == 0.0


def test_duplicate_effort_handles_an_empty_mission():
    assert duplicate_effort([]) == 0.0


def test_double_work_counts_a_task_taken_by_two_robots():
    events = [
        _event("l1", "task_claimed", task="t1"),
        _event("l2", "task_claimed", task="t1"),
        _event("l1", "task_claimed", task="t2"),
    ]
    assert metrics_mod.compute(events, victims_total=1, horizon=100).double_work_incidents == 1


# --- coordination gain -------------------------------------------------------


def test_coordination_gain_is_the_median_improvement():
    comparison = Comparison(
        coordinated=Metrics(median_time_to_stabilize=100.0),
        baseline=Metrics(median_time_to_stabilize=400.0),
    )
    assert comparison.coordination_gain == pytest.approx(0.75)


def test_coordination_gain_is_zero_when_the_baseline_rescued_nobody():
    """Dividing by nothing would report an infinite win — the most flattering
    number available and the least defensible in front of a judge."""
    comparison = Comparison(
        coordinated=Metrics(median_time_to_stabilize=100.0),
        baseline=Metrics(median_time_to_stabilize=None),
    )
    assert comparison.coordination_gain == 0.0


# --- the modes, on the demo map ---------------------------------------------


@pytest.fixture(scope="module")
def demo_comparison():
    return compare_modes(load_map(MAP_PATH), FakeFleetMem, seed=3)


def test_coordination_rescues_more_people(demo_comparison):
    """The claim the whole product rests on, measured rather than asserted.
    Coordinated 7 of 9; baseline 1 of 9."""
    assert demo_comparison.coordinated.victims_stabilized > (
        demo_comparison.baseline.victims_stabilized * 2
    )
    assert demo_comparison.coordinated.rescue_rate > demo_comparison.baseline.rescue_rate


def test_the_coordination_gain_is_positive_and_large(demo_comparison):
    """The number §4.7 says the video ends on."""
    assert demo_comparison.coordination_gain > 0.5


def test_baseline_really_is_uncoordinated():
    """A baseline that quietly shares beliefs would flatter every comparison."""
    baseline = run_mission(load_map(MAP_PATH), FakeFleetMem(), coordinated=False,
                           seed=3, max_ticks=60)
    assert baseline.world.shared_vision is False
    sector_tasks = [t for t in baseline.mem._tasks.values()
                    if t["kind"].startswith("explore_sector")]
    assert sector_tasks == [], "baseline was given sector tasks to claim"


def test_the_two_modes_do_not_share_fleet_memory():
    """`compare_modes` takes a factory for exactly this reason: one shared store
    and the baseline inherits the coordinated run's beliefs."""
    comparison = compare_modes(load_map(MAP_PATH), FakeFleetMem, seed=3, max_ticks=40)
    assert comparison.baseline.victims_stabilized <= comparison.coordinated.victims_stabilized


def test_metrics_serialize_for_the_scoreboard(demo_comparison):
    payload = demo_comparison.to_json()
    assert set(payload) == {"coordinated", "baseline", "coordination_gain",
                            "rescue_rate_delta"}
    assert set(payload["coordinated"]) >= {
        "rescue_rate", "median_time_to_stabilize", "duplicate_effort_index",
        "coverage_at_500",
    }
