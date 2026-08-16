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

import psycopg
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agents.planning import Planner
from bedrock.adapter import REPLAY, adapter_from_env
from console import questions as console_questions
from console.reader import NotReadOnly, ReadOnlyReader
from orchestrator.lost import LostWatch
from sim import metrics as metrics_mod
from sim import recall as recall_mod
from sim.mission import build_fleet
from sim.world import World
from world.map_format import load_map

TICK_HZ = 4
TICK_SECONDS = 1 / TICK_HZ
# Frames a viewer may fall behind before it is dropped: two seconds at 4 Hz.
# Enough to ride out a slow paint, short enough that a wedged tab is noticed.
QUEUE_FRAMES = 8
# §4.7's metrics are derived from the whole event log, so they are recomputed
# once a second rather than four times (see Mission.metrics).
METRICS_EVERY_TICKS = TICK_HZ
# The lost scan is one indexed read and is measured in wall-clock seconds, so
# running it four times a second would ask the same question four times for the
# same answer.
LOST_SCAN_EVERY_TICKS = TICK_HZ

# Semantic memory (§4.0): read what earlier missions on this map learned, and
# write this one down when it ends. A kill switch rather than a code change,
# because the one place this could misbehave is in front of an audience — and
# `COLONY_RECALL=0` also gives the demo a way to show the cold-start run and the
# remembering run back to back on the same binary.
RECALL_ENABLED = os.environ.get("COLONY_RECALL", "1") != "0"

ROOT = Path(__file__).resolve().parents[1]
CLIENT_DIR = ROOT / "client"
# `COLONY_MAP` points the server at another map — a playtest variant, or a
# tuned copy for rehearsing the aftershock beat without waiting for tick 300.
DEFAULT_MAP = Path(
    os.environ.get("COLONY_MAP", ROOT / "world" / "maps" / "aftershock.json")
)


