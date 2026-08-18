/* Colony digital-twin renderer (docs/designs/3d-simulation-view.md).
 *
 * The same authoritative sim as `/`, the same 4 Hz frames off the same socket,
 * presented as an orbitable diorama instead of a top-down tilemap. The server
 * does not know this file exists: everything below is derived from the snapshot
 * payload in colony/sim/world.py:599.
 *
 *   /ws frame ──> fog (Uint8Array, 4 Hz) ──┐
 *                 tiles_changed ───────────┼──> 4 InstancedMesh (ground,
 *                 zones + hash jitter ─────┘     structure, objects, fire)
 *                 robots ──> rigs.js ──> pose, sensor volume, telemetry
 *                 events ──> director.js ──> camera
 *
 * WHY INSTANCED, AND ALLOCATED ONCE:
 * boot() re-fires on EVERY snapshot — `make sim` runs uvicorn with --reload and
 * the ON/OFF toggle restarts the mission (see app.js:63). In Canvas 2D a rebuild
 * is free. Here, new geometries and materials hold GPU handles the GC cannot
 * reclaim, so a rebuild-per-snapshot leaks until the browser drops the context
 * and the canvas goes black — mid-recording, on the beat you retake most. So the
 * pool is allocated once for width*height and only its attributes are rewritten.
 * Geometry is only disposed if the map dimensions actually change.
 *
 * WHY THE FOG IS RECOMPUTED ON TICK, NOT ON FRAME:
 * app.js:138 rebuilds the visible set at 60fps for data that changes at 4 Hz —
 * absorbable when it feeds fillRect, wasteful here where it would re-upload the
 * whole instance colour buffer 60 times a second. Robot positions only move on
 * tick, so the fog moves on tick. The render loop interpolates pose and camera
 * and nothing else.
 */

import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/CSS2DRenderer.js";
import {
  setText,
  updateHud,
  refreshComparison,
  formatEvent,
  initConsole,
  // Everything below already worked here the moment sim3d.html grew the markup
  // with the same ids app.js uses — each of these looks its nodes up by id and
  // no-ops when they are absent, which is why they were "2D-only" for two days
  // without anyone writing a line of 2D-specific code.
  initInterventions,
  initKillRobot,
  initCompare,
  refreshMemoryRail,
  refreshCoordination,
  refreshFleet,
  armedIntervention,
  placeIntervention,
} from "./ui-shared.js";
import {
  makeRig,
  makeTrace,
  pushTrace,
  updateRig,
  updateLabel,
  approachAngle,
  facingYaw,
  ROLE_COLOR,
  TURN_FRACTION,
} from "./rigs.js";
import { Director } from "./director.js";

const TICK_MS = 250; // 4 Hz, matching the server
const TICKER_MAX = 200;

/* Instrument grading. Cool, dark, low chroma; colour reserved for meaning.
 * This is the §3.6 palette's opposite on purpose — the cosy pixel look is what
 * made the 2D view read as a game rather than as a simulation. */
const PALETTE = {
  sky: 0x07090d,
  slab: 0x241f1a,
  slabRim: 0x15120f,
  door: 0x6b5638,
  unstable: 0x44394a,
  debris: 0x585245,
  rubble: 0x6a6254,
  fire: 0xff7a3c,
};

/** A located, unreached person. Red because they need help, and far enough from
 *  PALETTE.fire that a small sphere is never mistaken for a small blaze. */
const VICTIM_RED = 0xff2f45;

/* Zones come straight off the wire (world.py:616) and are what turn a tile grid
 * into a place: a low staging yard, a road, dense mid-rise housing, a tall
 * office block. Nothing else in the client reads this field. */
const ZONE = {
  staging:     { minH: 1.2, maxH: 2.2, wall: 0x7d8899, ground: 0x59616e },
  street:      { minH: 1.8, maxH: 3.0, wall: 0x848e9d, ground: 0x4c535e },
  residential: { minH: 3.0, maxH: 6.5, wall: 0xa08a86, ground: 0x606775 },
  office:      { minH: 6.0, maxH: 13.0, wall: 0x7b8ba6, ground: 0x5a626f },
  courtyard:   { minH: 1.4, maxH: 2.4, wall: 0x7f8a90, ground: 0x5b6a5f },
  _default:    { minH: 2.2, maxH: 3.6, wall: 0x828c9a, ground: 0x575e6a },
};

/* Fog, as light rather than as an alpha rectangle. Unseen tiles are not dark:
 * they are ABSENT, so the city builds itself out of the void as the fleet
 * explores. That is the thesis, and it is the one visual that only exists
 * because the fleet writes to shared memory.
 *
 * The mix is deliberately moderate. An early version used 0.72 and the whole
 * block went near-black, because "remembered" is the *steady state* — coverage
 * hits 100% and almost nothing is under a sensor at any instant. Remembered has
 * to stay legible; it is live that should pop. */
const FOG_UNSEEN = 0;
const FOG_REMEMBERED = 1;
const FOG_LIVE = 2;
const REMEMBERED_MIX = 0.44;          // how far toward the cold tone
const REMEMBERED_MIX_BASELINE = 0.60; // private maps: dimmer still (app.js:37)
const COLD = new THREE.Color(0x0e141c);

// --- state -------------------------------------------------------------------

let world = null;
let width = 0;
let height = 0;
let robots = [];
let victims = [];
let lost = new Set();
let sharedVision = true;
let selected = null;
let windowStart = 0;

let everSeen = new Uint8Array(0);  // monotonic: has any robot ever seen this tile
let fog = new Uint8Array(0);       // 0 unseen / 1 remembered / 2 live, per tick
let zoneOf = new Uint8Array(0);    // index into ZONE_KEYS
let ZONE_KEYS = ["_default"];

