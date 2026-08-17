/* Robot rigs for the digital-twin view.
 *
 * These carry the "physical intelligence" claim, so they get the detail budget:
 * a viewer spends the whole three minutes looking at four robots and only a
 * couple of seconds at any given wide shot of the block.
 *
 * Three things separate a machine from a game sprite, in order of how much
 * they buy per line of code:
 *
 *   1. Pose. Position AND heading, with the turn happening before the
 *      translation rather than the sprite sliding sideways. See TURN_FRACTION.
 *   2. A visible sensor footprint. The fleet's whole thesis is about what has
 *      and has not been perceived, and a robot with a drawn sensor volume reads
 *      as perceiving where a sprite reads as merely being somewhere.
 *   3. Articulation on the verb. The lifter's arm goes into the debris it is
 *      clearing; the medic's stretcher deploys. Without this, "clear_debris"
 *      is a status string rather than an act.
 *
 * SENSOR FOOTPRINT — READ BEFORE CHANGING:
 * The volume drawn here is a SQUARE of side (2*vision+1), because that is what
 * the simulation actually models. colony/sim/world.py:535-538 builds each
 * robot's percept as range(x-radius, x+radius+1) over both axes, and
 * app.js:138 mirrors it for the 2D fog. Drawing a tapered line-of-sight cone
 * would look better and would be a lie: it would show a robot "seeing" past
 * corners it cannot see past, and dark tiles inside its own beam. If vision
 * ever becomes true line-of-sight, change the server, the fog and this file in
 * the same commit or the view starts contradicting the sim. (Tracked in
 * TODOS.md item 3.)
 */

import * as THREE from "three";
import { CSS2DObject } from "three/addons/CSS2DRenderer.js";

export const ROLE_COLOR = {
  scout: 0x63c5da,
  lifter: 0xd9884a,
  medic: 0xd96a9a,
};

const ROLE_HEX = {
  scout: "#63c5da",
  lifter: "#d9884a",
  medic: "#d96a9a",
};

/* Screen convention is +y down (protocol.py DIRECTIONS), and the grid maps
 * x -> world X, y -> world Z. Rigs are modelled facing +Z. */
const FACING_YAW = { s: 0, e: Math.PI / 2, n: Math.PI, w: -Math.PI / 2 };

/** Share of the 250ms tick window spent rotating before translating.
 *  This single number is most of what makes the fleet read as machines. */
export const TURN_FRACTION = 0.35;

const METAL = 0x8f97a6;
const DARK = 0x2a303a;

function mat(color, opts = {}) {
  return new THREE.MeshStandardMaterial({
    color,
    roughness: opts.roughness ?? 0.55,
    metalness: opts.metalness ?? 0.25,
    emissive: opts.emissive ?? 0x000000,
    emissiveIntensity: opts.emissiveIntensity ?? 1,
    transparent: opts.transparent ?? false,
    opacity: opts.opacity ?? 1,
  });
}

function box(w, h, d, material) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

/** The vision square, as a floor glow plus a low wireframe volume. */
function sensorVolume(radius, color) {
  const side = radius * 2 + 1;
  const group = new THREE.Group();

  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(side, side),
    new THREE.MeshBasicMaterial({
      color,
      transparent: true,
      opacity: 0.055,
      depthWrite: false,
      side: THREE.DoubleSide,
    }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = 0.055;
  group.add(floor);

  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.BoxGeometry(side, 0.9, side)),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.28 }),
  );
  edges.position.y = 0.45;
  group.add(edges);

  // The sweep plane is the part that reads as "actively sampling" rather than
  // "has an area". It rises through the volume and restarts.
  const sweep = new THREE.Mesh(
    new THREE.PlaneGeometry(side, side),
    new THREE.MeshBasicMaterial({
      color,
      transparent: true,
      opacity: 0.1,
      depthWrite: false,
      side: THREE.DoubleSide,
    }),
  );
  sweep.rotation.x = -Math.PI / 2;
  group.add(sweep);
  group.userData.sweep = sweep;

  return group;
}