def _make_memory():
    """CockroachDB when it is reachable, the in-memory fake otherwise.

    The walking skeleton has to run on a laptop with no cluster — if the sim
    refused to start without one, nobody could work on the renderer. So an
    unreachable cluster degrades to the fake.

    **But only when nobody named a cluster.** Setting `COLONY_DSN` is a
    statement that fleet memory lives at a specific address, and falling back
    from that is the most expensive failure this file can have: the mission
    runs, the UI looks perfect, and every claim the demo makes is quietly
    false — no serializable claiming, no vector index, no reconcile gate, and
    nothing persisted for the console or the next mission to read. A typo in a
    Cloud DSN would look exactly like a working demo. So when `COLONY_DSN` is
    set, a connection failure is fatal and says why.

    `COLONY_MEMORY=fake` remains the way to ask for the fake on purpose.
    """
    if os.environ.get("COLONY_MEMORY") == "fake":
        from fleetmem.fake import FakeFleetMem

        return FakeFleetMem(), "fake"
    try:
        from fleetmem.client import CockroachFleetMem

        return CockroachFleetMem(), "cockroach"
    except Exception as exc:  # noqa: BLE001 - any failure means no cluster
        if os.environ.get("COLONY_DSN"):
            raise RuntimeError(
                f"COLONY_DSN is set but the cluster is unreachable "
                f"({type(exc).__name__}: {exc}).\n"
                "Refusing to start on in-memory memory: the mission would run and "
                "look healthy while writing nothing to CockroachDB.\n"
                "Fix the DSN, or set COLONY_MEMORY=fake to ask for the fake."
            ) from exc
        from fleetmem.fake import FakeFleetMem

        print(
            f"[sim] no CockroachDB ({type(exc).__name__}); using in-memory fleet memory"
        )
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
        self.map_path = map_path
        self.seed = seed
        self.mem, self.memory_kind = _make_memory()
        self.viewers: set[Viewer] = set()
        self.running = False
        # Bedrock at plan boundaries (§4.3). Replay mode without a cassette
        # yields nothing and every robot runs on rules, which is why the server
        # starts with no AWS credentials at all. Built once and reused across
        # restarts: the cassette and the thread pool outlive any one mission.
        self.embedder = adapter_from_env()
        self.planner = Planner(adapter=self.embedder)
        # Final §4.7 numbers per mode, so the scoreboard can show ON against OFF
        # side by side (FR-9) rather than only whichever ran last.
        self.last_runs: dict[str, dict[str, Any]] = {}
        # Held while a frame goes out, and while a joining viewer is snapshotted
        # and registered. Without it those two interleave: registering first lets
        # a diff reach the browser ahead of the snapshot that predates it, and
        # registering last drops the diff entirely. Tile diffs are cumulative, so
        # either way the browser's grid is permanently wrong for that tile.
        self._broadcast_lock = asyncio.Lock()
        # The tick loop, when one is running. Held so a restart can tell a
        # finished mission from a live one.
        self._loop: asyncio.Task | None = None
        # The commander console's own connection, opened on first use (FR-10).
        # Separate from `self.mem` on purpose: the sim writes and the console
        # does not, so they are different identities against the same cluster.
        self._reader: ReadOnlyReader | None = None
        self._build(coordinated=True)

    # --- the commander console (FR-10) ------------------------------------

    @property
    def console_available(self) -> bool:
        """The console reads SQL out of fleet memory, so it needs fleet memory
        to be a database. On the fake it would answer every question with
        nothing, which reads as "the fleet did not do that" rather than as
        "there is no cluster here"."""
        return self.memory_kind == "cockroach"

    def reader(self) -> ReadOnlyReader:
        if self._reader is None:
            self._reader = ReadOnlyReader()
        return self._reader

    # --- mission lifecycle ------------------------------------------------

    def _build(self, *, coordinated: bool) -> None:
        """Stand up a fresh world, mission and fleet in this coordination mode."""
        self.coordinated = coordinated
        self.world = World(load_map(self.map_path), seed=self.seed)
        self.world.shared_vision = coordinated
        self.mission_id = uuid.uuid4()
        # What earlier missions on this map taught us, for the ticker and the
        # HUD. Populated by build_fleet, which is where recall is read.
        self.recalled: list[Any] = []
        self.agents = build_fleet(
            self.world,
            self.mem,
            self.mission_id,
            coordinated=coordinated,
            seed=self.seed,
            embedder=self.embedder,
            planner=self.planner,
            recall_enabled=RECALL_ENABLED,
            recalled=self.recalled,
        )
        self._metrics: dict[str, Any] = {}
        self._metrics_at = -1
        # Lane 4's heartbeat scan (§4.4). Scoped to this fleet because `robots`
        # has no mission_id — see LostWatch. Live only: `run_mission` is the
        # seeded, deterministic path §3.5 records the golden run from, and
        # lostness is measured against a wall clock, so wiring it in there would
        # make an identical seed produce a different event log on a slow laptop.
        self.lost_watch = LostWatch(self.mem, self.mission_id, self.agents)
        self._lost_at = -1

    async def reset(self, *, coordinated: bool) -> None:
        """Restart the mission in the other coordination mode (FR-9).

        The tick loop reads `self.world` and `self.agents` once per tick and
        never awaits inside `tick_once`, so swapping them between ticks cannot
        tear. Doing it under the broadcast lock is what keeps a viewer from
        receiving a diff for the old world after the snapshot for the new one.
        """
        async with self._broadcast_lock:
            # Recorded with whether the mission actually ended: toggling away
            # from a run twenty ticks old stores numbers that are true and
            # meaningless, and §4.7's coordination gain is the one number the
            # video ends on.
            self.record_run()
            self._build(coordinated=coordinated)
            snapshot = self._snapshot()
            for viewer in list(self.viewers):
                if not viewer.offer(snapshot):
                    self.viewers.discard(viewer)
                    viewer.close()

        # A mission that ran to completion stopped its own tick loop, and the
        # usual reason to hit the toggle is that you have just watched one
        # finish. Without this the new world is built, broadcast, and then sits
        # at tick 0 forever — which looks exactly like a hung server.
        if not self.running:
            self._loop = asyncio.create_task(self.run())

    @property
    def mode(self) -> str:
        return "coordinated" if self.coordinated else "baseline"

    def metrics(self) -> dict[str, Any]:
        """The §4.7 numbers, recomputed on the belief-read cadence.

        Derived from the event log like every other metric (§4.7), which means
        walking the whole mission's events — cheap at a few thousand rows, but
        not four times a second for the life of a mission, so it is cached for a
        second at a time. The live counters the world already tracks ride along,
        because the scoreboard wants both.

        **The world's counters go in first so the event-log values win.** They
        collide on `victims_total`, `victims_stabilized` and `victims_lost`, and
        merged the other way round the simulator silently overwrote all three —
        so the scoreboard showed ground truth while `rescue_rate` beside it was
        computed from the log. That defeats the promise `sim/metrics.py` opens
        with: one source of truth, and it is the log. `victims_located`,
        `coverage` and `tick` do not collide and survive either way.
        """
        if self.world.tick - self._metrics_at < METRICS_EVERY_TICKS and self._metrics:
            return {**self.world.metrics(), **self._metrics}
        computed = metrics_mod.compute(
            self.mem.events(self.mission_id),
            victims_total=len(self.world.victims),
            coverage_at_500=self.world.coverage(),
            ticks=self.world.tick,
            horizon=self.world.map.mission_length_ticks,
            # Read back out of fleet memory rather than counted off the
            # simulator, so the number on the scoreboard is one a judge can
            # reproduce with a SELECT.
            victims_located=len(self.mem.get_beliefs(self.mission_id, kind="victim")),
        )
        self._metrics = {
            **computed.to_json(),
            "mode": self.mode,
            # Semantic memory and Bedrock, on the scoreboard rather than only in
            # /health. Both are claims the demo makes out loud — "the fleet has
            # done this before", "the LLM is deciding" — and a claim nobody can
            # see on screen is a claim a judge has to take on trust.
            "recalled_memories": len(self.recalled),
            "bedrock_mode": self.embedder.mode,
            "bedrock_calls": self.embedder.calls,
        }
        self._metrics_at = self.world.tick
        return {**self.world.metrics(), **self._metrics}

    def focus_point(self) -> tuple[int, int] | None:
        """Somewhere worth asking "what do we know about here?" about.

        `what_do_we_know` takes x/y, and the console's static defaults are the
        origin — a corner of the map where nothing is ever observed, so a cold
        start answered "nothing has been observed near there". That is an empty
        panel in the demo's best feature, caused by a default rather than by an
        absence of memory.

        The best-known victim is the honest answer: it is the thing the fleet
        has the most to say about. The point used is echoed back in the
        rendered prompt, so the operator always sees which area was answered
        for rather than being quietly redirected.
        """
        beliefs = self.mem.get_beliefs(self.mission_id, kind="victim")
        if beliefs:
            best = max(beliefs, key=lambda b: (b.sightings or 0, b.confidence or 0.0))
            return best.pos
        robot = next(iter(self.world.robots.values()), None)
        return None if robot is None else (robot.x, robot.y)

    def provenance(self, robot_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """Why this robot did what it did, with its sources resolved (FR-17).

        `based_on` holds observation ids, which are the right thing to store and
        the wrong thing to show: §3.6 wants "based on 2 sightings + hazard #7",
        not a column of UUIDs. Resolving them here keeps the join on the server
        where the memory already is, and keeps the client a renderer.
        """
        beliefs = {b.id: b for b in self.mem.get_beliefs(self.mission_id)}
        plans = self.mem.plans_for(self.mission_id, robot_id)[-limit:]
        return [
            {
                "trigger": plan.trigger,
                "rationale": plan.rationale,
                "chosen": plan.chosen,
                "source": (plan.chosen or {}).get("source", "rules"),
                "based_on": [
                    {
                        "kind": beliefs[bid].kind,
                        "x": beliefs[bid].pos[0],
                        "y": beliefs[bid].pos[1],
                        "sightings": beliefs[bid].sightings,
                        "confidence": round(beliefs[bid].confidence, 2),
                    }
                    for bid in (plan.based_on or [])
                    if bid in beliefs
                ],
            }
            for plan in reversed(plans)  # newest first: the panel reads top-down
        ]

    def tick_once(self) -> dict[str, Any]:
        """One pass of the pipeline. Split out from the loop so tests can drive
        the mission without asyncio or wall-clock time."""
        actions = {
            robot_id: agent.step(self.world) for robot_id, agent in self.agents.items()
        }
        frame = self.world.step(actions)
        # One round trip for the whole tick's events rather than one each: a
        # busy tick logs several, and against a Cloud cluster the latency
        # dominates the insert.
        self.mem.log_events(
            self.mission_id,
            [(e["actor"], e["verb"], e["detail"]) for e in frame.events],
        )
        frame.lost = self._scan_for_lost(frame)
        self._announce_recall(frame)
        payload = frame.to_json()
        payload["metrics"] = self.metrics()
        return payload

    def _announce_recall(self, frame: Any) -> None:
        """Put what the fleet remembered on the ticker, once, on tick 1.

        Recall happens in `build_fleet`, before the first tick and before any
        viewer is attached, so without this it is invisible: the mission simply
        starts, and the fact that it started *knowing something* never reaches
        the screen. `memory_recalled` is already in the event log — this is the
        same append-to-frame treatment `_scan_for_lost` gives the lost edges,
        for the same reason, and is likewise not re-logged.
        """
        if self.world.tick != 1 or not self.recalled:
            return
        top = self.recalled[0]
        sectors = recall_mod.hot_sectors(self.recalled)
        frame.events.append(
            {
                "tick": self.world.tick,
                "actor": "fleet",
                "verb": "memory_recalled",
                "detail": {
                    "memories": len(self.recalled),
                    "distance": round(top.distance, 3),
                    "sectors": sectors,
                },
            }
        )

    def _scan_for_lost(self, frame: Any) -> list[str]:
        """Run the heartbeat scan on its own cadence and put the edges on the
        ticker (§5.1 lane 4).

        `LostWatch` has already written both transitions to `events`, so they
        are appended to the frame here rather than logged again — the ticker
        prints the frame, the commander console reads the table, and a verb
        written twice would be counted twice by anything aggregating the log.
        """
        if self.world.tick - self._lost_at >= LOST_SCAN_EVERY_TICKS:
            self._lost_at = self.world.tick
            scan = self.lost_watch.scan()
            for robot_id in scan.newly_lost:
                frame.events.append(
                    {
                        "tick": self.world.tick,
                        "actor": robot_id,
                        "verb": "robot_lost",
                        "detail": {"silent_for_seconds": self.lost_watch.after_seconds},
                    }
                )
            for robot_id in scan.recovered:
                frame.events.append(
                    {
                        "tick": self.world.tick,
                        "actor": robot_id,
                        "verb": "robot_recovered",
                        "detail": {},
                    }
                )
        return self.lost_watch.lost_ids()

    async def run(self) -> None:
        self.running = True
        while self.running and not self.world.finished:
            frame = self.tick_once()
            await self._broadcast(frame)
            await asyncio.sleep(TICK_SECONDS)
        self.running = False
        if self.world.finished:
            # Recorded the moment the mission ends, not when somebody navigates
            # away from it: a run that has just finished is exactly the one the
            # scoreboard should be showing, and waiting for a toggle meant the
            # numbers appeared only after they stopped being on screen.
            self.record_run()

    def record_run(self) -> None:
        """Keep this mode's numbers for the ON/OFF comparison (FR-9, §4.7)."""
        self.last_runs[self.mode] = {
            **self.metrics(),
            "finished": self.world.finished,
        }
        if self.world.finished and self.coordinated:
            self._remember()

    def _remember(self) -> None:
        """Write what this mission learned into semantic memory (§4.0).

        Finished, coordinated runs only. An unfinished run has nothing settled
        to teach, and a baseline run is a control condition — a memory it wrote
        would be read by a later coordinated run and leak across the very
        comparison the ON/OFF toggle exists to make.

        Never allowed to fail loudly. This runs at the exact moment a mission
        ends, which is the moment somebody is watching the screen; a summarizer
        that raises would take the scoreboard down with it. `remember_mission`
        is idempotent per mission, so the second call this makes on a toggle
        away from an already-finished run is a no-op rather than a duplicate.
        """
        try:
            recall_mod.write_memory(
                self.mem,
                self.embedder,
                self.mission_id,
                self.world.map,
                self.metrics(),
            )
        except Exception as exc:  # noqa: BLE001 - a demo must not die here
            print(f"[sim] could not write mission memory: {type(exc).__name__}: {exc}")

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

    async def attach(self, socket: WebSocket) -> Viewer:
        """Register a viewer with the current world already queued.

        Snapshotting and registering happen under the broadcast lock and without
        awaiting the socket, so the first frame a browser sees is a snapshot and
        the next is the very next tick's diff — no gap, no reordering, and no way
        for a slow client to stall the simulation while it is joining.
        """
        viewer = Viewer(socket)
        async with self._broadcast_lock:
            viewer.offer(self._snapshot())
            self.viewers.add(viewer)
        return viewer

    def _snapshot(self) -> str:
        """The full world, with the scoreboard's numbers rather than the world's.

        `World.snapshot()` carries the live counters it owns — victims located,
        stabilized, coverage. The §4.7 set is derived from the event log and the
        world knows nothing about it, so a browser attaching to a mission that
        has already finished would show 8 rescued at a rescue rate of 0%: two
        true numbers from two sources, disagreeing on screen.
        """
        payload = self.world.snapshot().to_json()
        payload["metrics"] = self.metrics()
        # Who is already lost, not who just became lost: a browser joining a
        # mission in progress never saw the transition go by.
        payload["lost"] = self.lost_watch.lost_ids()
        return json.dumps(payload)

    def detach(self, viewer: Viewer) -> None:
        self.viewers.discard(viewer)
        viewer.close()


mission = Mission()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    """Start the tick loop with the app and stop it cleanly on shutdown."""
    task = asyncio.create_task(mission.run())
    # "N robots", not "N scouts": the fleet is 2 scouts, a lifter and a medic,
    # and the heterogeneity is the premise. Also says what was recalled, so the
    # cold run and the remembering run are distinguishable from the log alone.
    print(
        f"[sim] mission {mission.mission_id} ticking at {TICK_HZ} Hz "
        f"({mission.memory_kind} memory, {len(mission.agents)} robots, "
        f"{len(mission.recalled)} recalled)"
    )
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
        "ok": True,
        "tick": mission.world.tick,
        "memory": mission.memory_kind,
        "mode": mission.mode,
        "agents": sorted(mission.agents),
        "lost": mission.lost_watch.lost_ids(),
        "metrics": mission.metrics(),
        # Nested rather than flat because `mode` up there is already the
        # coordination mode (§4.7) and these are two different modes.
        #
        # `requested` against `mode` is the whole point of reporting both:
        # `adapter_from_env` silently downgrades live to replay when no
        # credentials resolve, which is exactly the failure that leaves the
        # fleet running on rules while looking perfectly healthy. Read off the
        # environment rather than stored at build time, so a downgrade shows up
        # as the disagreement it is.
        "bedrock": {
            "requested": os.environ.get("COLONY_BEDROCK_MODE", REPLAY),
            **mission.embedder.status(),
        },
    }


