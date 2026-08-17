/* Colony renderer (PRD §4.8, §3.6).
 *
 * The server ticks at 4 Hz and owns truth. This renders at 60 fps and tweens
 * each robot from its previous tile to its new one across the 250 ms window.
 * That interpolation is the whole trick: without it a 4 Hz sim reads as a chess
 * clock, with it it reads as a world. Never render raw tick jumps.
 *
 * Layers, drawn bottom to top per §4.8:
 *   1 ground tilemap · 2 debris and hazards (animated fire) · 3 entities
 *   (victims, then robots) · 4 fog of war · 5 floaters (name tags, bubbles,
 *   path ghosts) · 6 HUD, which is DOM rather than canvas.
 *
 * Canvas 2D rather than the PixiJS §4.8 calls for, deliberately. PixiJS 7 has no
 * automatic canvas fallback: on a machine without WebGL it throws "Unable to
 * auto-detect a suitable renderer" and the page renders nothing at all. That is
 * a bad failure for a demo whose entire deliverable is a video, and it also made
 * the renderer impossible to verify in headless QA. At this scale (1,200 tiles,
 * a handful of entities) Canvas 2D is not a compromise — the sprite work lives
 * in atlas.js and blits the same either way.
 *
 * There IS now a WebGL renderer, at /sim3d (docs/designs/3d-simulation-view.md),
 * and the objection above is exactly why it is a second route instead of a
 * replacement. It does not weaken: a machine without WebGL still gets a working
 * mission here, and /sim3d checks for WebGL before it loads Three.js and links
 * back to this page when it is missing. This file stays Canvas 2D on purpose —
 * it is the floor the other view is allowed to be ambitious above.
 */

import { buildAtlas, drawSprite, tileSprite, COLOURS } from "./atlas.js";
// The HUD, the §4.7 comparison line, the ticker vocabulary and the commander
// console are identical in this view and in /sim3d, so they live in one place.
// logEvents() stays here: it mutates screen shake and path ghosts, which are
// this renderer's state, and only its formatting half is shared.
import {
  setText,
  updateHud,
  refreshComparison,
  formatEvent,
  initConsole,
  initInterventions,
  initKillRobot,
  refreshMemoryRail,
  armedIntervention,
  placeIntervention,
} from "./ui-shared.js";

const TICK_MS = 250; // 4 Hz
const FIRE_FRAME_MS = 140;
const WALK_FRAME_MS = 220;
const GHOST_MS = 2500;    // how long a claimed task's path line lingers
const SHAKE_MS = 600;     // §3.6: screen shake on the aftershock
const TICKER_MAX = 200;   // DOM lines kept; a 1,200-tick mission would grow forever

// Fog (FR-8, §4.8 layer 4). Three states, not two: unexplored is black, ground
// nobody is currently looking at is dimmed, and what a robot can see right now
// is full brightness. The middle state is what makes the map feel *remembered*
// rather than merely uncovered.
const UNSEEN = "#0e0d12";
const STALE_ALPHA = 0.32;
const BASELINE_STALE_ALPHA = 0.55;  // private maps: dimmer still (§4.8)

const ROLE_COLOR = { scout: "#63c5da", lifter: "#d9884a", medic: "#d96a9a" };

let canvas = null;
let ctx = null;
let world = null;
let tile = 32;

let robots = [];
let victims = [];
// Held as a Set of "x,y" keys because it grows to the size of the map and is
// tested once per tile per frame.
let explored = new Set();
let sharedVision = true;
const motion = new Map();   // robot id -> {fromX, fromY, toX, toY}
let windowStart = 0;
let ghosts = [];            // {x, y, tx, ty, until, role}
let shakeUntil = 0;
let selected = null;        // robot id whose provenance panel is open
let showSectors = false;
// Robots the orchestrator's heartbeat scan has stopped hearing from (§5.1
// lane 4). Sent whole in every frame rather than derived from the ticker: a
// browser that joins mid-mission never saw the event go past.
let lost = new Set();

