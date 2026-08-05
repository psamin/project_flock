"""Lost-marking, and the two ways it could quietly break FR-5.

The interesting tests here are not "a silent robot is marked lost" — that is one
query. They are the guards that the marker stays *off the recovery path*: a
lost-marker that releases tasks, or that renews the leases of the robot it just
declared dead, passes any test that only checks the label.
"""

from __future__ import annotations

import uuid

import pytest

from orchestrator.lost import ROBOT_LOST, ROBOT_RECOVERED, LostWatch


# `robots` has no mission_id (§4.5), so the table is shared by every test that
# ever ran against this database. Unique ids per test keep them from colliding.
def _robot(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _verbs(mem, mission_id, robot_id):
    return [e["verb"] for e in mem.events(mission_id) if e["actor"] == robot_id]


# --- the basic transition ---------------------------------------------------


def test_a_silent_robot_is_marked_lost(mem, mission):
    robot = _robot("s")
    mem.register_robot(robot, "scout", (1, 1), 120)

    watch = LostWatch(mem, mission, [robot], after_seconds=0)
    scan = watch.scan()

    assert scan.newly_lost == [robot]
    assert watch.lost_ids() == [robot]
    assert ROBOT_LOST in _verbs(mem, mission, robot)


def test_a_heartbeating_robot_is_never_marked_lost(mem, mission):
    robot = _robot("s")
    mem.register_robot(robot, "scout", (1, 1), 120)
    mem.heartbeat(robot, pos=(2, 2), battery=119)

    # An hour of silence would be needed; the robot just spoke.
    watch = LostWatch(mem, mission, [robot], after_seconds=3600)
    scan = watch.scan()

    assert not scan
    assert watch.lost_ids() == []
    assert _verbs(mem, mission, robot) == []


def test_lostness_is_edge_triggered_not_repeated_every_scan(mem, mission):
    """A robot that stays silent is logged once.

    §4.7's metrics and the commander console both read `events`. A verb emitted
    four times a second for the rest of the mission would swamp both, and the
    scoreboard's event ticker would show nothing else.
    """
    robot = _robot("s")
    mem.register_robot(robot, "scout", (1, 1), 120)
    watch = LostWatch(mem, mission, [robot], after_seconds=0)

    first = watch.scan()
    later = [watch.scan() for _ in range(5)]

    assert first.newly_lost == [robot]
    assert not any(later), "re-logged a robot that was already lost"
    assert _verbs(mem, mission, robot).count(ROBOT_LOST) == 1


def test_a_robot_that_comes_back_is_marked_recovered(mem, mission):
    robot = _robot("s")
    mem.register_robot(robot, "scout", (1, 1), 120)

    watch = LostWatch(mem, mission, [robot], after_seconds=0)
    watch.scan()
    assert watch.lost_ids() == [robot]

    # It speaks again, and the window widens to something it satisfies.
    mem.heartbeat(robot, pos=(2, 2), battery=118)
    watch.after_seconds = 3600
    scan = watch.scan()

    assert scan.recovered == [robot]
    assert watch.lost_ids() == []
    assert _verbs(mem, mission, robot) == [ROBOT_LOST, ROBOT_RECOVERED]


# --- the roster guard -------------------------------------------------------


def test_robots_outside_this_fleet_are_ignored(mem, mission):
    """`robots` is the fleet table, not the mission table (§4.5).

    Without the roster filter a bare `stale_robots()` also returns every robot
    from every mission that ever ran against this database, and a demo run
    would open by declaring last week's fleet lost.
    """
    mine = _robot("mine")
    stranger = _robot("stranger")
    mem.register_robot(mine, "scout", (1, 1), 120)
    mem.register_robot(stranger, "lifter", (2, 2), 300)

    watch = LostWatch(mem, mission, [mine], after_seconds=0)
    scan = watch.scan()

    assert scan.newly_lost == [mine]
    assert watch.lost_ids() == [mine]
    assert _verbs(mem, mission, stranger) == []


def test_an_empty_fleet_scans_clean(mem, mission):
    watch = LostWatch(mem, mission, [], after_seconds=0)
    assert not watch.scan()
    assert watch.lost_ids() == []


# --- the guards that matter (FR-5) ------------------------------------------


def test_marking_lost_does_not_release_the_robots_task(mem, mission):
    """Recovery is lease-native (§4.4). The marker must not race it.

    If the watch released work, there would be two recovery paths — one
    transactional and one on a polling timer — and "robot loss self-heals with
    no supervisor on the recovery path" would stop being true.
    """
    robot = _robot("l")
    mem.register_robot(robot, "lifter", (1, 1), 300)
    task = mem.create_task(mission, "clear_debris", (5, 5))
    assert mem.claim_task(task, robot)  # a full-length lease, still live

    watch = LostWatch(mem, mission, [robot], after_seconds=0)
    assert watch.scan().newly_lost == [robot]

    # Still held: the lease has not lapsed, so nobody else may take it yet.
    assert task not in [t.id for t in mem.open_tasks(mission)]
    assert mem.claim_task(task, _robot("other")) is False


def test_marking_lost_does_not_renew_the_lost_robots_leases(mem, mission):
    """The trap this whole module is shaped around.

    `heartbeat()` is the SDK method that writes robot status — and it also
    stamps `heartbeat_at = now()` and renews every lease the robot holds. A
    lost-marker built on it would resurrect the robot on the next scan *and*
    keep the dead robot's work leased out forever, which is the fleet stall
    FR-11 rules out. So: claim with an already-expired lease, scan repeatedly,
    and another robot must still be able to take the work.
    """
    dead = _robot("l")
    alive = _robot("l2")
    mem.register_robot(dead, "lifter", (1, 1), 300)
    task = mem.create_task(mission, "clear_debris", (5, 5))
    assert mem.claim_task(task, dead, lease_seconds=0)

    watch = LostWatch(mem, mission, [dead], after_seconds=0)
    for _ in range(5):
        watch.scan()

    assert task in [t.id for t in mem.open_tasks(mission)], "lease was renewed"
    assert mem.claim_task(task, alive), "a dead robot's work never came back"


def test_a_lost_robot_stays_lost_across_scans_without_new_events(mem, mission):
    robot = _robot("m")
    mem.register_robot(robot, "medic", (1, 1), 200)
    watch = LostWatch(mem, mission, [robot], after_seconds=0)

    watch.scan()
    for _ in range(3):
        watch.scan()
        assert watch.lost_ids() == [robot]

    assert _verbs(mem, mission, robot) == [ROBOT_LOST]


def test_the_whole_fleet_going_silent_is_reported_in_a_stable_order(mem, mission):
    """Sorted, not set-iteration order: the ticker renders this list."""
    fleet = sorted(_robot("r") for _ in range(4))
    for robot in fleet:
        mem.register_robot(robot, "scout", (1, 1), 120)

    watch = LostWatch(mem, mission, fleet, after_seconds=0)
    scan = watch.scan()

    assert scan.newly_lost == fleet
    assert watch.lost_ids() == fleet


def test_lost_then_recovered_then_lost_again_logs_both_edges(mem, mission):
    robot = _robot("s")
    mem.register_robot(robot, "scout", (1, 1), 120)
    watch = LostWatch(mem, mission, [robot], after_seconds=0)

    watch.scan()  # lost
    mem.heartbeat(robot)
    watch.after_seconds = 3600
    watch.scan()  # recovered
    watch.after_seconds = 0
    scan = watch.scan()  # lost again

    assert scan.newly_lost == [robot]
    assert _verbs(mem, mission, robot) == [ROBOT_LOST, ROBOT_RECOVERED, ROBOT_LOST]


@pytest.mark.parametrize("after_seconds", [0, 5, 10, 3600])
def test_the_window_is_the_only_knob(mem, mission, after_seconds):
    """Whatever the window, a robot that has just spoken is never lost and the
    scan never touches its work."""
    robot = _robot("s")
    mem.register_robot(robot, "scout", (1, 1), 120)
    task = mem.create_task(mission, "explore_sector", (0, 0))
    assert mem.claim_task(task, robot)

    watch = LostWatch(mem, mission, [robot], after_seconds=after_seconds)
    watch.scan()

    # Held either way: lostness is a label, never a release.
    assert task not in [t.id for t in mem.open_tasks(mission)]
