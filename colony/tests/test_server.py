"""Viewer fan-out (§4.8): a browser must never be able to stall the mission."""

import asyncio
import json

import pytest

from sim.server import QUEUE_FRAMES, Mission, Viewer


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