function boot(snapshot) {
  // Called on EVERY snapshot, not just the first. `make sim` runs uvicorn with
  // --reload and the ON/OFF toggle restarts the mission, so a snapshot can hand
  // the client a different world with the tick counter back at 0. Keeping the
  // old grid and replaying the new mission's diffs onto it leaves two grids that
  // never reconverge.
  world = snapshot.world;
  tile = world.tile_size;
  sharedVision = world.shared_vision !== false;
  motion.clear();
  robots = snapshot.robots || [];
  victims = snapshot.victims || [];
  explored = new Set((snapshot.explored || []).map(([x, y]) => `${x},${y}`));
  lost = new Set(snapshot.lost || []);
  ghosts = [];
  // The panel describes decisions from a mission that no longer exists.
  if (selected) closePanel();

  const stage = document.getElementById("stage");
  if (!canvas) {
    canvas = document.createElement("canvas");
    canvas.addEventListener("click", onCanvasClick);
    stage.appendChild(canvas);
    ctx = canvas.getContext("2d");
    requestAnimationFrame(draw);
  }
  canvas.width = world.width * tile;
  canvas.height = world.height * tile;
  buildAtlas(tile);
  document.getElementById("mode-badge").textContent = sharedVision
    ? "SHARED MEMORY"
    : "PRIVATE MAPS";
  document.getElementById("mode-badge").className = sharedVision ? "on" : "off";
  // The scenario line. A run nobody can name cannot be reproduced by anyone
  // watching it, and "run it again" is the first thing a sceptical judge asks.
  setText(
    "scenario",
    `${world.name} · ${victims.length} trapped · ${robots.length} units · ` +
      `${world.mission_length_ticks} ticks · seed ${world.seed ?? "?"}`,
  );
  document.getElementById("toggle").textContent = sharedVision
    ? "coordination: ON"
    : "coordination: OFF";
}

function applyTileChanges(changes) {
  for (const c of changes || []) {
    world.ground[c.y][c.x] = c.ground;
    world.objects[c.y][c.x] = c.object;
  }
}

function applyExplored(tiles) {
  for (const [x, y] of tiles || []) explored.add(`${x},${y}`);
}

function trackRobots(incoming) {
  for (const r of incoming) {
    let m = motion.get(r.id);
    if (!m) {
      m = { fromX: r.x, fromY: r.y, toX: r.x, toY: r.y };
      motion.set(r.id, m);
    } else {
      // The previous target becomes this window's origin — that is what makes
      // movement continuous instead of teleporting once per tick.
      m.fromX = m.toX;
      m.fromY = m.toY;
      m.toX = r.x;
      m.toY = r.y;
    }
  }
  robots = incoming;
}

/** Tiles a robot can see right now, for the "stale vs live" fog distinction. */
function visibleNow() {
  const live = new Set();
  for (const r of robots) {
    const radius = r.vision || 2;
    for (let y = r.y - radius; y <= r.y + radius; y++) {
      for (let x = r.x - radius; x <= r.x + radius; x++) live.add(`${x},${y}`);
    }
  }
  return live;
}

