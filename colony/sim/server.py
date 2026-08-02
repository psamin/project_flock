"""Tick server: 4 Hz authoritative simulation + websocket broadcast (§4.1, §4.8).

The server owns truth and ticks at 4 Hz; the browser renders at 60 fps and
interpolates between frames. That split is what makes a slow simulation look
alive, and it only works because the server never sends anything the client has
to guess about.

Run it:  make sim     (then open http://localhost:8000)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agents.scout import Scout, seed_sector_tasks, split_sectors
from agents.worker import Worker
from bedrock.adapter import adapter_from_env
from sim.world import World
from world.map_format import load_map

TICK_HZ = 4
TICK_SECONDS = 1 / TICK_HZ
# Frames a viewer may fall behind before it is dropped: two seconds at 4 Hz.
# Enough to ride out a slow paint, short enough that a wedged tab is noticed.
QUEUE_FRAMES = 8

ROOT = Path(__file__).resolve().parents[1]
CLIENT_DIR = ROOT / "client"
DEFAULT_MAP = ROOT / "world" / "maps" / "aftershock.json"


def _make_memory():
    """CockroachDB when it is reachable, the in-memory fake otherwise.

    The walking skeleton has to run on a laptop with no cluster — if the sim
    refused to start without one, nobody could work on the renderer.
    """
    if os.environ.get("COLONY_MEMORY") == "fake":
        from fleetmem.fake import FakeFleetMem

        return FakeFleetMem(), "fake"
    try:
        from fleetmem.client import CockroachFleetMem

        return CockroachFleetMem(), "cockroach"
    except Exception as exc:                       # noqa: BLE001 - any failure means no cluster
        from fleetmem.fake import FakeFleetMem

        print(f"[sim] no CockroachDB ({type(exc).__name__}); using in-memory fleet memory")
        return FakeFleetMem(), "fake"


class Viewer:
    """One browser, with its own outbound queue.

    The queue is what keeps a slow client's problem to itself: frames are handed
    over without ever awaiting the socket, and a viewer that falls more than
    QUEUE_FRAMES behind is dropped rather than allowed to hold up the tick loop.
    """

    def __init__(self, socket: WebSocket):
        self.socket = socket
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_FRAMES)
        self.dropped = False

    def offer(self, payload: str) -> bool:
        """Queue a frame. False when the viewer is too far behind to keep."""
        if self.dropped:
            return False
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            return False
        return True

    def close(self) -> None:
        self.dropped = True

    async def pump(self) -> None:
        """Own the socket: drain the queue until the client goes away."""
        while not self.dropped:
            payload = await self.queue.get()
            await self.socket.send_text(payload)


class Mission:
    """One running mission: the world, its agents, and everyone watching."""

    def __init__(self, map_path: Path = DEFAULT_MAP, seed: int | None = None):
        self.world = World(load_map(map_path), seed=seed)
        self.mission_id = uuid.uuid4()
        self.mem, self.memory_kind = _make_memory()
        self.viewers: set[Viewer] = set()
        self.running = False
        # Held while a frame goes out, and while a joining viewer is snapshotted
        # and registered. Without it those two interleave: registering first lets
        # a diff reach the browser ahead of the snapshot that predates it, and
        # registering last drops the diff entirely. Tile diffs are cumulative, so
        # either way the browser's grid is permanently wrong for that tile.
        self._broadcast_lock = asyncio.Lock()

        embedder = adapter_from_env()
        self.agents: dict[str, Any] = {}
        scouts = [r for r in self.world.robots.values() if r.role == "scout"]
        # Contiguous shares of the map's sector grid, until scouts claim
        # `explore_sector` tasks for themselves (FR-16, Aug 4-6).
        # FR-16: one explore_sector task per sector at bootstrap. Scouts claim
        # them one at a time under a short lease, so two live scouts can never
        # sweep the same ground and a dead scout's sector frees itself. The
        # static shares below remain as the fallback for maps with no grid.
        seed_sector_tasks(self.mem, self.mission_id, self.world.map)
        shares = split_sectors(self.world.map.sectors, len(scouts))
        for i, robot in enumerate(scouts):
            self.mem.register_robot(robot.id, robot.role, (robot.x, robot.y), robot.battery)
            self.agents[robot.id] = Scout(
                robot_id=robot.id, mission_id=self.mission_id,
                mem=self.mem, embedder=embedder, seed=(seed or 0) + i,
                sectors=shares[i],
            )
        for robot in self.world.robots.values():
            if robot.role in ("lifter", "medic"):
                self.mem.register_robot(robot.id, robot.role, (robot.x, robot.y),
                                        robot.battery)
                self.agents[robot.id] = Worker(
                    robot_id=robot.id, role=robot.role,
                    mission_id=self.mission_id, mem=self.mem,
                )

    def tick_once(self) -> dict[str, Any]:
        """One pass of the pipeline. Split out from the loop so tests can drive
        the mission without asyncio or wall-clock time."""
        actions = {
            robot_id: agent.step(self.world) for robot_id, agent in self.agents.items()
        }
        frame = self.world.step(actions)
        for event in frame.events:
            self.mem.log_event(self.mission_id, event["actor"], event["verb"], event["detail"])
        return frame.to_json()

    async def run(self) -> None:
        self.running = True
        while self.running and not self.world.finished:
            frame = self.tick_once()
            await self._broadcast(frame)
            await asyncio.sleep(TICK_SECONDS)
        self.running = False

    async def _broadcast(self, frame: dict[str, Any]) -> None:
        """Hand one frame to every viewer's queue. Never touches a socket.

        The tick loop must not be able to block on a browser. Writing to sockets
        here — even concurrently, even with a timeout — means a wedged client can
        hold things up for as long as that timeout allows, and the mission stops
        for everyone else. Queueing is O(viewers) and cannot block at all; the
        per-viewer pump owns the socket.
        """
        payload = json.dumps(frame)
        async with self._broadcast_lock:
            for viewer in list(self.viewers):
                if not viewer.offer(payload):
                    # Queue full: this viewer is more than QUEUE_FRAMES behind
                    # and will never catch up. Drop it; the browser reconnects
                    # and gets a fresh snapshot.
                    self.viewers.discard(viewer)
                    viewer.close()

    async def attach(self, socket: WebSocket) -> "Viewer":
        """Register a viewer with the current world already queued.

        Snapshotting and registering happen under the broadcast lock and without
        awaiting the socket, so the first frame a browser sees is a snapshot and
        the next is the very next tick's diff — no gap, no reordering, and no way
        for a slow client to stall the simulation while it is joining.
        """
        viewer = Viewer(socket)
        async with self._broadcast_lock:
            viewer.offer(json.dumps(self.world.snapshot().to_json()))
            self.viewers.add(viewer)
        return viewer

    def detach(self, viewer: "Viewer") -> None:
        self.viewers.discard(viewer)
        viewer.close()


mission = Mission()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    """Start the tick loop with the app and stop it cleanly on shutdown."""
    task = asyncio.create_task(mission.run())
    print(f"[sim] mission {mission.mission_id} ticking at {TICK_HZ} Hz "
          f"({mission.memory_kind} memory, {len(mission.agents)} scouts)")
    try:
        yield
    finally:
        mission.running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Colony sim", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True, "tick": mission.world.tick,
        "memory": mission.memory_kind, "agents": sorted(mission.agents),
        "metrics": mission.world.metrics(),
    }


@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    """A viewer gets the full world once, then diffs (§4.8)."""
    await socket.accept()
    viewer = await mission.attach(socket)

    async def watch_for_disconnect() -> None:
        while True:
            await socket.receive_text()            # viewers are read-only for now

    # Whichever finishes first ends the connection: the pump raises when the
    # socket dies, the watcher raises when the client disconnects.
    tasks = [asyncio.create_task(viewer.pump()),
             asyncio.create_task(watch_for_disconnect())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                task.result()
    finally:
        mission.detach(viewer)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(CLIENT_DIR / "index.html")


app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