function telemetryLabel(role) {
  const el = document.createElement("div");
  el.className = "tag";
  el.style.borderLeftColor = ROLE_HEX[role] || "#8f97a6";
  const object = new CSS2DObject(el);
  object.position.set(0, 1.15, 0);
  object.userData.el = el;
  return object;
}

function scoutRig() {
  const group = new THREE.Group();
  const hull = new THREE.Mesh(new THREE.OctahedronGeometry(0.17, 0), mat(ROLE_COLOR.scout, { metalness: 0.5 }));
  hull.castShadow = true;
  hull.position.y = 1.25;
  group.add(hull);

  const eye = new THREE.Mesh(
    new THREE.SphereGeometry(0.06, 10, 8),
    mat(0xbfeaf5, { emissive: 0x63c5da, emissiveIntensity: 1.6 }),
  );
  eye.position.set(0, 1.16, 0.13);
  group.add(eye);

  const rotors = new THREE.Group();
  for (const [dx, dz] of [[-0.2, -0.2], [0.2, -0.2], [-0.2, 0.2], [0.2, 0.2]]) {
    const arm = box(0.05, 0.03, 0.05, mat(DARK));
    arm.position.set(dx, 1.27, dz);
    group.add(arm);
    const disc = new THREE.Mesh(
      new THREE.CircleGeometry(0.15, 14),
      new THREE.MeshBasicMaterial({ color: 0xbfeaf5, transparent: true, opacity: 0.22, side: THREE.DoubleSide }),
    );
    disc.rotation.x = -Math.PI / 2;
    disc.position.set(dx, 1.31, dz);
    rotors.add(disc);
  }
  group.add(rotors);
  group.userData.rotors = rotors;

  // Altitude is only legible against the ground. Without this the drone reads
  // as a sprite floating at an arbitrary height.
  const shadow = new THREE.Mesh(
    new THREE.CircleGeometry(0.22, 16),
    new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.3, depthWrite: false }),
  );
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.y = 0.07;
  group.add(shadow);

  return group;
}

function lifterRig() {
  const group = new THREE.Group();
  const chassis = box(0.46, 0.2, 0.58, mat(ROLE_COLOR.lifter));
  chassis.position.y = 0.28;
  group.add(chassis);

  for (const dx of [-0.26, 0.26]) {
    const track = box(0.12, 0.18, 0.66, mat(DARK, { roughness: 0.85, metalness: 0.1 }));
    track.position.set(dx, 0.16, 0);
    group.add(track);
  }

  const cab = box(0.26, 0.16, 0.24, mat(METAL));
  cab.position.set(0, 0.46, -0.1);
  group.add(cab);

  // Arm pivots at the front of the chassis so it swings down into the tile
  // ahead — the tile the sim is decrementing work_left on.
  const armPivot = new THREE.Group();
  armPivot.position.set(0, 0.4, 0.24);
  const upper = box(0.1, 0.1, 0.42, mat(METAL, { metalness: 0.6 }));
  upper.position.z = 0.21;
  armPivot.add(upper);
  const bucket = box(0.26, 0.14, 0.16, mat(0xb8721f, { metalness: 0.5 }));
  bucket.position.z = 0.46;
  armPivot.add(bucket);
  group.add(armPivot);
  group.userData.arm = armPivot;

  return group;
}

