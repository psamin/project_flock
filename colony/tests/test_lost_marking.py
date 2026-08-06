"""Lost-marking as the browser and the mission log actually see it (§5.1 lane 4).

`test_orchestrator.py` covers the scan in isolation. What is left is the wiring:
the frame carries the lost set, a browser joining late learns it, a robot that
never stops talking is never marked, and — the one that matters — the label
never touches the work.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from orchestrator.lost import ROBOT_LOST
from sim.server import LOST_SCAN_EVERY_TICKS, Mission


class StubSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


@pytest.fixture
def mission(monkeypatch):
    monkeypatch.setenv("COLONY_MEMORY", "fake")
    return Mission()


def _tick(mission, n: int = 1) -> dict:
    frame = {}
    for _ in range(n):
        frame = mission.tick_once()
    return frame


def _tick_all(mission, n: int) -> list[dict]:
    """Every frame, because the scan runs on its own cadence: a transition lands
    on whichever tick the scan fired, not on the last one."""
    return [mission.tick_once() for _ in range(n)]


# --- the happy path: nobody is lost -----------------------------------------


def test_a_healthy_fleet_reports_nobody_lost(mission):
    """Every agent heartbeats every tick, so a normal run never marks anyone."""
    frame = _tick(mission, LOST_SCAN_EVERY_TICKS + 1)
    assert frame["lost"] == []
    assert not any(e["verb"] == ROBOT_LOST for e in frame["events"])


def test_the_frame_always_carries_the_field(mission):
    """Contract 3: absent is not the same as empty. The renderer does
    `new Set(frame.lost || [])`, but a field that only appears sometimes is how
    a client ends up rendering stale state on the frames where it is missing."""
    frame = _tick(mission)
    assert "lost" in frame
    assert isinstance(frame["lost"], list)
    json.dumps(frame)  # it goes over a websocket


# --- a robot actually going quiet -------------------------------------------


def test_a_robot_that_stops_heartbeating_reaches_the_frame_and_the_log(mission):
    """The scan is wall-clock, so the window is set to zero rather than slept
    through: every robot is 'silent' the instant it is asked."""
    mission.lost_watch.after_seconds = 0
    frames = _tick_all(mission, LOST_SCAN_EVERY_TICKS + 1)

    assert sorted(frames[-1]["lost"]) == sorted(mission.agents)
    lost_events = {
        e["actor"] for f in frames for e in f["events"] if e["verb"] == ROBOT_LOST
    }
    assert lost_events == set(mission.agents)

    logged = [
        e for e in mission.mem.events(mission.mission_id) if e["verb"] == ROBOT_LOST
    ]
    assert {e["actor"] for e in logged} == set(mission.agents)


def test_the_transition_is_printed_once_not_every_tick(mission):
    """The ticker shows this. Repeating it four times a second would push every
    other event off the panel within seconds."""
    mission.lost_watch.after_seconds = 0
    _tick(mission, LOST_SCAN_EVERY_TICKS + 1)
    later = [_tick(mission) for _ in range(3 * LOST_SCAN_EVERY_TICKS)]

    assert not any(
        e["verb"] == ROBOT_LOST for frame in later for e in frame["events"]
    ), "re-announced a robot that was already lost"
    # Still lost, though — the state persists even though the edge does not.
    assert sorted(later[-1]["lost"]) == sorted(mission.agents)


def test_the_frame_and_the_event_log_do_not_double_count(mission):
    """`LostWatch` already wrote to `events`; the frame copy is for the ticker.
    Logging it again would make anything aggregating the mission log count each
    loss twice."""
    mission.lost_watch.after_seconds = 0
    _tick(mission, LOST_SCAN_EVERY_TICKS + 1)

    logged = [
        e for e in mission.mem.events(mission.mission_id) if e["verb"] == ROBOT_LOST
    ]
    assert len(logged) == len(mission.agents)


# --- a browser joining after the fact ---------------------------------------


def test_a_browser_joining_late_is_told_who_is_already_lost(mission):
    """The reconnect bug class lane 3 documented: an edge-triggered field tells
    a client nothing when the edge happened before it connected."""
    mission.lost_watch.after_seconds = 0
    _tick(mission, LOST_SCAN_EVERY_TICKS + 1)

    viewer = asyncio.run(mission.attach(StubSocket()))
    snapshot = json.loads(viewer.queue.get_nowait())

    assert snapshot["kind"] == "snapshot"
    assert sorted(snapshot["lost"]) == sorted(mission.agents)


def test_a_restart_starts_from_a_clean_slate(mission):
    """The toggle rebuilds the fleet against a new mission id (FR-9). Carrying
    the previous run's lost set over would open the new mission with robots
    declared lost that are standing at base."""
    mission.lost_watch.after_seconds = 0
    _tick(mission, LOST_SCAN_EVERY_TICKS + 1)
    assert mission.lost_watch.lost_ids()

    asyncio.run(mission.reset(coordinated=False))

    assert mission.lost_watch.lost_ids() == []
    assert _tick(mission)["lost"] == []


# --- the guard that matters -------------------------------------------------


def test_marking_the_fleet_lost_does_not_stop_the_mission(mission):
    """FR-5/FR-11: lostness is a label. A fleet that is entirely 'lost' — the
    orchestrator's view — still works, because nothing on the recovery path
    consults it."""
    mission.lost_watch.after_seconds = 0
    before = mission.world.metrics()["victims_located"]
    _tick(mission, 60)

    assert sorted(mission.lost_watch.lost_ids()) == sorted(mission.agents)
    assert mission.world.tick == 60
    assert mission.world.metrics()["victims_located"] >= before
    assert any(
        e["verb"] in ("task_claimed", "sector_claimed")
        for e in mission.mem.events(mission.mission_id)
    ), "the fleet stopped working once it was marked lost"
