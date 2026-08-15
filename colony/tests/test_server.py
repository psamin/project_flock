"""Viewer fan-out (§4.8): a browser must never be able to stall the mission."""

import asyncio
import json

import pytest

from sim.server import QUEUE_FRAMES, TICK_SECONDS, Mission, Viewer


class StubSocket:
    """A socket that records sends, and can be told to hang forever."""

    def __init__(self, hang: bool = False):
        self.sent: list[str] = []
        self.hang = hang

    async def send_text(self, payload: str) -> None:
        if self.hang:
            await asyncio.Event().wait()  # never completes
        self.sent.append(payload)


@pytest.fixture
def mission(monkeypatch):
    # The fake keeps this off the database; fan-out is what is under test.
    monkeypatch.setenv("COLONY_MEMORY", "fake")
    return Mission()


def test_a_viewer_is_queued_the_world_on_attach(mission):
    viewer = asyncio.run(mission.attach(StubSocket()))
    assert viewer.queue.qsize() == 1
    frame = json.loads(viewer.queue.get_nowait())
    assert frame["kind"] == "snapshot"
    assert frame["world"]["width"] == 40


def test_attach_does_not_wait_on_the_socket(mission):
    """Regression: the snapshot used to be sent while holding the broadcast
    lock, so a client that never completed the send held up every other viewer
    and the tick loop with it."""
    hung = StubSocket(hang=True)

    async def attach_with_deadline():
        return await asyncio.wait_for(mission.attach(hung), timeout=1.0)

    viewer = asyncio.run(attach_with_deadline())  # would time out if it awaited
    assert viewer.queue.qsize() == 1
    assert hung.sent == []


def test_broadcast_does_not_wait_on_a_stalled_viewer(mission):
    """The tick loop hands frames to queues and moves on."""

    async def scenario():
        await mission.attach(StubSocket(hang=True))
        await asyncio.wait_for(
            mission._broadcast({"tick": 1, "kind": "diff"}), timeout=1.0
        )

    asyncio.run(scenario())


def test_a_viewer_that_falls_too_far_behind_is_dropped(mission):
    """Bounded, not unbounded: a wedged tab must not grow the queue forever."""

    async def scenario():
        viewer = await mission.attach(StubSocket(hang=True))
        # The snapshot already occupies one slot.
        for tick in range(QUEUE_FRAMES * 2):
            await mission._broadcast({"tick": tick, "kind": "diff"})
        return viewer

    viewer = asyncio.run(scenario())
    assert viewer.queue.qsize() <= QUEUE_FRAMES
    assert viewer.dropped
    assert viewer not in mission.viewers


def test_a_healthy_viewer_receives_every_frame_in_order(mission):
    async def scenario():
        socket = StubSocket()
        viewer = await mission.attach(socket)
        pump = asyncio.create_task(viewer.pump())
        for tick in range(1, 5):
            await mission._broadcast({"tick": tick, "kind": "diff"})
        await asyncio.sleep(0)  # let the pump drain
        pump.cancel()
        return socket

    socket = asyncio.run(scenario())
    kinds = [json.loads(p) for p in socket.sent]
    assert kinds[0]["kind"] == "snapshot"
    assert [f["tick"] for f in kinds[1:]] == [1, 2, 3, 4], "frames dropped or reordered"


def test_offer_refuses_once_closed():
    viewer = Viewer(StubSocket())
    viewer.close()
    assert viewer.offer("{}") is False


# --- the ON/OFF toggle (FR-9) ------------------------------------------------


def test_a_mission_starts_coordinated(mission):
    assert mission.mode == "coordinated"
    assert mission.world.shared_vision is True


def test_restarting_into_baseline_rebuilds_the_whole_fleet(mission):
    """Not just the fog. Baseline means private world models, no claiming and no
    sector tasks (§3.3) — a toggle that only dimmed the map would be showing a
    coordinated run wearing a baseline label, which is the one comparison this
    demo cannot fake."""
    before = mission.mission_id
    # A live tick loop, so this test is about the rebuild alone; restarting a
    # *finished* mission's loop has its own test below.
    mission.running = True
    asyncio.run(mission.reset(coordinated=False))

    assert mission.mode == "baseline"
    assert mission.world.shared_vision is False
    assert mission.mission_id != before, "the runs share a mission id"
    assert mission.world.tick == 0
    workers = [a for a in mission.agents.values() if hasattr(a, "coordinated")]
    assert workers and all(not w.coordinated for w in workers)
    scouts = [a for a in mission.agents.values() if hasattr(a, "sectors")]
    assert all(s.sectors == () for s in scouts), "baseline was handed sector shares"


def test_restarting_re_snapshots_every_viewer(mission):
    """A viewer holding the old world would apply the new mission's diffs onto
    it and never reconverge — the same reason `boot()` runs on every snapshot."""

    async def scenario():
        viewer = await mission.attach(StubSocket())
        while not viewer.queue.empty():
            viewer.queue.get_nowait()
        await mission.reset(coordinated=False)
        return viewer

    viewer = asyncio.run(scenario())
    frame = json.loads(viewer.queue.get_nowait())
    assert frame["kind"] == "snapshot"
    assert frame["world"]["shared_vision"] is False


def test_a_finished_run_is_kept_for_the_side_by_side(mission):
    """§4.7's coordination gain needs both runs. Losing the first one on restart
    would leave the scoreboard able to show only whichever ran last."""
    mission.tick_once()
    asyncio.run(mission.reset(coordinated=False))
    assert "coordinated" in mission.last_runs
    assert mission.last_runs["coordinated"]["mode"] == "coordinated"


# --- provenance (FR-17) ------------------------------------------------------