@app.get("/api/plans/{robot_id}")
async def plans(robot_id: str, limit: int = 5) -> dict[str, Any]:
    """Why a robot did what it did (FR-17) — the bubble click in §3.6.

    Read-only, and read straight out of fleet memory rather than from anything
    the renderer was told: what a judge sees when they click a robot is the same
    rows the commander console queries.
    """
    if robot_id not in mission.agents:
        return {"robot": robot_id, "plans": [], "error": "no such robot"}
    return {"robot": robot_id, "plans": mission.provenance(robot_id, limit=limit)}


@app.post("/api/mission/restart")
async def restart(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flip coordination ON/OFF and start the mission again (FR-9).

    The whole fleet is rebuilt, not just the fog: baseline means private world
    models, no claiming and no sector tasks (§3.3), which is exactly the
    `coordinated=False` path the metrics suite measures.
    """
    coordinated = bool((body or {}).get("coordinated", True))
    await mission.reset(coordinated=coordinated)
    return {
        "mode": mission.mode,
        "tick": mission.world.tick,
        "previous": mission.last_runs,
    }


@app.get("/api/console/questions")
async def console_catalog() -> dict[str, Any]:
    """The five canned demo questions (§5.1 lane 4, FR-10).

    Canned rather than free-form: §5.4 puts demo reliability first, and a model
    improvising SQL live is the one part of the console that can fail in a way
    nobody can recover from on camera.
    """
    return {
        "available": mission.console_available,
        "memory": mission.memory_kind,
        "mission_id": str(mission.mission_id),
        "questions": console_questions.catalog(),
    }


@app.post("/api/console/ask")
async def console_ask(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Answer one canned question from live fleet memory, read-only.

    The SQL comes back with the answer on purpose. FR-10's claim is that the
    console reads the fleet's actual memory — showing the query alongside the
    rows is what lets a judge check that rather than take it on faith.
    """
    body = body or {}
    question_id = body.get("question")
    if not mission.console_available:
        return {
            "error": (
                f"the console reads SQL from fleet memory, and this mission is "
                f"running on {mission.memory_kind} memory — start a cluster "
                f"(make dev) and restart the sim"
            )
        }
    params = {k: v for k, v in body.items() if k != "question"}
    # A question that asks about a place, asked without one, gets the mission's
    # own focus rather than the console's static origin default. Supplied here
    # for the same reason mission_id is: only the running mission knows it.
    question = console_questions.BY_ID.get(question_id)
    if question is not None and "x" in question.params and "x" not in params:
        focus = mission.focus_point()
        if focus is not None:
            params["x"], params["y"] = focus
    # Semantic memory is scoped by map, not by mission, and only the running
    # mission knows which map it is on — the same reason mission_id is injected
    # rather than accepted. It is also what stops the console being asked about
    # a map the fleet is not on.
    if question is not None and "map_key" in question.params:
        params["map_key"] = recall_mod.map_key(mission.world.map)
    try:
        result = console_questions.answer(
            mission.reader(),
            question_id,
            mission.mission_id,
            **params,
        )
    except console_questions.UnknownQuestion as exc:
        return {"error": str(exc)}
    except NotReadOnly as exc:  # pragma: no cover - the canned set is all reads
        return {"error": f"refused: {exc}"}
    except KeyError as exc:
        return {"error": f"missing parameter {exc}"}
    except psycopg.Error as exc:
        # A parameter the cluster could not use — `limit="lots"`, a malformed
        # id. Values are bound rather than interpolated, so this is a rejected
        # argument and never a rewritten statement; it should read as a bad
        # question, not as a crashed console.
        return {"error": f"the cluster refused that: {str(exc).splitlines()[0]}"}

    return {
        "question": result.question_id,
        "prompt": result.prompt,
        "memory": result.memory,
        "sql": result.sql,
        "summary": result.summary,
        "rows": json.loads(json.dumps(result.rows, default=str)),
    }


@app.get("/api/runs")
async def runs() -> dict[str, Any]:
    """Final numbers per mode, for the side-by-side scoreboard (FR-9, §4.7)."""
    return {"current": mission.mode, "runs": mission.last_runs}


@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    """A viewer gets the full world once, then diffs (§4.8)."""
    await socket.accept()
    viewer = await mission.attach(socket)

    async def watch_for_disconnect() -> None:
        while True:
            await socket.receive_text()  # viewers are read-only for now

    # Whichever finishes first ends the connection: the pump raises when the
    # socket dies, the watcher raises when the client disconnects.
    tasks = [
        asyncio.create_task(viewer.pump()),
        asyncio.create_task(watch_for_disconnect()),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(
                WebSocketDisconnect, RuntimeError, asyncio.CancelledError
            ):
                task.result()
    finally:
        mission.detach(viewer)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(CLIENT_DIR / "index.html")


app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
