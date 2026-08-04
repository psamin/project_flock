/* The sprite sheet, drawn in code (PRD §3.6, §6.1).
 *
 * No downloads and no image files: §6.1 forbids third-party art in the
 * submission, §3.6 allows CC0 packs *or* hand-drawn, and a repo that needs an
 * asset pack fetched before it renders is a repo that breaks on somebody else's
 * laptop the day before the deadline. Everything here is painted once into an
 * offscreen canvas at boot and then blitted, so the per-frame cost is a
 * drawImage regardless of how fussy the pixel work gets.
 *
 * Real pixel art, not smoothed shapes: every sprite is built from `px()` blocks
 * on a 16x16 grid scaled to the tile size, which is what gives the chunky
 * readable silhouettes §3.6 asks for. Squint at a robot and you should know its
 * role before you read its name tag.
 *
 * Lane 5 swapping in a CC0 atlas later replaces this file alone — `drawSprite`
 * is the only thing the renderer calls.
 */

// Sprites are authored on a 16x16 grid and scaled to the map's tile size, so
// the art is resolution-independent without anybody redrawing it.
const GRID = 16;

// §3.6: warm, slightly desaturated, with accent hues reserved for meaning.
const C = {
  ground: ["#3b3547", "#413a4e"],      // two variants so the floor is not flat
  groundEdge: "#332e3f",
  wall: "#161320",
  wallTop: "#221d2e",
  door: "#7a6242",
  doorFrame: "#4a3a28",
  unstable: "#403347",
  unstableCrack: "#2b2231",
  debris: "#585065",
  debrisDark: "#463f52",
  rubble: "#6a5f76",
  rubbleDark: "#514859",
  fire: ["#f2a03f", "#e2603f", "#f2c14e"],
  fireCore: "#fff1c9",
  ember: "#7a2d1e",
  scout: "#63c5da",
  scoutDark: "#3d8ca0",
  lifter: "#d9884a",
  lifterDark: "#a15f31",
  medic: "#d96a9a",
  medicDark: "#a04570",
  metal: "#8f8aa0",
  metalDark: "#5d596b",
  glass: "#bfeaf5",
  victim: "#f2c14e",
  victimSkin: "#e8b98a",
  stabilized: "#6fbf73",
  lost: "#6b6472",
  shadow: "rgba(0,0,0,0.30)",
};

// Every sprite the renderer can ask for, in the order they are laid out.
const SPRITES = [
  "ground0", "ground1", "wall", "door", "unstable",
  "debris", "rubble_heavy",
  "fire0", "fire1", "fire2",
  "scout0", "scout1",
  "lifter0", "lifter1",
  "medic0", "medic1",
  "victim0", "victim1", "victim_stabilized", "victim_lost",
];

let sheet = null;
let cell = 32;
const index = new Map();

/** Fill a block on the 16x16 authoring grid. */
function px(ctx, x, y, w, h, colour) {
  ctx.fillStyle = colour;
  ctx.fillRect(x * (cell / GRID), y * (cell / GRID), w * (cell / GRID), h * (cell / GRID));
}

function drawGround(ctx, variant) {
  px(ctx, 0, 0, 16, 16, C.ground[variant]);
  // A couple of darker flecks, placed by hand rather than randomly so the
  // tiling never produces a seam that reads as a grid line.
  px(ctx, 3, 5, 2, 1, C.groundEdge);
  px(ctx, 11, 10, 2, 1, C.groundEdge);
  if (variant === 1) px(ctx, 6, 2, 1, 2, C.groundEdge);
}

function drawWall(ctx) {
  px(ctx, 0, 0, 16, 16, C.wall);
  px(ctx, 0, 0, 16, 4, C.wallTop);      // lit top face: gives the block height
  px(ctx, 0, 4, 16, 1, "#0d0a14");
}

function drawDoor(ctx) {
  px(ctx, 0, 0, 16, 16, C.doorFrame);
  px(ctx, 2, 1, 12, 14, C.door);
  px(ctx, 10, 8, 2, 2, "#d8c9a0");      // handle
}

