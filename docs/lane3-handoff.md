# Lane 3 — sim world & rendering: status

Written after lanes 2 and 3 were built back to back. Lane 3's §5.1 checklist is
complete; what remains for other lanes is at the bottom. Nothing in §5.2's frozen
contracts moved: `Action`, the `StateFrame` *shape*, the `fleetmem` signatures
and `map.json` are untouched.

## What is done

| §5.1 lane 3 item | Where |
|---|---|
| Tick server: state, validation, dynamics per §4.8 | `sim/world.py`, `sim/server.py` (was already done) |
| Tile map loader incl. sectors | `world/map_format.py` (was already done) |
| Layered renderer with client-side lerp | `client/app.js` — §4.8's six layers, smoothstep interpolation across the 250 ms window |
| Sprite atlas | `client/atlas.js` — drawn in code, no downloads, no licensing risk |
| Fog of war, shared vs baseline | three states: unexplored, explored-but-stale, in-vision; baseline dims harder and shows a `PRIVATE MAPS` badge |
| Thought bubbles + click-through provenance | bubbles from `robots[].bubble`; clicking calls `GET /api/plans/{id}` |
| Name tags, event ticker | humanised verbs, colour-coded, capped at 200 lines |
| Websocket diff protocol + reconnect | was already done; reconnect now retires the old socket (see below) |
| Scoreboard + ON/OFF toggle | full §4.7 metric set, `POST /api/mission/restart`, side-by-side once both modes finish |

Extras that earned their place: path ghosts on `task_claimed`, screen shake on
the aftershock, and a sector grid on `S` (FR-16's story, on demand).

## Renderer decisions worth knowing

**Canvas 2D, not PixiJS.** §4.8 asks for Pixi; PixiJS 7 has no automatic canvas
fallback, so a machine without WebGL renders a blank page — a bad failure for a
demo whose deliverable is a video, and one that also makes headless QA
impossible. At 1,200 tiles Canvas 2D is not a compromise. The note is at the top
of `client/app.js`; the writeup should own the deviation rather than hide it.

**Sprites are generated, not downloaded.** §6.1 forbids third-party art and §3.6
allows CC0 *or* hand-drawn. `client/atlas.js` paints a spritesheet once at boot
from `px()` blocks on a 16×16 grid. If lane 5 supplies a CC0 atlas, `buildAtlas`
is the only function that changes — `drawSprite` is all the renderer calls, and
`ASSETS.md` stays lane 5's deliverable.

**Fire does not look like a person.** The first flame sprite was narrow at the
base with a symmetric column above it, which read as a small orange human beside
the amber victim sprites. On a map whose whole job is showing where the people
are, a hazard that can be mistaken for a casualty is a bug, not a style note.

## Server additions (lane 3/4 boundary)

Three endpoints, all read-only except the restart:

```
GET  /api/plans/{robot_id}?limit=5   rationale + trigger + source + resolved based_on
POST /api/mission/restart            {"coordinated": false} -> rebuilds the fleet
GET  /api/runs                       final numbers per mode, for the comparison
```

`sim/mission.py` now exposes `build_fleet()`, used by both the batch runner and
the server. They had drifted — the server always seeded sector tasks and
hard-coded coordinated behaviour — so the toggle would have switched the fog
without switching the fleet underneath it. Lane 4's baseline-mode work is
unaffected: the toggle reuses the same `coordinated=False` path the metrics suite
already measures.

`COLONY_MAP` points the server at another map, which is how the aftershock beat
gets rehearsed without waiting for tick 300.

## Bugs this shook out

Worth knowing because each one was invisible until the browser was actually
driven, and each has a test now:

- **The snapshot carried only the world's live counters.** A browser attaching to
  a finished mission read "8 stabilized" beside "rescue rate 0%" — two true
  numbers from two sources, disagreeing on screen.
- **A restarted mission never ticked.** A finished mission stops its own tick
  loop, and the usual reason to press the toggle is having just watched one
  finish; the new world was built, broadcast, then sat at tick 0 looking exactly
  like a hung server.
- **Every reconnect left the old socket subscribed.** After two server restarts
  each frame was applied three times and the aftershock printed as three separate
  earthquakes.
- **Half-finished runs were compared.** Toggling away mid-run recorded those
  numbers as a result, and the scoreboard cheerfully reported that coordination
  made things worse. Runs now carry `finished`, and the gain line refuses to
  compute otherwise.

## Still open, for other lanes

**Lane 5 — the demo map is too easy.** A working fleet clears Aftershock v1 in
~250 ticks, so the tick-300 aftershock never fires in a normal run.
`test_the_demo_map_is_neither_trivial_nor_hopeless` and
`test_the_aftershock_fires_during_the_mission` are `xfail(strict=True)` and will
fail loudly as XPASS the moment the map is retuned. Knobs, cheapest first: move
the escalation to tick 150–200; add victims; deepen the debris; tighten vitals
deadlines. Verified with `COLONY_MAP` and the escalation at tick 45: the fleet
rescues **9/9 including the victim the aftershock reveals**, so the beat works —
the map just never reaches it.

**Lane 4 — the console and the orchestrator.** `GET /api/plans/{id}` is the same
join the commander console needs for FR-10's "why did robot X do Y"; reuse it or
query `plans` directly. `Worker.orchestrated` is the flag that switches on the 5s
self-claim wait once an orchestrator exists.

**Repo hygiene.** `infra/__pycache__/*.pyc` are committed and show as modified on
every test run — `git rm --cached` them when someone does lane 5's hygiene pass.
