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

from agents.scout import Scout, split_sectors
from bedrock.adapter import adapter_from_env
from sim.world import World
from world.map_format import load_map

TICK_HZ = 4
TICK_SECONDS = 1 / TICK_HZ
# Shorter than a tick: a viewer that cannot absorb a frame within one tick is
# already falling behind, and waiting longer would hold up the simulation.
SEND_TIMEOUT = TICK_SECONDS

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


class Mission:
    """One running mission: the world, its agents, and everyone watching."""

    def __init__(self, map_path: Path = DEFAULT_MAP, seed: int | None = None):
        self.world = World(load_map(map_path), seed=seed)
        self.mission_id = uuid.uuid4()
        self.mem, self.memory_kind = _make_memory()
        self.viewers: set[WebSocket] = set()
        self.running = False
        # Held while a frame goes out, and while a joining viewer is snapshotted
        # and registered. Without it those two interleave: registering first lets
        # a diff reach the browser ahead of the snapshot that predates it, and
        # registering last drops the diff entirely. Tile diffs are cumulative, so
        # either way the browser's grid is permanently wrong for that tile.
        self._broadcast_lock = asyncio.Lock()

        embedder = adapter_from_env()
        self.agents: dict[str, Scout] = {}
        scouts = [r for r in self.world.robots.values() if r.role == "scout"]
        # Contiguous shares of the map's sector grid, until scouts claim
        # `explore_sector` tasks for themselves (FR-16, Aug 4-6).
        shares = split_sectors(self.world.map.sectors, len(scouts))
        for i, robot in enumerate(scouts):
            # lifter and medic behaviours are Aug 4-6; only scouts think today.
            self.mem.register_robot(robot.id, robot.role, (robot.x, robot.y), robot.battery)
            self.agents[robot.id] = Scout(
                robot_id=robot.id, mission_id=self.mission_id,
                mem=self.mem, embedder=embedder, seed=(seed or 0) + i,
                sectors=shares[i],
            )
        for robot in self.world.robots.values():
            if robot.role != "scout":
                self.mem.register_robot(robot.id, robot.role, (robot.x, robot.y),
                                        robot.battery)

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
        """Fan out one frame. Concurrent, and bounded per viewer.

        Sending sequentially without a timeout let a single viewer that stopped
        reading fill its send buffer and block the await — which blocks the tick
        loop, which stops the mission for everyone. A browser tab that cannot
        keep up gets dropped instead; it reconnects and receives a fresh
        snapshot.
        """
        if not self.viewers:
            return
        payload = json.dumps(frame)

        async def send(viewer: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(viewer.send_text(payload), timeout=SEND_TIMEOUT)
            except Exception:                      # noqa: BLE001 - a dropped viewer is routine
                return viewer
            return None

        async with self._broadcast_lock:
            viewers = list(self.viewers)
            for dead in await asyncio.gather(*(send(v) for v in viewers)):
                if dead is not None:
                    self.viewers.discard(dead)

    async def attach(self, socket: WebSocket) -> None:
        """Register a viewer and send it the world, atomically w.r.t. broadcasts.

        Snapshotting, sending and registering all happen under the broadcast
        lock, so the first thing a browser sees is a snapshot and the next thing
        is the very next tick's diff — no gap, no reordering.
        """
        async with self._broadcast_lock:
            await socket.send_text(json.dumps(self.world.snapshot().to_json()))
            self.viewers.add(socket)


app = FastAPI(title="Colony sim")
mission = Mission()
_loop_task: asyncio.Task | None = None


@app.on_event("startup")
async def _start() -> None:
    global _loop_task
    _loop_task = asyncio.create_task(mission.run())
    print(f"[sim] mission {mission.mission_id} ticking at {TICK_HZ} Hz "
          f"({mission.memory_kind} memory, {len(mission.agents)} scouts)")


@app.on_event("shutdown")
async def _stop() -> None:
    mission.running = False
    if _loop_task is not None:
        _loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _loop_task


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
    await mission.attach(socket)
    try:
        while True:
            await socket.receive_text()            # viewers are read-only for now
    except WebSocketDisconnect:
        pass
    finally:
        mission.viewers.discard(socket)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(CLIENT_DIR / "index.html")


app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