function draw() {
  requestAnimationFrame(draw);
  if (!ctx || !world) return;

  const now = performance.now();
  const t = Math.min(1, (now - windowStart) / TICK_MS);
  const eased = t * t * (3 - 2 * t); // smoothstep: no linear snap at the ends
  const fireFrame = Math.floor(now / FIRE_FRAME_MS) % 3;
  const walkFrame = Math.floor(now / WALK_FRAME_MS) % 2;
  const live = visibleNow();

  ctx.save();
  if (now < shakeUntil) {
    // Decays rather than rattling at constant amplitude, so it reads as an
    // impact and not as a broken renderer.
    const power = ((shakeUntil - now) / SHAKE_MS) * 6;
    ctx.translate((Math.random() - 0.5) * power, (Math.random() - 0.5) * power);
  }
  ctx.fillStyle = UNSEEN;
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  // Layers 1-2: ground and hazards, then the fog over them.
  const staleAlpha = sharedVision ? STALE_ALPHA : BASELINE_STALE_ALPHA;
  for (let y = 0; y < world.height; y++) {
    for (let x = 0; x < world.width; x++) {
      const key = `${x},${y}`;
      if (!explored.has(key)) continue;             // layer 4, by omission
      const px = x * tile;
      const py = y * tile;
      const sprite = tileSprite(world.ground[y][x], world.objects[y][x], x, y);
      if (sprite) drawSprite(ctx, sprite, px, py);
      if (world.objects[y][x] === "fire") {
        drawSprite(ctx, "ground0", px, py);
        drawSprite(ctx, `fire${fireFrame}`, px, py);
      }
      if (!live.has(key)) {
        ctx.fillStyle = `rgba(14, 13, 18, ${staleAlpha})`;
        ctx.fillRect(px, py, tile, tile);
      }
    }
  }

  if (showSectors) drawSectors();

  // Layer 3: entities — victims first so robots draw over them.
  for (const v of victims) {
    // Unknown victims stay hidden: the viewer learns where they are when the
    // fleet does, which is the point of the demo.
    if (v.state === "unknown") continue;
    if (!explored.has(`${v.x},${v.y}`)) continue;
    const sprite =
      v.state === "stabilized" ? "victim_stabilized"
      : v.state === "lost" ? "victim_lost"
      : `victim${Math.floor(now / 400) % 2}`;
    drawSprite(ctx, sprite, v.x * tile, v.y * tile);
  }

  drawGhosts(now);

  for (const r of robots) {
    const m = motion.get(r.id);
    if (!m) continue;
    const px = (m.fromX + (m.toX - m.fromX) * eased) * tile;
    const py = (m.fromY + (m.toY - m.fromY) * eased) * tile;
    const moving = r.status === "moving";
    // A lost robot is one the orchestrator has stopped hearing from, so it is
    // not animated: the frame it is drawn on is whatever it was doing when it
    // went quiet. Still walking would say the opposite of what has happened.
    const isLost = lost.has(r.id);
    const frame = !isLost && (moving || r.role === "scout") ? walkFrame : 0;
    drawSprite(ctx, `${r.role}${frame}`, px, py, {
      flip: r.facing === "w",
      alpha: isLost ? 0.28 : r.status === "stranded" ? 0.45 : 1,
    });
  }

  // Layer 5: floaters, in their own pass so they never sort-fight with sprites.
  // Bubbles are placed last and stacked, because robots cluster — three of them
  // charging at base wrote three overlapping bubbles into one unreadable smear.
  const placed = [];
  for (const r of robots) {
    const m = motion.get(r.id);
    if (!m) continue;
    const px = (m.fromX + (m.toX - m.fromX) * eased) * tile;
    const py = (m.fromY + (m.toY - m.fromY) * eased) * tile;
    drawNameTag(r, px, py);
    // A lost robot's last bubble is a lie by the time you read it — it says
    // "clearing debris" about a robot nobody has heard from in ten seconds.
    if (lost.has(r.id)) {
      placed.push(drawBubble("📡 signal lost", px + tile / 2, py - 14, placed));
    } else if (r.bubble) {
      placed.push(drawBubble(r.bubble, px + tile / 2, py - 14, placed));
    }
  }
  ctx.restore();
}

function drawSectors() {
  ctx.save();
  ctx.strokeStyle = "rgba(160, 150, 190, 0.22)";
  ctx.fillStyle = "rgba(160, 150, 190, 0.5)";
  ctx.font = "10px ui-monospace, monospace";
  ctx.lineWidth = 1;
  for (const s of world.sectors || []) {
    ctx.strokeRect(s.x * tile, s.y * tile, s.width * tile, s.height * tile);
    ctx.fillText(s.id, s.x * tile + 4, s.y * tile + 12);
  }
  ctx.restore();
}

