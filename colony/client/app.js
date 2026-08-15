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
 */

import { buildAtlas, drawSprite, tileSprite, COLOURS } from "./atlas.js";

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

function setText(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
}

function updateHud(metrics) {
  if (!metrics) return;
  setText("m-tick", metrics.tick ?? 0);
  setText("m-located", metrics.victims_located ?? 0);
  setText("m-stabilized", metrics.victims_stabilized ?? 0);
  setText("m-lost", metrics.victims_lost ?? 0);
  setText("m-coverage", `${Math.round((metrics.coverage ?? 0) * 100)}%`);
  setText("m-rescue", `${Math.round((metrics.rescue_rate ?? 0) * 100)}%`);
  setText("m-duplicate", `${Math.round((metrics.duplicate_effort_index ?? 0) * 100)}%`);
  // Computed since the first playtest and never shown. It is the most direct
  // "coordination is working" number the project has: with claiming on it
  // should sit at zero, and the baseline is where it climbs.
  setText("m-doublework", metrics.double_work_incidents ?? 0);
  setText(
    "m-median",
    metrics.median_time_to_stabilize == null
      ? "—"
      : Math.round(metrics.median_time_to_stabilize),
  );
}

/** §3.6: put the memory on screen.
 *
 * The data layer is the thesis and it was the one part of the demo with nothing
 * to look at — a viewer saw robots moving and had to take on faith that any of
 * it went through a database. This shows live row counts grouped by the four
 * memory types the schema is organised around, so "the fleet is writing to
 * CockroachDB right now" is watchable rather than asserted.
 *
 * It also shows the empty tables honestly. `hazards 0` and `mission_memories 0`
 * are real — nothing writes to either — and that is better on screen than
 * discovered.
 */
async function refreshMemoryRail() {
  const box = document.getElementById("memory-rail");
  if (!box) return;
  try {
    const data = await (await fetch("/api/memory")).json();
    if (!data.available) {
      box.textContent = `fleet memory: ${data.memory} — no cluster, counts unavailable`;
      return;
    }
    const groups = Object.entries(data.counts)
      .map(([group, tables]) => {
        const cells = Object.entries(tables)
          .map(([t, n]) => `<span class="${n ? "" : "zero"}">${t} <b>${n}</b></span>`)
          .join(" ");
        return `<span class="grp"><em>${group}</em> ${cells}</span>`;
      })
      .join("");
    box.innerHTML =
      `<span class="grp"><em>cockroachdb</em> <b>${data.total}</b> rows</span>` +
      groups;
  } catch {
    /* the rail is decoration; never let it break the render loop */
  }
}

/** §4.7's one number the video ends on, once both modes have run. */
async function refreshComparison() {
  try {
    const data = await (await fetch("/api/runs")).json();
    const runs = data.runs || {};
    const box = document.getElementById("comparison");
    const co = runs.coordinated;
    const base = runs.baseline;
    if (!co || !base) {
      box.textContent = co || base ? "run the other mode to compare" : "";
      return;
    }
    if (!co.finished || !base.finished) {
      // Comparing a finished run against one that was cut short would report a
      // coordination gain the fleet never earned, in either direction.
      box.textContent = "let each mode run to the end for a fair comparison";
      return;
    }
    const gain =
      base.median_time_to_stabilize && co.median_time_to_stabilize != null
        ? (base.median_time_to_stabilize - co.median_time_to_stabilize) /
          base.median_time_to_stabilize
        : 0;
    // Lives first, percentages second. Measured across 6 seeds, baseline loses
    // exactly five people every run and coordinated loses none: baseline fails,
    // so its mission does not end early, the vitals deadlines arrive, and the
    // victims nobody reached die. That is the §4.7 comparison stated in the unit
    // the scenario is actually about, and it needs no arithmetic from the
    // viewer. The rate and the gain stay — they are the defensible numbers — but
    // they are no longer the first thing read.
    const livesLost =
      base.victims_lost > co.victims_lost
        ? `<b>${base.victims_lost - co.victims_lost} more people died without ` +
          `shared memory</b> (${co.victims_lost} vs ${base.victims_lost}) · `
        : "";
    box.innerHTML =
      livesLost +
      `rescued ${co.victims_stabilized}/${co.victims_total} coordinated ` +
      `vs ${base.victims_stabilized}/${base.victims_total} baseline · ` +
      `coordination gain ${Math.round(gain * 100)}%`;
  } catch {
    /* the scoreboard is decoration until both runs exist */
  }
}

const TICKER_TEXT = {
  victim_found: (e) => `found ${e.detail.victim} at ${e.detail.x},${e.detail.y}`,
  victim_stabilized: (e) => `stabilized ${e.detail.victim}`,
  victim_lost: (e) => `lost ${e.detail.victim}`,
  debris_cleared: (e) => `cleared debris at ${e.detail.x},${e.detail.y}`,
  task_claimed: (e) => `claimed ${e.detail.kind}`,
  task_completed: (e) => `completed ${e.detail.kind}`,
  task_released: (e) => `released a task (${e.detail.reason})`,
  sector_claimed: (e) => `claimed sector ${e.detail.sector}`,
  sector_swept: (e) => `swept sector ${e.detail.sector}`,
  returning_to_base: () => "heading back to base",
  recharged: () => "recharged",
  restocked: () => "restocked kits",
  fire_spread: (e) => `fire spread to ${e.detail.x},${e.detail.y}`,
  aftershock: () => "AFTERSHOCK — the map just changed",
  robot_lost: (e) => `SIGNAL LOST — silent ${e.detail.silent_for_seconds}s`,
  robot_recovered: () => "back on the air",
};

