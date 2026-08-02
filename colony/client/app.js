/* Colony renderer (PRD §4.8).
 *
 * The server ticks at 4 Hz and owns truth. This renders at 60 fps and tweens
 * each robot from its previous tile to its new one across the 250 ms window.
 * That interpolation is the whole trick: without it a 4 Hz sim reads as a chess
 * clock, with it it reads as a world. Never render raw tick jumps.
 *
 * Layers, drawn bottom to top per §4.8: ground, hazards, entities, floaters.
 * Fog of war, thought bubbles and sprite atlases are Aug 4-6.
 *
 * Canvas 2D rather than the PixiJS §4.8 calls for, deliberately. PixiJS 7 has no
 * automatic canvas fallback: on a machine without WebGL it throws "Unable to
 * auto-detect a suitable renderer" and the page renders nothing at all. That is
 * a bad failure for a demo whose entire deliverable is a video, and it also made
 * the renderer impossible to verify in headless QA. At this scale (1,200 tiles,
 * a handful of entities) Canvas 2D is not a compromise. Pixi earns its place
 * when lane 3 adds sprite atlases and animation cycles; the websocket protocol,
 * layer order and interpolation below are unchanged by that swap.
 */

const TICK_MS = 250; // 4 Hz

// Warm, slightly desaturated base; accents reserved for meaning (§3.6).
const PALETTE = {
  open: "#2a2733",
  wall: "#151319",
  door: "#6b5a3e",
  unstable: "#3d3243",
  debris: "#4a4352",
  rubble_heavy: "#5c5265",
  fire: "#e2603f",
  victim: "#f2c14e",
  victimStabilized: "#6fbf73",
  victimLost: "#6b6472",
};
const ROLE_COLOR = { scout: "#63c5da", lifter: "#d9884a", medic: "#d96a9a" };

let canvas = null;
let ctx = null;
let world = null;
let tile = 32;

let robots = [];
let victims = [];
const motion = new Map();   // robot id -> {fromX, fromY, toX, toY}
let windowStart = 0;

function boot(snapshot) {
  // Called on EVERY snapshot, not just the first. `make sim` runs uvicorn with
  // --reload, so a restart hands the client a different world with the tick
  // counter back at 0. Keeping the old grid and replaying the new mission's
  // diffs onto it leaves two grids that never reconverge.
  world = snapshot.world;
  tile = world.tile_size;
  motion.clear();
  robots = [];
  victims = [];

  const stage = document.getElementById("stage");
  if (!canvas) {
    canvas = document.createElement("canvas");
    stage.appendChild(canvas);
    ctx = canvas.getContext("2d");
    requestAnimationFrame(draw);
  }
  canvas.width = world.width * tile;
  canvas.height = world.height * tile;
}

function tileColor(ground, object) {
  if (object && PALETTE[object]) return PALETTE[object];
  return PALETTE[ground] || PALETTE.open;
}

function applyTileChanges(changes) {
  for (const c of changes || []) {
    world.ground[c.y][c.x] = c.ground;
    world.objects[c.y][c.x] = c.object;
  }
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

function draw() {
  requestAnimationFrame(draw);
  if (!ctx || !world) return;

  const t = Math.min(1, (performance.now() - windowStart) / TICK_MS);
  const eased = t * t * (3 - 2 * t); // smoothstep: no linear snap at the ends

  // 1-2: ground and hazards. Redrawn wholesale; 1,200 fills is nothing.
  for (let y = 0; y < world.height; y++) {
    for (let x = 0; x < world.width; x++) {
      ctx.fillStyle = tileColor(world.ground[y][x], world.objects[y][x]);
      ctx.fillRect(x * tile, y * tile, tile - 1, tile - 1);
    }
  }

  // 3: entities — victims first so robots draw over them.
  const pulse = 0.75 + 0.25 * Math.sin(performance.now() / 300);
  for (const v of victims) {
    // Unknown victims stay hidden: the viewer learns where they are when the
    // fleet does, which is the point of the demo.
    if (v.state === "unknown") continue;
    ctx.globalAlpha = v.state === "located" ? pulse : 1;
    ctx.fillStyle =
      v.state === "stabilized" ? PALETTE.victimStabilized
      : v.state === "lost" ? PALETTE.victimLost
      : PALETTE.victim;
    ctx.beginPath();
    ctx.arc(v.x * tile + tile / 2, v.y * tile + tile / 2, tile * 0.2, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  for (const r of robots) {
    const m = motion.get(r.id);
    const px = (m.fromX + (m.toX - m.fromX) * eased) * tile;
    const py = (m.fromY + (m.toY - m.fromY) * eased) * tile;
    const color = ROLE_COLOR[r.role] || "#ffffff";

    if (r.role === "scout") {
      // Hovering drone: soft shadow under a round body (§3.6 silhouettes).
      ctx.globalAlpha = 0.28;
      ctx.fillStyle = "#000000";
      ctx.beginPath();
      ctx.ellipse(px + tile / 2, py + tile - 5, tile * 0.28, tile * 0.12, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(px + tile / 2, py + tile / 2 - 2, tile * 0.3, 0, Math.PI * 2);
      ctx.fill();
    } else {
      ctx.fillStyle = color;
      ctx.fillRect(px + tile * 0.18, py + tile * 0.18, tile * 0.64, tile * 0.64);
    }

    // 4: floaters — name tags above the sprites.
    ctx.fillStyle = "#e8e3d9";
    ctx.font = "10px monospace";
    ctx.textAlign = "center";
    ctx.fillText(r.id, px + tile / 2, py - 3);
  }
}

function updateHud(metrics) {
  if (!metrics) return;
  document.getElementById("m-tick").textContent = metrics.tick ?? 0;
  document.getElementById("m-located").textContent = metrics.victims_located ?? 0;
  document.getElementById("m-stabilized").textContent = metrics.victims_stabilized ?? 0;
  document.getElementById("m-lost").textContent = metrics.victims_lost ?? 0;
}

function logEvents(events) {
  const ticker = document.getElementById("ticker");
  for (const e of events || []) {
    if (e.verb === "action_rejected") continue; // noise, not signal
    const line = document.createElement("div");
    if (e.verb === "victim_found") line.className = "found";
    if (e.verb === "aftershock") line.className = "shock";
    line.textContent = `${String(e.tick).padStart(4, " ")}  ${e.actor}  ${e.verb}`;
    ticker.appendChild(line);
  }
  ticker.scrollTop = ticker.scrollHeight;
}

function connect() {
  const status = document.getElementById("status");
  const socket = new WebSocket(`ws://${location.host}/ws`);

  socket.onopen = () => { status.textContent = "live"; };
  socket.onerror = () => { status.textContent = "connection error"; };
  socket.onclose = () => {
    status.textContent = "disconnected — retrying";
    setTimeout(connect, 1000);
  };

  socket.onmessage = (message) => {
    const frame = JSON.parse(message.data);
    try {
      if (frame.kind === "snapshot") boot(frame);
      // A diff can arrive before the snapshot: the server registers a viewer
      // before sending it, deliberately, so no frame is skipped. Without a
      // world there is nothing to apply it to, and the next snapshot carries
      // the full grid anyway.
      if (!world) return;
      applyTileChanges(frame.tiles_changed);
      trackRobots(frame.robots || []);
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

connect();