function medicRig() {
  const group = new THREE.Group();
  const chassis = box(0.42, 0.2, 0.54, mat(ROLE_COLOR.medic));
  chassis.position.y = 0.26;
  group.add(chassis);

  const wheels = new THREE.Group();
  const wheelGeo = new THREE.CylinderGeometry(0.11, 0.11, 0.07, 12);
  for (const [dx, dz] of [[-0.23, -0.19], [0.23, -0.19], [-0.23, 0.19], [0.23, 0.19]]) {
    const wheel = new THREE.Mesh(wheelGeo, mat(DARK, { roughness: 0.9, metalness: 0.05 }));
    wheel.rotation.z = Math.PI / 2;
    wheel.position.set(dx, 0.11, dz);
    wheel.castShadow = true;
    wheels.add(wheel);
  }
  group.add(wheels);
  group.userData.wheels = wheels;

  // A cross, so the role is readable at wide-shot distance without the label.
  const crossMat = mat(0xf4f7fa, { emissive: 0xd96a9a, emissiveIntensity: 0.35 });
  const bar1 = box(0.2, 0.045, 0.06, crossMat);
  const bar2 = box(0.06, 0.045, 0.2, crossMat);
  bar1.position.y = bar2.position.y = 0.38;
  group.add(bar1, bar2);

  // Stretcher slides out of the back on stabilize.
  const stretcher = box(0.3, 0.05, 0.44, mat(0xdfe6ee, { metalness: 0.1 }));
  stretcher.position.set(0, 0.3, -0.1);
  group.add(stretcher);
  group.userData.stretcher = stretcher;

  return group;
}

const BUILDERS = { scout: scoutRig, lifter: lifterRig, medic: medicRig };

/** A complete robot: chassis group, sensor volume, telemetry label, trace. */
export function makeRig(robot) {
  const role = robot.role in BUILDERS ? robot.role : "lifter";
  const root = new THREE.Group();

  const chassis = BUILDERS[role]();
  // Every mesh under the chassis answers to the same robot on a raycast, so
  // clicking any part of it opens that robot's provenance (FR-17).
  chassis.traverse((node) => {
    node.userData.robotId = robot.id;
  });
  root.add(chassis);
  root.userData.chassis = chassis;

  const sensor = sensorVolume(robot.vision ?? 2, ROLE_COLOR[role]);
  root.add(sensor);
  root.userData.sensor = sensor;

  const label = telemetryLabel(role);
  root.add(label);
  root.userData.label = label;

  root.userData.robotId = robot.id;
  root.userData.role = role;
  root.userData.yaw = FACING_YAW[robot.facing] ?? 0;
  return root;
}

/** A fading breadcrumb of where this robot has been. */
export function makeTrace(role) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(TRACE_MAX * 3), 3));
  geometry.setDrawRange(0, 0);
  const line = new THREE.Line(
    geometry,
    new THREE.LineBasicMaterial({
      color: ROLE_COLOR[role] ?? METAL,
      transparent: true,
      opacity: 0.4,
    }),
  );
  line.userData.points = [];
  return line;
}

export const TRACE_MAX = 220;

export function pushTrace(line, x, z) {
  const points = line.userData.points;
  const last = points[points.length - 1];
  if (last && Math.abs(last[0] - x) < 0.02 && Math.abs(last[1] - z) < 0.02) return;
  points.push([x, z]);
  if (points.length > TRACE_MAX) points.shift();
  const array = line.geometry.attributes.position.array;
  for (let i = 0; i < points.length; i++) {
    array[i * 3] = points[i][0];
    array[i * 3 + 1] = 0.09;
    array[i * 3 + 2] = points[i][1];
  }
  line.geometry.setDrawRange(0, points.length);
  line.geometry.attributes.position.needsUpdate = true;
  line.geometry.computeBoundingSphere();
}

/** Shortest-arc angle step, so a robot turning from w to n never spins 270°. */
export function approachAngle(from, to, t) {
  let delta = ((to - from + Math.PI) % (Math.PI * 2)) - Math.PI;
  if (delta < -Math.PI) delta += Math.PI * 2;
  return from + delta * t;
}

export function facingYaw(facing) {
  return FACING_YAW[facing] ?? 0;
}

/**
 * Drive one rig for this frame.
 *
 * `pose`  {x, z, yaw}      interpolated world position and heading
 * `state` {robot, lost, acting, moving, now}
 */