const motion = new Map();   // robot id -> {fromX, fromY, toX, toY, yawFrom, yawTo, yaw}
const rigs = new Map();     // robot id -> THREE.Group
const traces = new Map();   // robot id -> THREE.Line
const victimMeshes = new Map();

let instancesDirty = false;

// --- three setup (once) ------------------------------------------------------

const stage = document.getElementById("stage");
const scene = new THREE.Scene();
scene.background = new THREE.Color(PALETTE.sky);
// Starts beyond the camera's 90-unit orbit limit, so the slab never sits in
// haze. This only softens the void behind the island, never the mission.
scene.fog = new THREE.Fog(PALETTE.sky, 95, 210);

// 45° is wide enough to hold the whole 40x30 island in an establishing shot
// without the barrel distortion a wider lens puts on the slab edges.
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 500);
camera.position.set(36, 40, 36);

const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.35;
stage.appendChild(renderer.domElement);

const labelRenderer = new CSS2DRenderer({ element: document.getElementById("labels") });
labelRenderer.domElement.style.position = "absolute";
labelRenderer.domElement.style.top = "0";
labelRenderer.domElement.style.pointerEvents = "none";

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.07;
controls.minDistance = 6;
controls.maxDistance = 90;
// Never let the camera go under the slab: a diorama viewed from below reads as
// a bug, not as a choice.
controls.maxPolarAngle = Math.PI * 0.49;
controls.target.set(0, 0, 0);

const director = new Director(camera, controls);
controls.addEventListener("start", () => director.notifyUserInput(performance.now()));

// Sun, sky bounce, and a cold rim. Lighting does most of the work of making
// extruded boxes read as buildings.
const sun = new THREE.DirectionalLight(0xffeedd, 2.1);
sun.position.set(26, 38, 18);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
sun.shadow.camera.near = 1;
sun.shadow.camera.far = 120;
sun.shadow.camera.left = -34;
sun.shadow.camera.right = 34;
sun.shadow.camera.top = 34;
sun.shadow.camera.bottom = -34;
sun.shadow.bias = -0.0008;
scene.add(sun);
scene.add(new THREE.HemisphereLight(0x9ab4d4, 0x1a1712, 0.75));
const rim = new THREE.DirectionalLight(0x5f7fa8, 0.5);
rim.position.set(-22, 14, -20);
scene.add(rim);

const worldRoot = new THREE.Group();
scene.add(worldRoot);

let ground = null;
let structure = null;
let objects = null;
let fires = null;
let slab = null;

const dummy = new THREE.Object3D();
const colorScratch = new THREE.Color();

// --- deterministic jitter ----------------------------------------------------

/* Building heights must be varied (or the block reads as one extruded slab)
 * AND identical on every reload (or a re-recorded take does not match the one
 * before it). So: hashed, never random. */
function hash2(x, y) {
  let h = (x * 374761393 + y * 668265263) | 0;
  h = (h ^ (h >>> 13)) * 1274126177;
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}

// --- world construction ------------------------------------------------------

/** Detach a subtree and hand its GPU resources back. */
function disposeTree(root) {
  root.traverse((node) => {
    if (node.geometry) node.geometry.dispose();
    const material = node.material;
    if (!material) return;
    for (const m of Array.isArray(material) ? material : [material]) m.dispose();
  });
  root.removeFromParent();
}

function zoneAt(index) {
  return ZONE[ZONE_KEYS[zoneOf[index]]] || ZONE._default;
}

function buildZoneIndex() {
  ZONE_KEYS = ["_default"];
  zoneOf = new Uint8Array(width * height);
  for (const zone of world.zones || []) {
    // Guard the shape rather than trusting it: this is the one field on the
    // wire with no other consumer, so a rename would land here first.
    if (
      typeof zone?.x !== "number" || typeof zone?.y !== "number" ||
      typeof zone?.width !== "number" || typeof zone?.height !== "number"
    ) continue;
    const key = ZONE[zone.name] ? zone.name : "_default";
    let slot = ZONE_KEYS.indexOf(key);
    if (slot < 0) slot = ZONE_KEYS.push(key) - 1;
    for (let y = zone.y; y < zone.y + zone.height; y++) {
      for (let x = zone.x; x < zone.x + zone.width; x++) {
        if (x < 0 || y < 0 || x >= width || y >= height) continue;
        zoneOf[y * width + x] = slot;
      }
    }
  }
}

function allocate() {
  for (const mesh of [ground, structure, objects, fires]) {
    if (!mesh) continue;
    worldRoot.remove(mesh);
    mesh.geometry.dispose();
    mesh.material.dispose();
  }
  if (slab) {
    worldRoot.remove(slab);
    slab.traverse((n) => { if (n.geometry) n.geometry.dispose(); if (n.material) n.material.dispose(); });
  }

  const count = width * height;
  const unit = new THREE.BoxGeometry(1, 1, 1);

  ground = new THREE.InstancedMesh(unit, new THREE.MeshStandardMaterial({ roughness: 0.92, metalness: 0.04 }), count);
  structure = new THREE.InstancedMesh(unit.clone(), new THREE.MeshStandardMaterial({ roughness: 0.78, metalness: 0.12 }), count);
  objects = new THREE.InstancedMesh(unit.clone(), new THREE.MeshStandardMaterial({ roughness: 0.95, metalness: 0.02 }), count);
  fires = new THREE.InstancedMesh(unit.clone(), new THREE.MeshStandardMaterial({
    roughness: 0.4, emissive: new THREE.Color(PALETTE.fire), emissiveIntensity: 2.4,
  }), count);

  for (const mesh of [ground, structure, objects, fires]) {
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.frustumCulled = false;
    worldRoot.add(mesh);
  }

  // The floating island: an earth slab with a darker rim under it.
  slab = new THREE.Group();
  const top = new THREE.Mesh(
    new THREE.BoxGeometry(width + 1.4, 1.6, height + 1.4),
    new THREE.MeshStandardMaterial({ color: PALETTE.slab, roughness: 1, metalness: 0 }),
  );
  top.position.y = -0.82;
  top.receiveShadow = true;
  const rimBox = new THREE.Mesh(
    new THREE.BoxGeometry(width + 0.4, 2.6, height + 0.4),
    new THREE.MeshStandardMaterial({ color: PALETTE.slabRim, roughness: 1, metalness: 0 }),
  );
  rimBox.position.y = -2.7;
  slab.add(top, rimBox);
  worldRoot.add(slab);

  everSeen = new Uint8Array(count);
  fog = new Uint8Array(count);
}