function drawUnstable(ctx) {
  px(ctx, 0, 0, 16, 16, C.unstable);
  // Cracks, so "half speed until shored" (§3.3) reads without a legend.
  px(ctx, 2, 6, 5, 1, C.unstableCrack);
  px(ctx, 7, 7, 1, 3, C.unstableCrack);
  px(ctx, 8, 10, 6, 1, C.unstableCrack);
  px(ctx, 4, 12, 3, 1, C.unstableCrack);
}

function drawDebris(ctx, heavy) {
  const light = heavy ? C.rubble : C.debris;
  const dark = heavy ? C.rubbleDark : C.debrisDark;
  px(ctx, 1, 8, 6, 5, dark);
  px(ctx, 2, 6, 4, 3, light);
  px(ctx, 8, 9, 6, 4, dark);
  px(ctx, 9, 7, 4, 3, light);
  px(ctx, 5, 11, 6, 3, heavy ? light : dark);
  if (heavy) {
    // Taller pile, so 6-tick rubble is visibly worse than 3-tick debris.
    px(ctx, 4, 3, 4, 3, light);
    px(ctx, 10, 4, 3, 3, dark);
  }
}

function drawFire(ctx, frame) {
  // Wide at the base, tapering to a tip, and asymmetric per frame. The first
  // version was narrow at the bottom with a symmetric column above it, which
  // read as a small orange *person* — beside amber victim sprites, on a map
  // whose whole job is telling a viewer where the people are. A hazard must
  // never be mistakable for a casualty.
  const lean = [0, -1, 1][frame];
  px(ctx, 1, 12, 14, 4, C.ember);                  // burning ground
  px(ctx, 2, 10, 12, 3, C.fire[1]);
  px(ctx, 3 + lean, 7, 9, 4, C.fire[0]);
  px(ctx, 5 + lean, 5, 5, 3, C.fire[2]);
  px(ctx, 6 + lean, 3, 3, 2, C.fire[2]);
  px(ctx, 7 + lean, 1 + (frame === 1 ? 0 : 1), 2, 2, C.fireCore);  // tip
  px(ctx, 6, 11, 3, 2, C.fireCore);                // hot core at the base
}

function drawScout(ctx, frame) {
  const bob = frame === 0 ? 0 : 1;
  // Soft shadow: what sells "hovering" more than the body does (§3.6).
  ctx.fillStyle = C.shadow;
  ctx.beginPath();
  ctx.ellipse(cell / 2, cell * 0.82, cell * 0.24, cell * 0.09, 0, 0, Math.PI * 2);
  ctx.fill();
  px(ctx, 3, 5 + bob, 10, 2, C.scoutDark);     // rotor bar
  px(ctx, 1, 5 + bob, 3, 1, C.metal);
  px(ctx, 12, 5 + bob, 3, 1, C.metal);
  px(ctx, 5, 7 + bob, 6, 4, C.scout);          // body
  px(ctx, 6, 8 + bob, 4, 2, C.glass);          // sensor eye
  px(ctx, 5, 11 + bob, 6, 1, C.scoutDark);
}

function drawLifter(ctx, frame) {
  px(ctx, 2, 11, 12, 3, C.metalDark);          // tracks
  for (let i = 0; i < 5; i++) {
    px(ctx, 3 + i * 2 + (frame ? 1 : 0), 12, 1, 1, C.metal);
  }
  px(ctx, 3, 5, 10, 6, C.lifter);              // body
  px(ctx, 3, 5, 10, 1, "#f0a468");             // lit top edge
  px(ctx, 4, 7, 3, 2, C.glass);                // cab window
  px(ctx, 11, 3, 2, 4, C.lifterDark);          // lift arm
  px(ctx, 10, 2, 4, 1, C.metal);
}

function drawMedic(ctx, frame) {
  px(ctx, 3, 6, 10, 6, C.medic);               // cart body
  px(ctx, 3, 6, 10, 1, "#f08cb4");
  px(ctx, 5, 3, 6, 3, "#efe7e0");              // supply crate
  px(ctx, 7, 3, 2, 3, "#d94f4f");              // red cross
  px(ctx, 5, 4, 6, 1, "#d94f4f");
  const spin = frame ? 1 : 0;
  px(ctx, 3 + spin, 12, 3, 3, C.metalDark);    // wheels
  px(ctx, 10 - spin, 12, 3, 3, C.metalDark);
}

