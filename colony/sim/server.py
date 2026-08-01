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

from agents.scout import Scout
from bedrock.adapter import adapter_from_env
from sim.world import World
from world.map_format import load_map

TICK_HZ = 4
TICK_SECONDS = 1 / TICK_HZ

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

        embedder = adapter_from_env()
        self.agents: dict[str, Scout] = {}
        scouts = [r for r in self.world.robots.values() if r.role == "scout"]
        for i, robot in enumerate(scouts):
            # lifter and medic behaviours are Aug 4-6; only scouts think today.
            self.mem.register_robot(robot.id, robot.role, (robot.x, robot.y), robot.battery)
            self.agents[robot.id] = Scout(
                robot_id=robot.id, mission_id=self.mission_id,
                mem=self.mem, embedder=embedder, seed=(seed or 0) + i,
                sector=i, sector_count=len(scouts),
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
        if not self.viewers:
            return
        payload = json.dumps(frame)
        dead = []
        for viewer in self.viewers:
            try:
                await viewer.send_text(payload)
            except Exception:                      # noqa: BLE001 - a dropped viewer is routine
                dead.append(viewer)
        for viewer in dead:
            self.viewers.discard(viewer)


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
    await socket.send_text(json.dumps(mission.world.snapshot().to_json()))
    mission.viewers.add(socket)
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