const TICKER_CLASS = {
  victim_found: "found",
  victim_stabilized: "good",
  victim_lost: "bad",
  aftershock: "shock",
  sector_claimed: "sector",
  sector_swept: "sector",
  robot_lost: "bad",
  robot_recovered: "good",
};

function logEvents(events) {
  const ticker = document.getElementById("ticker");
  for (const e of events || []) {
    if (e.verb === "action_rejected" || e.verb === "tile_visited") continue; // noise
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
    line.className = TICKER_CLASS[e.verb] || "";
    const say = TICKER_TEXT[e.verb];
    line.textContent =
      `${String(e.tick).padStart(4, " ")}  ${e.actor.padEnd(6)} ` +
      (say ? say(e) : e.verb.replace(/_/g, " "));
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

/** Which robot a question about "robot X" should ask about.
 *
 * The selected one when the provenance panel is open, otherwise a scout — a
 * lifter has logged nothing until a scout has found somebody to dig out, so
 * defaulting alphabetically makes the flagship question look broken for the
 * first forty ticks of every run. */
function subjectRobot() {
  if (selected) return selected;
  const scout = robots.find((r) => r.role === "scout");
  return scout ? scout.id : robots.length ? robots[0].id : "s1";
}

async function askConsole(question) {
  const summary = document.getElementById("console-summary");
  const sqlBox = document.getElementById("console-sql");
  const rowsBox = document.getElementById("console-rows");
  summary.className = "";
  summary.textContent = "asking fleet memory…";
  sqlBox.textContent = "";
  rowsBox.textContent = "";

  const body = { question };
  if (question === "why_did_robot") body.robot_id = subjectRobot();
  if (question === "what_do_we_know") {
    // Centred on the selected robot when there is one: "what do we know around
    // here" is the question a commander actually asks, and (0,0) is a corner.
    const robot = robots.find((r) => r.id === subjectRobot());
    body.x = robot ? robot.x : 20;
    body.y = robot ? robot.y : 15;
    body.radius = 6;
  }

  try {
    const answer = await (
      await fetch("/api/console/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
    ).json();

    if (answer.error) {
      summary.className = "err";
      summary.textContent = answer.error;
      return;
    }
    summary.textContent = `${answer.prompt} — ${answer.summary}`;
    // The SQL is shown on purpose: FR-10 claims these answers come out of
    // fleet memory, and the query beside the rows is what makes that
    // checkable rather than asserted.
    sqlBox.textContent = answer.sql;
    rowsBox.textContent = answer.rows.length
      ? answer.rows.map((r) => JSON.stringify(r)).join("\n")
      : "(no rows)";
  } catch (err) {
    summary.className = "err";
    summary.textContent = `console error: ${err.message}`;
  }
}

async function buildConsole() {
  const holder = document.getElementById("console-questions");
  const status = document.getElementById("console-status");
  try {
    const data = await (await fetch("/api/console/questions")).json();
    if (!data.available) {
      status.textContent = `unavailable — running on ${data.memory} memory`;
    }
    holder.innerHTML = "";
    for (const q of data.questions) {
      const button = document.createElement("button");
      button.textContent = q.prompt.replace(/\{[^}]+\}/g, "…");
      const tag = document.createElement("span");
      tag.className = "mem";
      tag.textContent = q.memory.toUpperCase();
      button.appendChild(tag);
      button.addEventListener("click", () => askConsole(q.id));
      holder.appendChild(button);
    }
  } catch {
    status.textContent = "console unreachable";
  }
}

buildConsole();

// The comparison changes when a mission *ends*, which is not an event the
// frame stream carries — polling a read-only endpoint every few seconds is
// cheaper than inventing one.
setInterval(refreshComparison, 4000);

document.getElementById("toggle").addEventListener("click", toggleMode);

/** §3.6's first failure beat: kill a robot, watch its work get taken over.
 *
 * AUDIT B measured zero contended claims across a whole normal run, so the
 * lease-takeover branch — the entire "why a database and not a queue" argument
 * — never fires on its own. This is the button that fires it.
 *
 * The answer names the orphaned task so a viewer knows what to watch, and says
 * how long the lease has left, because the fifteen seconds of nothing happening
 * is the part that needs narrating.
 */
document.getElementById("kill-robot").addEventListener("click", async () => {
  const button = document.getElementById("kill-robot");
  button.disabled = true;
  try {
    const res = await (
      await fetch("/api/failure/kill-robot", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      })
    ).json();
    const box = document.getElementById("comparison");
    if (res.error) {
      box.textContent = res.error;
    } else {
      const orphan = res.orphaned_tasks?.[0];
      box.innerHTML =
        `<b>${res.killed} is down</b> at tick ${res.tick}` +
        (orphan
          ? ` — holding ${orphan.kind} at ${orphan.target.join(",")}. ` +
            `Its lease lapses in ${res.lease_seconds}s, then anyone can claim it.`
          : " — it was not holding any work.");
    }
  } catch {
    /* the button is a demo aid; never let it break the render loop */
  } finally {
    button.disabled = false;
  }
});
document.getElementById("panel-close").addEventListener("click", closePanel);
window.addEventListener("keydown", (e) => {
  if (e.key === "s") showSectors = !showSectors;   // FR-16's grid, on demand
  if (e.key === "Escape") closePanel();
});

connect();