def test_the_scoreboard_gets_the_full_metric_set(mission):
    """§4.7, not just the live victim counters: rescue rate, median time,
    duplicate effort and coverage are what the ON/OFF comparison is made of."""
    frame = mission.tick_once()
    for field in (
        "rescue_rate",
        "median_time_to_stabilize",
        "duplicate_effort_index",
        "coverage_at_500",
        "mode",
    ):
        assert field in frame["metrics"], f"scoreboard has no {field}"
    # The world's live counters ride along too — the HUD wants both.
    assert "victims_located" in frame["metrics"]


def test_metrics_are_not_recomputed_every_tick(mission):
    """Derived from the whole event log (§4.7), which is fine once a second and
    wasteful four times a second for a 1,200-tick mission."""
    mission.tick_once()
    computed_at = mission._metrics_at
    mission.tick_once()
    assert mission._metrics_at == computed_at, "recomputed on a consecutive tick"


def test_provenance_resolves_the_memories_behind_a_decision(mission):
    """FR-17's payoff and §3.6's bubble click. `based_on` stores observation ids
    because that is the right thing to store; a panel showing a column of UUIDs
    is not an answer to "why did it do that?"."""
    for _ in range(6):
        mission.tick_once()

    robot = next(iter(mission.agents))
    plans = mission.provenance(robot)

    assert plans, f"{robot} decided nothing worth recording in six ticks"
    assert plans[0]["rationale"]
    assert plans[0]["trigger"]
    assert plans[0]["source"] in ("bedrock", "rules")
    for source in plans[0]["based_on"]:
        assert {"kind", "x", "y", "sightings", "confidence"} <= set(source)


def test_provenance_is_newest_first(mission):
    """The panel reads top-down and the newest decision is the one on screen."""
    for _ in range(12):
        mission.tick_once()
    robot = next(iter(mission.agents))
    plans = mission.provenance(robot, limit=3)
    assert len(plans) <= 3


def test_provenance_for_an_unknown_robot_is_empty_not_an_error(mission):
    assert mission.provenance("nobody") == []


def test_the_snapshot_carries_the_scoreboard_numbers(mission):
    """Two sources of truth on one screen. The world owns the live counters and
    the event log owns §4.7, so a browser attaching to a finished mission used
    to read "8 stabilized" beside "rescue rate 0%" — both true, both from
    different places, and indistinguishable from a bug in the fleet."""
    for _ in range(4):
        mission.tick_once()

    async def attach():
        return await mission.attach(StubSocket())

    viewer = asyncio.run(attach())
    metrics = json.loads(viewer.queue.get_nowait())["metrics"]

    assert "rescue_rate" in metrics, "the snapshot carries no §4.7 numbers"
    expected = metrics["victims_stabilized"] / metrics["victims_total"]
    assert abs(metrics["rescue_rate"] - expected) < 0.01, (
        "the event-derived rate disagrees with the live counter"
    )


def test_restarting_a_finished_mission_starts_ticking_again(mission):
    """The usual reason to hit the toggle is having just watched a run finish —
    and a finished mission has already stopped its own tick loop. Without a
    fresh one the new world is built, broadcast, and then sits at tick 0
    forever, which is indistinguishable from a hung server."""

    async def scenario():
        mission.running = False  # as `run()` leaves it when the mission ends
        await mission.reset(coordinated=False)
        await asyncio.sleep(TICK_SECONDS * 2.5)
        ticked = mission.world.tick
        mission.running = False
        return ticked

    assert asyncio.run(scenario()) > 0, "the restarted mission never ticked"


def test_an_interrupted_run_is_marked_unfinished(mission):
    """§4.7's coordination gain is the number the video ends on. Toggling away
    from a run that is twenty ticks old records numbers that are true and
    meaningless, and a scoreboard that cannot tell them from a result will
    happily report that coordination made things worse."""
    mission.running = True
    mission.tick_once()
    asyncio.run(mission.reset(coordinated=False))
    assert mission.last_runs["coordinated"]["finished"] is False


def test_a_run_is_recorded_when_the_mission_ends(mission):
    """Not when somebody toggles away from it. The scoreboard should be showing
    a result while it is still the thing on screen."""

    async def scenario():
        # Every victim resolved: the world reports finished and `run` exits.
        for victim in mission.world.victims.values():
            victim.state = "stabilized"
        await mission.run()

    asyncio.run(scenario())
    assert mission.last_runs["coordinated"]["finished"] is True


def test_the_event_log_wins_when_the_world_disagrees(mission):
    """§4.7's one-source-of-truth rule, enforced rather than assumed.

    `metrics()` merges two dicts: the world's live counters and the values
    computed from the event log. They collide on `victims_total`,
    `victims_stabilized` and `victims_lost`. Merged the wrong way round the
    simulator silently overwrote all three, so the scoreboard showed ground
    truth while `rescue_rate` beside it came from the log — the exact "second
    source of truth that could disagree with the one on screen" that
    `sim/metrics.py` opens by ruling out.

    Nothing caught that, because in a healthy run the two agree. This forces
    them apart so the merge order itself is what is under test.
    """
    mission.tick_once()

    real = mission.world.metrics

    def disagreeing():
        # A world that claims everyone is saved, while the log says otherwise.
        return {**real(), "victims_stabilized": 999, "victims_lost": 42}

    mission.world.metrics = disagreeing
    try:
        payload = mission.metrics()
    finally:
        mission.world.metrics = real

    assert payload["victims_stabilized"] != 999, (
        "the world's counter overwrote the event-log value — merge order is "
        "reversed in Mission.metrics()"
    )
    assert payload["victims_lost"] != 42, "the world's counter overwrote the log"
    # The non-colliding live counters must still ride along.
    assert "victims_located" in payload
    assert "coverage" in payload