function drawVictim(ctx, state, frame) {
  const body =
    state === "stabilized" ? C.stabilized : state === "lost" ? C.lost : C.victim;
  const lift = state === "located" && frame ? 1 : 0;
  px(ctx, 6, 4 - lift, 4, 4, C.victimSkin);    // head
  px(ctx, 5, 8 - lift, 6, 5, body);            // torso
  px(ctx, 4, 9 - lift, 1, 3, body);
  px(ctx, 11, 9 - lift, 1, 3, body);
  if (state === "stabilized") px(ctx, 7, 9 - lift, 2, 3, "#ffffff");
}

const PAINTERS = {
  ground0: (c) => drawGround(c, 0),
  ground1: (c) => drawGround(c, 1),
  wall: drawWall,
  door: drawDoor,
  unstable: drawUnstable,
  debris: (c) => drawDebris(c, false),
  rubble_heavy: (c) => drawDebris(c, true),
  fire0: (c) => drawFire(c, 0),
  fire1: (c) => drawFire(c, 1),
  fire2: (c) => drawFire(c, 2),
  scout0: (c) => drawScout(c, 0),
  scout1: (c) => drawScout(c, 1),
  lifter0: (c) => drawLifter(c, 0),
  lifter1: (c) => drawLifter(c, 1),
  medic0: (c) => drawMedic(c, 0),
  medic1: (c) => drawMedic(c, 1),
  victim0: (c) => drawVictim(c, "located", 0),
  victim1: (c) => drawVictim(c, "located", 1),
  victim_stabilized: (c) => drawVictim(c, "stabilized", 0),
  victim_lost: (c) => drawVictim(c, "lost", 0),
};

/** Paint every sprite once. Called at boot, and again if the tile size changes. */
export function buildAtlas(tileSize) {
  cell = tileSize;
  sheet = document.createElement("canvas");
  sheet.width = cell * SPRITES.length;
  sheet.height = cell;
  const ctx = sheet.getContext("2d");
  index.clear();

  SPRITES.forEach((name, i) => {
    index.set(name, i * cell);
    ctx.save();
    ctx.translate(i * cell, 0);
    // Clip so a sprite that overdraws cannot bleed into its neighbour — one
    // stray pixel here shows up on every instance of the sprite beside it.
    ctx.beginPath();
    ctx.rect(0, 0, cell, cell);
    ctx.clip();
    PAINTERS[name](ctx);
    ctx.restore();
  });
  return sheet;
}

/** Blit one sprite. Unknown names draw nothing rather than throwing: a missing
 *  sprite should be a hole in the picture, not a dead render loop. */
export function drawSprite(ctx, name, x, y, { flip = false, alpha = 1 } = {}) {
  const sx = index.get(name);
  if (sx === undefined || !sheet) return;
  ctx.save();
  if (alpha !== 1) ctx.globalAlpha = alpha;
  if (flip) {
    ctx.translate(x + cell, y);
    ctx.scale(-1, 1);
    ctx.drawImage(sheet, sx, 0, cell, cell, 0, 0, cell, cell);
  } else {
    ctx.drawImage(sheet, sx, 0, cell, cell, x, y, cell, cell);
  }
  ctx.restore();
}

/** The tile sprite for a ground/object pair. Object wins — debris covers floor. */
export function tileSprite(ground, object, x, y) {
  if (object && object !== "empty") {
    if (object === "fire") return null;           // animated; drawn per frame
    if (object === "debris") return "debris";
    if (object === "rubble_heavy") return "rubble_heavy";
  }
  if (ground === "wall") return "wall";
  if (ground === "door") return "door";
  if (ground === "unstable") return "unstable";
  // Deterministic variant: the same tile always looks the same, so the floor
  // does not shimmer as the fog reveals it.
  return (x * 7 + y * 13) % 5 === 0 ? "ground1" : "ground0";
}

export const COLOURS = C;