/* NO DECORATIVE CITY, DELIBERATELY — the note is here because it is the first
 * thing anyone will want to add.
 *
 * The map is 1,009 open tiles against 178 wall tiles, and those walls are
 * almost entirely the boundary rectangle plus the office building's interior
 * rooms (world/maps/aftershock.json). There is no dense city in the data.
 *
 * Massing drawn on the playable surface to fill that space would be the
 * renderer lying about the simulation: a viewer reads a building as terrain the
 * fleet must route around, and here a robot would walk straight through it. A
 * surrounding skyline was tried and removed for a separate reason — the camera
 * orbits between 6 and 90 units, so any ring close enough to read ends up with
 * the camera inside it, occluding the mission.
 *
 * The scene this map actually describes is a flattened block with one
 * part-standing office building, and that is what gets drawn. Density comes
 * from light, debris and the robots, not from invented geometry. */

/** Write one tile's geometry and colour into the instance pool. */
function writeTile(x, y) {
  const index = y * width + x;
  const seen = fog[index];
  const zone = zoneAt(index);
  const groundType = world.ground[y][x];
  const objectType = world.objects[y][x];

  const wx = x - width / 2 + 0.5;
  const wz = y - height / 2 + 0.5;

  if (seen === FOG_UNSEEN) {
    // Absent, not dark. Scale 0 removes it from the silhouette entirely, which
    // is what makes the city visibly assemble itself as the fleet explores.
    dummy.position.set(wx, 0, wz);
    dummy.scale.set(0, 0, 0);
    dummy.updateMatrix();
    for (const mesh of [ground, structure, objects, fires]) mesh.setMatrixAt(index, dummy.matrix);
    return;
  }

  const mix = sharedVision ? REMEMBERED_MIX : REMEMBERED_MIX_BASELINE;
  const tint = (hex) => {
    colorScratch.setHex(hex);
    if (seen === FOG_REMEMBERED) colorScratch.lerp(COLD, mix);
    // Under a sensor right now: lifted, so "observed" reads as illuminated
    // rather than merely less dim. This is the contrast the legend depends on.
    else colorScratch.multiplyScalar(1.16);
    return colorScratch;
  };

  const isWall = groundType === "wall";

  // Ground plate: every tile that is not a wall gets a floor.
  dummy.position.set(wx, isWall ? -5 : 0.05, wz);
  dummy.rotation.set(0, 0, 0);
  dummy.scale.set(isWall ? 0 : 1, isWall ? 0 : 0.1, isWall ? 0 : 1);
  dummy.updateMatrix();
  ground.setMatrixAt(index, dummy.matrix);
  if (!isWall) {
    const base =
      groundType === "unstable" ? PALETTE.unstable
      : groundType === "door" ? PALETTE.door
      : zone.ground;
    tint(base);
    // 1,009 of this map's 1,200 tiles are open ground. Without per-tile
    // variation the block reads as one flat sheet of colour and the whole
    // diorama falls apart at wide-shot distance.
    colorScratch.multiplyScalar(0.92 + hash2(x + 61, y + 47) * 0.17);
    ground.setColorAt(index, colorScratch);
  }

  // Structure. Two rules matter here, and both were learned the hard way:
  //
  //  1. THE MAP BORDER IS NOT A BUILDING. Every tile on the outer ring is a
  //     wall in the data, so extruding it to zone height builds an unbroken
  //     6-13 unit rampart around the block that hides the entire mission from
  //     every camera angle. It is the edge of the site, so it renders as a low
  //     retaining wall.
  //
  //  2. HEIGHT IS PER BUILDING, NOT PER TILE. Hashing each tile independently
  //     turns the office's interior room walls into a forest of mismatched
  //     pillars. Hashing a coarse 6x6 block instead means neighbouring wall
  //     tiles share a storey height and a contiguous run reads as one flat-
  //     topped mass — which is what makes it look like architecture.
  if (isWall) {
    const isBorder = x === 0 || y === 0 || x === width - 1 || y === height - 1;
    let h;
    if (isBorder) {
      h = 0.7;
    } else {
      const block = hash2(Math.floor(x / 6), Math.floor(y / 6));
      const storey = zone.minH + block * (zone.maxH - zone.minH);
      h = storey * (0.94 + hash2(x, y) * 0.12);   // slight per-tile relief only
    }
    dummy.position.set(wx, h / 2, wz);
    dummy.scale.set(0.98, h, 0.98);
    dummy.updateMatrix();
    structure.setMatrixAt(index, dummy.matrix);
    colorScratch.setHex(isBorder ? 0x4a4640 : zone.wall);
    colorScratch.multiplyScalar(0.88 + hash2(x + 91, y + 17) * 0.24);
    if (seen === FOG_REMEMBERED) colorScratch.lerp(COLD, mix);
    else colorScratch.multiplyScalar(1.16);
    structure.setColorAt(index, colorScratch);
  } else {
    dummy.position.set(wx, -5, wz);
    dummy.scale.set(0, 0, 0);
    dummy.updateMatrix();
    structure.setMatrixAt(index, dummy.matrix);
  }

  // Objects: debris and heavy rubble, as scattered low mass.
  const isDebris = objectType === "debris" || objectType === "rubble_heavy";
  if (isDebris) {
    const heavy = objectType === "rubble_heavy";
    const h = heavy ? 0.62 : 0.34;
    const jx = (hash2(x + 5, y + 3) - 0.5) * 0.22;
    const jz = (hash2(x + 11, y + 7) - 0.5) * 0.22;
    dummy.position.set(wx + jx, h / 2 + 0.08, wz + jz);
    dummy.rotation.set(0, hash2(x + 31, y + 13) * Math.PI, 0);
    dummy.scale.set(heavy ? 0.86 : 0.72, h, heavy ? 0.86 : 0.72);
    dummy.updateMatrix();
    objects.setMatrixAt(index, dummy.matrix);
    objects.setColorAt(index, tint(heavy ? PALETTE.rubble : PALETTE.debris));
  } else {
    dummy.position.set(wx, -5, wz);
    dummy.rotation.set(0, 0, 0);
    dummy.scale.set(0, 0, 0);
    dummy.updateMatrix();
    objects.setMatrixAt(index, dummy.matrix);
  }

  // Fire: emissive, and the one thing that stays bright when remembered,
  // because a fire nobody is watching is still burning.
  if (objectType === "fire") {
    dummy.position.set(wx, 0.3, wz);
    dummy.rotation.set(0, hash2(x + 3, y + 29) * Math.PI, 0);
    dummy.scale.set(0.8, 0.6, 0.8);
    dummy.updateMatrix();
    fires.setMatrixAt(index, dummy.matrix);
    colorScratch.setHex(PALETTE.fire);
    if (seen === FOG_REMEMBERED) colorScratch.lerp(COLD, mix * 0.45);
    fires.setColorAt(index, colorScratch);
  } else {
    dummy.position.set(wx, -5, wz);
    dummy.rotation.set(0, 0, 0);
    dummy.scale.set(0, 0, 0);
    dummy.updateMatrix();
    fires.setMatrixAt(index, dummy.matrix);
  }
}