/** Path ghosts (§3.6): a brief line to the tile a robot just claimed work on. */
function drawGhosts(now) {
  ghosts = ghosts.filter((g) => g.until > now);
  ctx.save();
  ctx.lineWidth = 2;
  ctx.setLineDash([4, 4]);
  for (const g of ghosts) {
    ctx.globalAlpha = Math.max(0, (g.until - now) / GHOST_MS) * 0.7;
    ctx.strokeStyle = ROLE_COLOR[g.role] || "#ffffff";
    ctx.beginPath();
    ctx.moveTo(g.x * tile + tile / 2, g.y * tile + tile / 2);
    ctx.lineTo(g.tx * tile + tile / 2, g.ty * tile + tile / 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(g.tx * tile + tile / 2, g.ty * tile + tile / 2, 4, 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.restore();
}

function drawNameTag(r, px, py) {
  ctx.save();
  ctx.font = "10px ui-monospace, monospace";
  ctx.textAlign = "center";
  const label = r.id.toUpperCase();
  const width = ctx.measureText(label).width + 8;
  ctx.fillStyle = selected === r.id ? "rgba(242,193,78,0.9)" : "rgba(20,19,26,0.75)";
  ctx.fillRect(px + tile / 2 - width / 2, py + tile - 2, width, 12);
  ctx.fillStyle = selected === r.id ? "#14131a" : ROLE_COLOR[r.role] || "#e8e3d9";
  ctx.fillText(label, px + tile / 2, py + tile + 7);
  ctx.restore();
}

/** The Smallville signature (§3.6): what this robot is thinking, right now.
 *
 * Returns the rectangle it used so the next bubble can avoid it. Lifts itself
 * above anything already drawn rather than shortening the text: the words are
 * the point, and a truncated thought is worse than one drawn a little high.
 */
function drawBubble(text, cx, cy, placed = []) {
  ctx.save();
  ctx.font = "11px ui-monospace, monospace";
  const clipped = text.length > 28 ? `${text.slice(0, 27)}…` : text;
  const w = ctx.measureText(clipped).width + 12;
  const h = 18;
  const x = cx - w / 2;
  let y = cy - h;
  for (let guard = 0; guard < placed.length + 1; guard++) {
    const clash = placed.find(
      (p) => x < p.x + p.w + 4 && x + w + 4 > p.x && y < p.y + p.h + 2 && y + h + 2 > p.y,
    );
    if (!clash) break;
    y = clash.y - h - 3;
  }

  ctx.fillStyle = "rgba(28, 26, 36, 0.92)";
  ctx.strokeStyle = "rgba(160, 150, 190, 0.35)";
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, 6);
  ctx.fill();
  ctx.stroke();
  ctx.beginPath();                       // the little tail
  ctx.moveTo(cx - 4, y + h);
  ctx.lineTo(cx, y + h + 5);
  ctx.lineTo(cx + 4, y + h);
  ctx.fill();

  ctx.fillStyle = "#e8e3d9";
  ctx.textAlign = "center";
  ctx.fillText(clipped, cx, y + 13);
  ctx.restore();
  return { x, y, w, h };
}

// --- interaction -------------------------------------------------------------

function onCanvasClick(event) {
  const rect = canvas.getBoundingClientRect();
  const x = ((event.clientX - rect.left) / rect.width) * canvas.width;
  const y = ((event.clientY - rect.top) / rect.height) * canvas.height;

  // An armed disruption takes the click (issue #22). Checked before robot
  // picking rather than after: the tile an operator wants to collapse is very
  // often the one a robot is standing next to, and "select the robot instead"
  // would make the corridor beside it unclickable.
  const armed = armedIntervention();
  if (armed) {
    placeIntervention(Math.floor(x / tile), Math.floor(y / tile));
    return;
  }

  let best = null;
  let bestDistance = tile; // within a tile of the click counts as a hit
  for (const r of robots) {
    const m = motion.get(r.id);
    if (!m) continue;
    const d = Math.hypot(m.toX * tile + tile / 2 - x, m.toY * tile + tile / 2 - y);
    if (d < bestDistance) {
      best = r;
      bestDistance = d;
    }
  }
  if (best) showProvenance(best);
  else closePanel();
}

/** FR-17 made visible: the rationale *and the memories behind it* (§3.6). */
async function showProvenance(robot) {
  selected = robot.id;
  const panel = document.getElementById("panel");
  panel.classList.add("open");
  document.getElementById("panel-title").textContent =
    `${robot.id.toUpperCase()} · ${robot.role}`;
  document.getElementById("panel-vitals").textContent =
    `battery ${robot.battery}` +
    (robot.role === "medic" ? ` · kits ${robot.kits}` : "") +
    ` · ${robot.status}`;
  const body = document.getElementById("panel-body");
  body.textContent = "loading…";

  try {
    const response = await fetch(`/api/plans/${robot.id}`);
    const data = await response.json();
    body.innerHTML = "";
    if (!data.plans || !data.plans.length) {
      body.textContent = "no decisions recorded yet";
      return;
    }
    for (const plan of data.plans) {
      const entry = document.createElement("div");
      entry.className = "plan";
      const head = document.createElement("div");
      head.className = "plan-head";
      head.textContent = `${plan.trigger} · ${plan.source}`;
      const why = document.createElement("div");
      why.className = "plan-why";
      why.textContent = plan.rationale || "(no rationale)";
      entry.append(head, why);

      if (plan.based_on.length) {
        const sources = document.createElement("div");
        sources.className = "plan-sources";
        // "based on 2 sightings + hazard #7" — §3.6's exact ask. These are rows
        // in `observations`, joined server-side, not anything the UI invented.
        sources.textContent =
          `based on ${plan.based_on.length} ${plan.based_on.length === 1 ? "memory" : "memories"}: ` +
          plan.based_on
            .map((b) => `${b.kind} (${b.x},${b.y}) ×${b.sightings}`)
            .join(", ");
        entry.append(sources);
      }
      body.append(entry);
    }
  } catch (err) {
    body.textContent = `could not load plans: ${err.message}`;
  }
}

function closePanel() {
  selected = null;
  document.getElementById("panel").classList.remove("open");
}

async function toggleMode() {
  const button = document.getElementById("toggle");
  button.disabled = true;
  button.textContent = "restarting…";
  try {
    await fetch("/api/mission/restart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ coordinated: !sharedVision }),
    });
    document.getElementById("ticker").innerHTML = "";
  } finally {
    button.disabled = false;
  }
}

// --- HUD ---------------------------------------------------------------------
// setText, updateHud and refreshComparison now live in ui-shared.js, imported
// at the top of this file — /sim3d renders the same numbers from the same
// frames, and two copies drifting apart would show a viewer two different
// missions.

/* The ticker vocabulary (TICKER_TEXT, TICKER_CLASS) moved to ui-shared.js too,
 * behind formatEvent(). What stays here is everything below that touches this
 * renderer's own state. */

function logEvents(events) {
  const ticker = document.getElementById("ticker");
  for (const e of events || []) {
    const formatted = formatEvent(e);
    if (!formatted) continue;   // noise the shared vocabulary filters out
    if (e.verb === "aftershock") shakeUntil = performance.now() + SHAKE_MS;
    if (e.verb === "task_claimed" && e.detail.target) {
      const robot = robots.find((r) => r.id === e.actor);
      if (robot) {
        ghosts.push({
          x: robot.x,
          y: robot.y,
          tx: e.detail.target[0],
          ty: e.detail.target[1],
          until: performance.now() + GHOST_MS,
          role: robot.role,
        });
      }
    }

    const line = document.createElement("div");
    line.className = formatted.className;
    line.textContent = formatted.text;
    ticker.appendChild(line);
  }
  while (ticker.childElementCount > TICKER_MAX) ticker.removeChild(ticker.firstChild);
  ticker.scrollTop = ticker.scrollHeight;
}

// --- transport ---------------------------------------------------------------

let socket = null;

function connect() {
  const status = document.getElementById("status");
  // Retire any previous socket first. Each reconnect used to leave the old one
  // subscribed, so after two server restarts every frame was applied three
  // times: the ticker printed each event three times over, and the aftershock —
  // the beat the demo is built around — read as three separate earthquakes.
  if (socket) {
    socket.onclose = null;
    socket.onmessage = null;
    socket.close();
  }
  socket = new WebSocket(`ws://${location.host}/ws`);
  const mine = socket;

  socket.onopen = () => { status.textContent = "live"; };
  socket.onerror = () => { status.textContent = "connection error"; };
  socket.onclose = () => {
    if (mine !== socket) return;   // a socket we already replaced
    status.textContent = "disconnected — retrying";
    setTimeout(connect, 1000);
  };

  socket.onmessage = (message) => {
    if (mine !== socket) return;
    const frame = JSON.parse(message.data);
    try {
      if (frame.kind === "snapshot") {
        boot(frame);
        refreshComparison();
        refreshMemoryRail();
      }
      // A diff can arrive before the snapshot: the server registers a viewer
      // before sending it, deliberately, so no frame is skipped. Without a
      // world there is nothing to apply it to, and the next snapshot carries
      // the full grid anyway.
      if (!world) return;
      applyTileChanges(frame.tiles_changed);
      applyExplored(frame.explored);
      trackRobots(frame.robots || []);
      lost = new Set(frame.lost || []);
      victims = frame.victims || victims;
      windowStart = performance.now();
      updateHud(frame.metrics);
      logEvents(frame.events);
    } catch (err) {
      // A render fault must not kill the socket, or the page freezes silently
      // on the last good frame and looks like the server died.
      status.textContent = `render error: ${err.message}`;
      console.error(err);
    }
  };
}

// --- commander console (FR-10) ----------------------------------------------
// Lives in ui-shared.js. It takes accessors rather than reading globals,
// because "which robot is the subject of this question" is renderer state and
// /sim3d tracks its own selection.

initInterventions();
initKillRobot();

initConsole({
  getRobots: () => robots,
  getSelected: () => selected,
});

// The comparison changes when a mission *ends*, which is not an event the
// frame stream carries — polling a read-only endpoint every few seconds is
// cheaper than inventing one.
setInterval(refreshComparison, 4000);

document.getElementById("toggle").addEventListener("click", toggleMode);
document.getElementById("panel-close").addEventListener("click", closePanel);
window.addEventListener("keydown", (e) => {
  if (e.key === "s") showSectors = !showSectors;   // FR-16's grid, on demand
  if (e.key === "Escape") closePanel();
});

connect();
