/* Camera director for the digital-twin view.
 *
 * WHY THIS EXISTS: /sim3d carries the whole submission video, and a static
 * orbit over a 40x30 block for three minutes is dead footage. The event stream
 * already knows where the story is — a task was claimed, a victim was
 * stabilized, the map just changed — so the camera can frame itself. That turns
 * "record a 1200-tick mission and hope the camera was pointed the right way"
 * into something shootable in one take.
 *
 * WHAT IT DELIBERATELY DOES NOT DO: pick an angle. It preserves whatever
 * azimuth and elevation the operator has orbited to and only changes WHERE the
 * camera is looking and HOW CLOSE it is. A director that also chose the angle
 * would fight the person driving.
 *
 *   event ──> cue {target, distance, hold, priority}
 *               │
 *               ├─ higher priority always wins outright
 *               ├─ equal priority: newer wins
 *               └─ cue expires after `hold` ──> idle: slow orbit, wide
 *
 *   user drags ──> OVERRIDE_MS of hands-off, then the director resumes
 *
 * UNMAPPED VERBS ARE A NO-OP, ON PURPOSE. The stream carries verbs that are not
 * in CUES (app.js:543 filters action_rejected and tile_visited as noise, and
 * more can be added server-side any time). One unknown verb must not stop the
 * camera dead, which is why lookup misses fall through to `continue` rather
 * than throwing. Same discipline as app.js:563's ticker fallback.
 */

import * as THREE from "three";

/** How long the director keeps its hands off after the operator touches it. */
const OVERRIDE_MS = 4000;
/** No cue for this long and we drift back to an establishing wide. */
const IDLE_MS = 4200;

// Sized so an idle wide holds the entire 40x30 slab in frame at fov 45.
const WIDE = { distance: 62, height: 0.62 };

/* priority: higher wins. hold: how long the shot stays before expiring. */
const CUES = {
  aftershock:        { priority: 100, hold: 5200, distance: 52, shake: 900 },
  victim_stabilized: { priority: 80,  hold: 3400, distance: 15 },
  victim_lost:       { priority: 80,  hold: 3000, distance: 17 },
  robot_lost:        { priority: 70,  hold: 3600, distance: 20 },
  victim_found:      { priority: 60,  hold: 2600, distance: 14 },
  debris_cleared:    { priority: 50,  hold: 2800, distance: 15 },
  task_claimed:      { priority: 40,  hold: 2400, distance: 22 },
  sector_claimed:    { priority: 20,  hold: 2000, distance: 30 },
};

export class Director {
  constructor(camera, controls) {
    this.camera = camera;
    this.controls = controls;
    this.enabled = true;

    this.target = new THREE.Vector3(0, 0, 0);
    this.desired = new THREE.Vector3(0, 0, 0);
    this.distance = WIDE.distance;
    this.desiredDistance = WIDE.distance;

    this.cue = null;          // {priority, expires, shakeUntil}
    this.overrideUntil = 0;
    this.lastCueAt = 0;
    this.azimuth = 0;         // accumulated idle drift
    this.shakeUntil = 0;
    this.shakePower = 0;
  }

  setEnabled(on) {
    this.enabled = on;
  }

  /** Called from OrbitControls 'start' — the operator has taken the wheel. */
  notifyUserInput(now) {
    this.overrideUntil = now + OVERRIDE_MS;
  }

  get overridden() {
    return performance.now() < this.overrideUntil;
  }

  /**
   * Feed one frame's events.
   * `locate(event)` returns {x, z} in world space, or null if the event has no
   * place on the map — a null simply means "no shot", never an error.
   */
  onEvents(events, locate, now) {
    if (!this.enabled) return;
    for (const event of events || []) {
      const cue = CUES[event.verb];
      if (!cue) continue;               // unmapped verb: no-op, by design
      let where = null;
      try {
        where = locate(event);
      } catch {
        where = null;                   // a lookup fault must not stop the camera
      }
      if (!where) continue;

      const active = this.cue && this.cue.expires > now;
      if (active && cue.priority < this.cue.priority) continue;

      this.desired.set(where.x, 0, where.z);
      this.desiredDistance = cue.distance;
      this.cue = { priority: cue.priority, expires: now + cue.hold };
      this.lastCueAt = now;
      if (cue.shake) {
        this.shakeUntil = now + cue.shake;
        this.shakePower = 0.9;
      }
    }
  }

  /**
   * Advance the camera one frame.
   * `home` is where to drift when nothing is happening — the centroid of the
   * fleet, so an idle wide still contains the robots.
   */
  update(now, home) {
    const shaking = now < this.shakeUntil;

    if (this.enabled && !this.overridden) {
      const stale = !this.cue || this.cue.expires <= now;
      if (stale && now - this.lastCueAt > IDLE_MS) {
        // Nothing happening: drift to an establishing wide on the fleet and
        // orbit slowly, so the shot is never completely still.
        this.desired.copy(home);
        this.desiredDistance = WIDE.distance;
        this.azimuth += 0.00013 * 16;
      }

      this.target.lerp(this.desired, 0.045);
      this.distance += (this.desiredDistance - this.distance) * 0.045;

      // Preserve the operator's angle: take the current camera->target vector,
      // keep its direction, and only change its length.
      const offset = this.camera.position.clone().sub(this.controls.target);
      const spherical = new THREE.Spherical().setFromVector3(offset);
      if (stale && now - this.lastCueAt > IDLE_MS) spherical.theta += 0.0007;
      spherical.radius = this.distance;
      spherical.makeSafe();

      this.controls.target.lerp(this.target, 0.08);
      this.camera.position.copy(
        this.controls.target.clone().add(new THREE.Vector3().setFromSpherical(spherical)),
      );
    }

    if (shaking) {
      // Decays rather than rattling at constant amplitude, so it reads as an
      // impact and not as a broken renderer. Same reasoning as app.js's 2D shake.
      const power = ((this.shakeUntil - now) / 900) * this.shakePower;
      this.camera.position.x += (Math.sin(now * 0.09) + Math.sin(now * 0.037)) * power * 0.16;
      this.camera.position.y += Math.sin(now * 0.13) * power * 0.12;
      this.camera.position.z += (Math.cos(now * 0.11) + Math.cos(now * 0.041)) * power * 0.16;
    }
  }
}