function writeAllTiles() {
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) writeTile(x, y);
  }
  instancesDirty = true;
}

// --- fog, recomputed once per tick (not per frame) ---------------------------

function recomputeFog() {
  try {
    for (let i = 0; i < fog.length; i++) fog[i] = everSeen[i] ? FOG_REMEMBERED : FOG_UNSEEN;
    for (const robot of robots) {
      if (lost.has(robot.id)) continue;
      const radius = robot.vision || 2;
      // A SQUARE, matching world.py:535-538 and app.js:138. See the note at the
      // top of rigs.js before changing this to anything cone-shaped.
      const y0 = Math.max(0, robot.y - radius);
      const y1 = Math.min(height - 1, robot.y + radius);
      const x0 = Math.max(0, robot.x - radius);
      const x1 = Math.min(width - 1, robot.x + radius);
      for (let y = y0; y <= y1; y++) {
        for (let x = x0; x <= x1; x++) {
          const i = y * width + x;
          if (everSeen[i]) fog[i] = FOG_LIVE;
        }
      }
    }
    writeAllTiles();
  } catch (err) {
    // A fog fault must not take the render loop with it: the frame would
    // freeze on the last good state and look like the server died.
    console.error("fog update failed", err);
    document.getElementById("status").textContent = `fog error: ${err.message}`;
  }
}

// --- frame handlers ----------------------------------------------------------

function boot(snapshot) {
  const incoming = snapshot.world;
  const dimensionsChanged = !world || incoming.width !== width || incoming.height !== height;

  world = incoming;
  width = world.width;
  height = world.height;
  sharedVision = world.shared_vision !== false;

  if (dimensionsChanged) {
    allocate();
  } else {
    // Same map: keep every geometry and material, just clear what we know.
    everSeen.fill(0);
    fog.fill(0);
  }
  buildZoneIndex();
  buildSectorGrid();   // cheap, and the map can change between missions

  // Rigs, traces and victim markers are rebuilt per mission, so unlike the
  // tile pool they genuinely are discarded — and removing them from the graph
  // is not enough. Their geometries and materials hold GPU handles the GC
  // cannot reach, so an undisposed rig leaks a little on every ON/OFF toggle,
  // which is the beat that gets retaken most during recording.
  motion.clear();
  for (const [, rig] of rigs) disposeTree(rig);
  rigs.clear();
  for (const [, trace] of traces) disposeTree(trace);
  traces.clear();
  for (const [, mesh] of victimMeshes) disposeTree(mesh);
  victimMeshes.clear();

  robots = snapshot.robots || [];
  victims = snapshot.victims || [];
  lost = new Set(snapshot.lost || []);
  if (selected) closePanel();

  applyExplored(snapshot.explored);
  trackRobots(robots);
  syncVictims();
  recomputeFog();

  const badge = document.getElementById("mode-badge");
  badge.textContent = sharedVision ? "SHARED MEMORY" : "PRIVATE MAPS";
  badge.className = sharedVision ? "on" : "off";
  document.getElementById("toggle").textContent =
    sharedVision ? "coordination: ON" : "coordination: OFF";
  setText(
    "scenario",
    `${world.name} · ${victims.length} trapped · ${robots.length} units · ` +
      `${world.mission_length_ticks} ticks · seed ${world.seed ?? "?"}`,
  );

  const centre = new THREE.Vector3(0, 0, 0);
  controls.target.copy(centre);
  director.target.copy(centre);
  director.desired.copy(centre);
}