export function updateRig(root, pose, state) {
  root.position.x = pose.x;
  root.position.z = pose.z;
  root.userData.chassis.rotation.y = pose.yaw;

  const { robot, lost, acting, moving, now } = state;
  const chassis = root.userData.chassis;
  const role = root.userData.role;

  // A robot nobody has heard from is frozen on the frame it went quiet, and
  // dimmed. Animating it would assert the opposite of what has happened.
  chassis.visible = true;
  const dim = lost ? 0.3 : robot.status === "stranded" ? 0.55 : 1;
  chassis.traverse((node) => {
    if (node.material && node.material.opacity !== undefined && node.userData.robotId) {
      if (dim < 1) {
        node.material.transparent = true;
        node.material.opacity = dim;
      } else if (node.material.transparent && node.material.opacity < 1) {
        node.material.opacity = 1;
      }
    }
  });

  root.userData.sensor.visible = !lost;
  if (!lost) {
    const sweep = root.userData.sensor.userData.sweep;
    // 2.4s sweep: slow enough to read as deliberate sampling, fast enough that
    // a three-second shot catches a full pass.
    sweep.position.y = 0.08 + ((now % 2400) / 2400) * 0.8;
  }

  if (role === "scout" && !lost) {
    root.userData.chassis.userData.rotors ??= null;
    const rotors = chassis.userData.rotors;
    if (rotors) rotors.rotation.y = (now / 90) % (Math.PI * 2);
    // A gentle bob so a hovering drone is never perfectly static.
    chassis.position.y = Math.sin(now / 520) * 0.045;
  }

  if (role === "lifter") {
    const arm = chassis.userData.arm;
    if (arm) {
      // Digs when acting, stows otherwise, and eases rather than snapping.
      const target = acting ? 0.55 + Math.sin(now / 150) * 0.16 : -0.15;
      arm.rotation.x = arm.rotation.x + (target - arm.rotation.x) * 0.12;
    }
  }

  if (role === "medic") {
    const wheels = chassis.userData.wheels;
    if (wheels && moving && !lost) {
      for (const wheel of wheels.children) wheel.rotation.x = (now / 110) % (Math.PI * 2);
    }
    const stretcher = chassis.userData.stretcher;
    if (stretcher) {
      const target = acting ? -0.46 : -0.1;
      stretcher.position.z += (target - stretcher.position.z) * 0.12;
    }
  }
}

/**
 * The telemetry block above each robot.
 *
 * Compact by default and expanded only for the selected robot. Four robots on a
 * 40x30 block cluster constantly, and four full telemetry blocks cover most of
 * the diorama — the labels were louder than the thing they were labelling. The
 * pose readout stays in the compact form because decimals and units are exactly
 * what separates "instrument" from "game HUD"; what goes is the battery, the
 * kit count and the verbose spacing.
 *
 * `slot` staggers the anchor height so overlapping robots do not stack their
 * labels in the same pixel row.
 */
export function updateLabel(root, pose, robot, lost, expanded = false, slot = 0) {
  const label = root.userData.label;
  const el = label.userData.el;
  label.position.y = 1.05 + (slot % 4) * 0.42;

  const heading = Math.round(((pose.yaw * 180) / Math.PI + 360) % 360);
  const say = lost ? "SIGNAL LOST" : robot.bubble || robot.status || "idle";
  el.classList.toggle("lost", !!lost);
  el.classList.toggle("wide", !!expanded);

  const pos = `${pose.gx.toFixed(1)},${pose.gy.toFixed(1)}`;
  const hdg = `${String(heading).padStart(3, "0")}°`;

  if (!expanded) {
    el.innerHTML =
      `<span class="who">${robot.id.toUpperCase()}</span>` +
      `<span class="num"> ${pos} ${hdg} v${robot.vision ?? 2}</span>`;
    return;
  }

  el.innerHTML =
    `<span class="who">${robot.id.toUpperCase()} · ${robot.role}</span>\n` +
    `<span class="num">pos ${pos}  hdg ${hdg}  vision ${robot.vision ?? 2}` +
    (robot.role === "medic" ? `  kits ${robot.kits ?? 0}` : "") +
    `  batt ${robot.battery ?? 0}</span>\n` +
    `<span class="say">${say}</span>`;
}