function applyTileChanges(changes) {
  for (const change of changes || []) {
    const { x, y } = change;
    // Bounds guard: an out-of-range index would write into another tile's
    // instance and leave a silently wrong city.
    if (!Number.isInteger(x) || !Number.isInteger(y)) continue;
    if (x < 0 || y < 0 || x >= width || y >= height) continue;
    world.ground[y][x] = change.ground;
    world.objects[y][x] = change.object;
    writeTile(x, y);
    instancesDirty = true;
  }
}

function applyExplored(tiles) {
  for (const tile of tiles || []) {
    const x = tile[0];
    const y = tile[1];
    if (x < 0 || y < 0 || x >= width || y >= height) continue;
    everSeen[y * width + x] = 1;
  }
}

function trackRobots(incoming) {
  for (const robot of incoming) {
    let m = motion.get(robot.id);
    const yawTo = facingYaw(robot.facing);
    if (!m) {
      m = { fromX: robot.x, fromY: robot.y, toX: robot.x, toY: robot.y, yaw: yawTo, yawFrom: yawTo, yawTo };
      motion.set(robot.id, m);
    } else {
      m.fromX = m.toX;
      m.fromY = m.toY;
      m.toX = robot.x;
      m.toY = robot.y;
      m.yawFrom = m.yaw;
      m.yawTo = yawTo;
    }
    if (!rigs.has(robot.id)) {
      const rig = makeRig(robot);
      worldRoot.add(rig);
      rigs.set(robot.id, rig);
      const trace = makeTrace(robot.role);
      worldRoot.add(trace);
      traces.set(robot.id, trace);
    }
  }
  robots = incoming;
}

function syncVictims() {
  for (const victim of victims) {
    // Unknown victims stay hidden: the viewer learns where they are when the
    // fleet does, which is the point of the demo.
    const index = victim.y * width + victim.x;
    const visible = victim.state !== "unknown" && everSeen[index];
    let mesh = victimMeshes.get(victim.id);
    if (!mesh) {
      mesh = new THREE.Mesh(
        new THREE.SphereGeometry(0.2, 12, 10),
        new THREE.MeshStandardMaterial({ emissiveIntensity: 1.7, roughness: 0.4 }),
      );
      mesh.castShadow = true;
      worldRoot.add(mesh);
      victimMeshes.set(victim.id, mesh);
    }
    mesh.visible = !!visible;
    if (!visible) continue;
    // Red for `located`, not amber. A person found and not yet reached is the
    // most urgent thing on the map, and amber reads as a warning where red
    // reads as a casualty — the distinction a viewer has to make in the two
    // seconds the marker is on screen. Green once stabilized, grey once lost.
    // Kept clear of the fire orange (PALETTE.fire) so the two never blur.
    const colour =
      victim.state === "stabilized" ? 0x5fc98a
      : victim.state === "lost" ? 0x5a5560
      : VICTIM_RED;
    mesh.material.color.setHex(colour);
    mesh.material.emissive.setHex(colour);
    // Emissive above ~1.3 clips to white under ACES at this exposure, which
    // cost the stabilized markers their green — and green-versus-red is the
    // only distinction that matters here. Kept under the knee.
    mesh.material.emissiveIntensity =
      victim.state === "lost" ? 0.15 : victim.state === "stabilized" ? 0.7 : 1.2;
    mesh.userData.urgent = victim.state === "located";
    mesh.position.set(victim.x - width / 2 + 0.5, 0.42, victim.y - height / 2 + 0.5);
  }
}

function logEvents(events) {
  const ticker = document.getElementById("ticker");
  for (const event of events || []) {
    const formatted = formatEvent(event);
    if (!formatted) continue;
    const line = document.createElement("div");
    line.className = formatted.className;
    line.textContent = formatted.text;
    ticker.appendChild(line);
  }
  while (ticker.childElementCount > TICKER_MAX) ticker.removeChild(ticker.firstChild);
  ticker.scrollTop = ticker.scrollHeight;
}

/** Where on the slab an event happened, for the camera. Null = no shot. */
function locateEvent(event) {
  const detail = event.detail || {};
  if (Number.isInteger(detail.x) && Number.isInteger(detail.y)) {
    return { x: detail.x - width / 2 + 0.5, z: detail.y - height / 2 + 0.5 };
  }
  const actor = robots.find((r) => r.id === event.actor);
  if (actor) return { x: actor.x - width / 2 + 0.5, z: actor.y - height / 2 + 0.5 };
  if (event.verb === "aftershock") return { x: 0, z: 0 };
  return null;
}

/** Centroid of the live fleet, so an idle wide still contains the robots. */
const homeScratch = new THREE.Vector3();
function fleetCentre() {
  let n = 0;
  homeScratch.set(0, 0, 0);
  for (const robot of robots) {
    homeScratch.x += robot.x - width / 2 + 0.5;
    homeScratch.z += robot.y - height / 2 + 0.5;
    n++;
  }
  if (n) homeScratch.divideScalar(n);
  homeScratch.y = 0;
  return homeScratch;
}

/* --- the sector grid (press S) ----------------------------------------------
 *
 * `world.sectors` has been on the wire since the first snapshot and only the 2D
 * renderer ever read it. Without it the ticker says "s2 swept sector B2" and
 * there is nowhere on screen that B2 is — the label names a place the view does
 * not have. Drawn flat on the slab rather than as a box: it is an annotation
 * over the city, not another thing in it.
 */
let sectorGroup = null;
let sectorsVisible = false;

function buildSectorGrid() {
  if (sectorGroup) {
    scene.remove(sectorGroup);
    sectorGroup.traverse((o) => o.geometry?.dispose());
    sectorGroup = null;
  }
  if (!world?.sectors?.length) return;

  sectorGroup = new THREE.Group();
  sectorGroup.visible = sectorsVisible;
  const material = new THREE.LineBasicMaterial({
    color: 0x63c5da, transparent: true, opacity: 0.42,
  });

  for (const s of world.sectors) {
    // Corners in world space. The +0.5 that centres a tile is deliberately
    // absent: a sector boundary runs *between* tiles, not through their middles.
    const x0 = s.x - world.width / 2;
    const z0 = s.y - world.height / 2;
    const x1 = x0 + s.width;
    const z1 = z0 + s.height;
    const y = 0.12;
    const outline = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(x0, y, z0), new THREE.Vector3(x1, y, z0),
      new THREE.Vector3(x1, y, z1), new THREE.Vector3(x0, y, z1),
      new THREE.Vector3(x0, y, z0),
    ]);
    sectorGroup.add(new THREE.Line(outline, material));

    const tag = document.createElement("div");
    tag.className = "sector-tag";
    tag.textContent = s.id;
    const label = new CSS2DObject(tag);
    label.position.set((x0 + x1) / 2, y, (z0 + z1) / 2);
    sectorGroup.add(label);
  }
  scene.add(sectorGroup);
}

function setSectorGrid(on) {
  sectorsVisible = on;
  if (sectorGroup) sectorGroup.visible = on;
}

// --- picking (FR-17) ---------------------------------------------------------

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let dragged = false;

/* A drag is movement past a threshold, not any movement at all.
 *
 * This was `if (e.buttons) dragged = true`, which meant a single pixel of
 * travel between pressing and releasing counted as an orbit and silently
 * swallowed the click. Mice move during clicks — trackpads especially — so
 * "click a robot for its reasoning" failed intermittently in a way that looked
 * like the robot was not clickable rather than like the click was discarded.
 * Measured from the press point rather than accumulated, so a slow drift out
 * and back is still a drag. */
const DRAG_SLOP_PX = 5;
let pressX = 0;
let pressY = 0;

renderer.domElement.addEventListener("pointerdown", (e) => {
  dragged = false;
  pressX = e.clientX;
  pressY = e.clientY;
});
renderer.domElement.addEventListener("pointermove", (e) => {
  if (!e.buttons) return;
  if (Math.hypot(e.clientX - pressX, e.clientY - pressY) > DRAG_SLOP_PX) dragged = true;
});
/* Tile picking, for the operator gesture.
 *
 * A mathematical plane at y=0 rather than a raycast against the `ground`
 * InstancedMesh, and the difference matters: unseen tiles and walls are written
 * at scale 0 (see `placeTile`), so an instanced raycast silently cannot hit
 * unexplored ground — which is exactly where an operator wants to drop a
 * collapse. The plane has no such holes. `controls.maxPolarAngle` keeps the
 * camera above the slab, so it is always facing us and the intersection always
 * exists.
 */
const groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
const groundHit = new THREE.Vector3();

/** Screen point -> integer tile, or null if the ray misses the slab entirely.
 *
 * The inverse of `placeTile`'s `wx = x - width/2 + 0.5`.
 */
function tileAt(event) {
  if (!world) return null;
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  if (!raycaster.ray.intersectPlane(groundPlane, groundHit)) return null;
  const x = Math.floor(groundHit.x + world.width / 2);
  const y = Math.floor(groundHit.z + world.height / 2);
  if (x < 0 || y < 0 || x >= world.width || y >= world.height) return null;
  return { x, y };
}

/* The aiming reticle: what the disruption will actually cover.
 *
 * Radius is in tiles and the affected area is a square box (see the server's
 * intervention handling), so this is a box outline rather than a ring — a ring
 * would promise a circle and then break more than it drew.
 */
const reticle = new THREE.LineSegments(
  new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 0.35, 1)),
  new THREE.LineBasicMaterial({ color: 0xd9884a, transparent: true, opacity: 0.9 }),
);
reticle.visible = false;
reticle.renderOrder = 999;
scene.add(reticle);

function updateReticle(event) {
  const armed = armedIntervention();
  document.body.classList.toggle("arming", !!armed);
  if (!armed) {
    reticle.visible = false;
    return;
  }
  const tile = tileAt(event);
  if (!tile) {
    reticle.visible = false;
    return;
  }
  const span = armed.radius * 2 + 1;
  reticle.scale.set(span, 1, span);
  reticle.position.set(
    tile.x - world.width / 2 + 0.5,
    0.18,
    tile.y - world.height / 2 + 0.5,
  );
  reticle.visible = true;
}

renderer.domElement.addEventListener("pointermove", updateReticle);
renderer.domElement.addEventListener("pointerleave", () => { reticle.visible = false; });

renderer.domElement.addEventListener("pointerup", (event) => {
  if (dragged) return;   // an orbit drag is not a click

  // An armed disruption takes the click, checked BEFORE robot picking for the
  // reason app.js gives: the tile an operator wants to collapse is very often
  // the one a robot is standing beside, and "select the robot instead" would
  // make the corridor next to it unclickable.
  if (armedIntervention()) {
    const tile = tileAt(event);
    if (tile) {
      placeIntervention(tile.x, tile.y);
      reticle.visible = false;
      document.body.classList.remove("arming");
    }
    return;
  }

  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);

  const hits = raycaster.intersectObjects([...rigs.values()], true);
  for (const hit of hits) {
    const id = hit.object?.userData?.robotId;
    if (!id) continue;                       // guard: geometry with no owner
    const robot = robots.find((r) => r.id === id);
    if (robot) {
      showProvenance(robot);
      return;
    }
  }
  closePanel();   // clicking empty space closes, never throws
});

/** FR-17 made visible: the rationale *and the memories behind it*. */
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
        // Rows in `observations`, joined server-side — not anything the UI invented.
        sources.textContent =
          `based on ${plan.based_on.length} ${plan.based_on.length === 1 ? "memory" : "memories"}: ` +
          plan.based_on.map((b) => `${b.kind} (${b.x},${b.y}) ×${b.sightings}`).join(", ");
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

// --- transport ---------------------------------------------------------------
// Copied from app.js rather than shared: it calls boot() and applyTileChanges(),
// which mean different things in each renderer. Consolidating it needs a
// handler-injection refactor, which is TODOS.md item 1.

let socket = null;

function connect() {
  const status = document.getElementById("status");
  if (socket) {
    socket.onclose = null;
    socket.onmessage = null;
    socket.close();
  }
  // Match the page's scheme. A hardcoded ws:// is blocked as mixed content
  // the moment the demo sits behind TLS, and the world just never ticks.
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws`);
  const mine = socket;

  socket.onopen = () => { status.textContent = "live"; };
  socket.onerror = () => { status.textContent = "connection error"; };
  socket.onclose = () => {
    if (mine !== socket) return;
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
        // Kicked on snapshot as well as on their intervals (app.js:536-537), so
        // a restarted mission does not show the previous run's row counts and
        // decisions for up to two seconds.
        refreshMemoryRail();
        refreshCoordination();
      }
      if (!world) return;
      applyTileChanges(frame.tiles_changed);
      applyExplored(frame.explored);
      trackRobots(frame.robots || []);
      lost = new Set(frame.lost || []);
      victims = frame.victims || victims;
      windowStart = performance.now();
      syncVictims();
      recomputeFog();          // 4 Hz, with the tick — not in the render loop
      updateHud(frame.metrics);
      logEvents(frame.events);
      director.onEvents(frame.events, locateEvent, performance.now());
    } catch (err) {
      status.textContent = `render error: ${err.message}`;
      console.error(err);
    }
  };
}

// --- render loop -------------------------------------------------------------

function resize() {
  const w = stage.clientWidth || 1;
  const h = stage.clientHeight || 1;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
  labelRenderer.setSize(w, h);
}
addEventListener("resize", resize);
resize();

function render() {
  requestAnimationFrame(render);
  const now = performance.now();
  const t = Math.min(1, (now - windowStart) / TICK_MS);

  // Turn, THEN translate. A machine rotates before it drives; a game sprite
  // slides sideways. This split is most of what sells "robot" over "token".
  const turnT = Math.min(1, t / TURN_FRACTION);
  const moveRaw = t <= TURN_FRACTION ? 0 : (t - TURN_FRACTION) / (1 - TURN_FRACTION);
  const moveT = moveRaw * moveRaw * (3 - 2 * moveRaw); // smoothstep

  let slot = 0;
  for (const robot of robots) {
    const rig = rigs.get(robot.id);
    const m = motion.get(robot.id);
    if (!rig || !m) continue;
    const mySlot = slot++;

    const gx = m.fromX + (m.toX - m.fromX) * moveT;
    const gy = m.fromY + (m.toY - m.fromY) * moveT;
    m.yaw = approachAngle(m.yawFrom, m.yawTo, turnT);

    const pose = { x: gx - width / 2 + 0.5, z: gy - height / 2 + 0.5, yaw: m.yaw, gx, gy };
    const isLost = lost.has(robot.id);
    updateRig(rig, pose, {
      robot,
      lost: isLost,
      acting: robot.status === "acting" || robot.status === "clearing" || robot.status === "stabilizing",
      moving: robot.status === "moving",
      now,
    });
    // Full telemetry for the robot under inspection, a compact chip for the
    // rest — otherwise four blocks cover the block they are describing.
    updateLabel(rig, pose, robot, isLost, selected === robot.id, mySlot);
    if (!isLost) pushTrace(traces.get(robot.id), pose.x, pose.z);
  }

  // A located person pulses. §3.6 asked for it in 2D and it earns its place
  // here too: a static dot is a map pin, a pulsing one is a countdown — which
  // is what a vitals deadline actually is.
  const pulse = 1.15 + Math.sin(now / 260) * 0.55;
  for (const mesh of victimMeshes.values()) {
    if (mesh.visible && mesh.userData.urgent) mesh.material.emissiveIntensity = pulse;
  }

  if (instancesDirty) {
    for (const mesh of [ground, structure, objects, fires]) {
      if (!mesh) continue;
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    }
    instancesDirty = false;
  }

  director.update(now, fleetCentre());
  controls.update();
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
}

// --- wiring ------------------------------------------------------------------

initConsole({
  getRobots: () => robots,
  getSelected: () => selected,
});

initInterventions();
initKillRobot();
initCompare();

setInterval(refreshComparison, 4000);
// Same cadences as app.js:571-575. The coordination feed is the slower of the
// two because a new cross-agent line is a decision boundary, not a tick; the
// fleet panel is the faster one because its lease countdown has to visibly run
// down or the takeover looks like a jump cut.
setInterval(refreshCoordination, 2000);
setInterval(refreshFleet, 1000);
// On an interval, not only on snapshot as app.js:536 does. Those row counts are
// the "CockroachDB is doing the work" signal, and a snapshot only arrives on
// connect or restart — so on the 2D page the rail freezes at whatever the
// counts were when you opened the tab, which is the opposite of the claim.
setInterval(refreshMemoryRail, 3000);

document.getElementById("toggle").addEventListener("click", toggleMode);
document.getElementById("panel-close").addEventListener("click", closePanel);

const directorButton = document.getElementById("director-toggle");
directorButton.addEventListener("click", () => {
  director.setEnabled(!director.enabled);
  directorButton.textContent = `director: ${director.enabled ? "ON" : "OFF"}`;
  // `.on`, not `.armed`: an engaged mode and a loaded gesture are different
  // states and sim3d.html now styles them differently.
  directorButton.classList.toggle("on", director.enabled);
});

addEventListener("keydown", (e) => {
  if (e.key === "Escape") closePanel();
  // Guarded on the focused element. The 2D page has this bound bare, so typing
  // "s" into the commander console's ask box toggles its sector grid; that bug
  // is not worth porting.
  if (e.key === "s" || e.key === "S") {
    const focused = document.activeElement;
    if (focused && (focused.tagName === "INPUT" || focused.tagName === "TEXTAREA")) return;
    setSectorGrid(!sectorsVisible);
  }
});

/* Read-only QA surface.
 *
 * The headless smoke check (T8) has no way to assert "the scene actually has a
 * city in it" from the outside — a WebGL canvas is opaque to the DOM, and
 * readPixels on a GPU canvas is both slow and driver-dependent. So the renderer
 * reports its own state, and the smoke test asserts on that plus a screenshot.
 * It also lets a script click a specific robot without guessing pixels.
 *
 * Deliberately gets nothing but getters: it must never become a way to drive
 * the renderer, or it stops being a test of the real thing. */
window.__colony = {
  stats() {
    let unseen = 0, remembered = 0, live = 0;
    for (let i = 0; i < fog.length; i++) {
      if (fog[i] === FOG_LIVE) live++;
      else if (fog[i] === FOG_REMEMBERED) remembered++;
      else unseen++;
    }
    // Counted from the grid the renderer is actually drawing from, so a
    // divergence here means applyTileChanges stopped writing (T2).
    const tally = { debris: 0, rubble_heavy: 0, fire: 0, wall: 0, door: 0 };
    if (world) {
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          const o = world.objects[y][x];
          if (o in tally) tally[o]++;
          const g = world.ground[y][x];
          if (g in tally) tally[g]++;
        }
      }
    }
    return {
      ready: !!world,
      tiles: fog.length,
      grid: tally,
      fog: { unseen, remembered, live },
      robots: robots.length,
      rigs: rigs.size,
      victimsVisible: [...victimMeshes.values()].filter((m) => m.visible).length,
      selected,
      directorEnabled: director.enabled,
      cameraDistance: +camera.position.distanceTo(controls.target).toFixed(1),
      drawCalls: renderer.info.render.calls,
      triangles: renderer.info.render.triangles,
      geometries: renderer.info.memory.geometries,
      textures: renderer.info.memory.textures,
    };
  },
  /** The operator gesture's state, for asserting it rather than eyeballing it.
   *
   * The reticle is the only part of this view whose correctness is invisible in
   * a screenshot when it is wrong — an absent box and a box drawn under the
   * slab look identical from above. `tile` is what the next click would hit. */
  aiming() {
    const armed = armedIntervention();
    return {
      armed: armed ? armed.kind : null,
      radius: armed ? armed.radius : null,
      reticleVisible: reticle.visible,
      tile: reticle.visible
        ? {
            x: Math.round(reticle.position.x + width / 2 - 0.5),
            y: Math.round(reticle.position.z + height / 2 - 0.5),
          }
        : null,
      sectorsVisible,
    };
  },
  /** Viewport pixel coordinates of a tile centre — the inverse of tileAt(),
   *  so a scripted click can target a specific tile rather than a pixel. */
  screenPositionOfTile(x, y) {
    const v = new THREE.Vector3(x - width / 2 + 0.5, 0, y - height / 2 + 0.5);
    v.project(camera);
    const rect = renderer.domElement.getBoundingClientRect();
    return {
      x: Math.round(rect.left + ((v.x + 1) / 2) * rect.width),
      y: Math.round(rect.top + ((-v.y + 1) / 2) * rect.height),
      onScreen: v.x > -1 && v.x < 1 && v.y > -1 && v.y < 1 && v.z < 1,
    };
  },
  /** What the pick ray finds at a viewport pixel.
   *
   * "Clicking a robot does nothing" has two very different causes — the ray
   * missing, or the hit having no `robotId` — and they look identical from
   * outside. This distinguishes them. */
  pickAt(x, y) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((x - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((y - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects([...rigs.values()], true);
    return {
      rigCount: rigs.size,
      hits: hits.length,
      first: hits[0]
        ? { name: hits[0].object.name || hits[0].object.type,
            robotId: hits[0].object.userData?.robotId ?? null }
        : null,
      ownersSeen: hits.map((h) => h.object.userData?.robotId ?? null),
    };
  },
  /** Viewport pixel coordinates of a robot, for scripted clicks. */
  screenPositionOf(id) {
    const rig = rigs.get(id);
    if (!rig) return null;
    const v = new THREE.Vector3();
    rig.getWorldPosition(v);
    v.y += 0.3;
    v.project(camera);
    const rect = renderer.domElement.getBoundingClientRect();
    return {
      x: Math.round(rect.left + ((v.x + 1) / 2) * rect.width),
      y: Math.round(rect.top + ((-v.y + 1) / 2) * rect.height),
      onScreen: v.x > -1 && v.x < 1 && v.y > -1 && v.y < 1 && v.z < 1,
    };
  },
};

connect();
render();
