/* UI that both renderers share (§4.7, FR-10, FR-17).
 *
 * Extracted from app.js when /sim3d landed. The HUD numbers, the §4.7
 * comparison line, the ticker vocabulary and the commander console are
 * identical in the 2D and 3D views, and a fix applied to one and not the other
 * shows a judge two different numbers for the same mission. That is a worse
 * failure than the extraction risk, which is why this file exists rather than a
 * second copy of app.js's bottom half.
 *
 * What deliberately did NOT move, and must stay per-renderer:
 *
 *   logEvents()  mutates renderer state — screen shake and path ghosts — so
 *                only its formatting half lives here, as formatEvent().
 *   connect()    calls boot() and applyTileChanges(), which mean different
 *                things in each view.
 *
 * The console needs to know which robot is "the" robot, which is renderer
 * state, so it takes accessors rather than reaching for globals. That is the
 * whole reason initConsole() has a parameter.
 */

// --- HUD ---------------------------------------------------------------------

export function setText(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
}

export function updateHud(metrics) {
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
  setText("m-recall", metrics.lessons_known ?? 0);
  // Mode and count together, because either alone misleads: "live" with zero
  // calls has not decided anything yet, and a count without the mode does not
  // say whether those calls were AWS or a cassette.
  const mode = metrics.bedrock_mode;
  setText(
    "m-bedrock",
    mode == null ? "—" : `${mode} · ${metrics.bedrock_calls ?? 0}`,
  );
}

/** §4.7's one number the video ends on, once both modes have run. */
export async function refreshComparison() {
  try {
    const data = await (await fetch("/api/runs")).json();
    const runs = data.runs || {};
    const box = document.getElementById("comparison");
    if (!box) return;
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

// --- the ticker vocabulary ---------------------------------------------------

export const TICKER_TEXT = {
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

export const TICKER_CLASS = {
  victim_found: "found",
  victim_stabilized: "good",
  victim_lost: "bad",
  aftershock: "shock",
  sector_claimed: "sector",
  sector_swept: "sector",
  robot_lost: "bad",
  robot_recovered: "good",
};

/** Verbs the stream carries that nobody wants to read scroll past. */
export const EVENT_NOISE = new Set(["action_rejected", "tile_visited"]);

/** One event as a ticker line, or null if it is noise.
 *
 * The event stream carries verbs beyond TICKER_TEXT — that is why the fallback
 * exists rather than being defensive for its own sake. Any consumer that maps
 * verbs to behaviour (the 3D camera director, for one) needs the same
 * default-and-carry-on discipline, or one unmapped verb stops it dead. */
export function formatEvent(e) {
  if (!e || EVENT_NOISE.has(e.verb)) return null;
  const say = TICKER_TEXT[e.verb];
  return {
    className: TICKER_CLASS[e.verb] || "",
    text:
      `${String(e.tick).padStart(4, " ")}  ${e.actor.padEnd(6)} ` +
      (say ? say(e) : e.verb.replace(/_/g, " ")),
  };
}

// --- commander console (FR-10) ----------------------------------------------

/** Which robot a question about "robot X" should ask about.
 *
 * The selected one when the provenance panel is open, otherwise a scout — a
 * lifter has logged nothing until a scout has found somebody to dig out, so
 * defaulting alphabetically makes the flagship question look broken for the
 * first forty ticks of every run. */
function subjectRobot(ctx) {
  const selected = ctx.getSelected();
  if (selected) return selected;
  const robots = ctx.getRobots();
  const scout = robots.find((r) => r.role === "scout");
  return scout ? scout.id : robots.length ? robots[0].id : "s1";
}

async function askConsole(question, ctx) {
  const summary = document.getElementById("console-summary");
  const sqlBox = document.getElementById("console-sql");
  const rowsBox = document.getElementById("console-rows");
  summary.className = "";
  summary.textContent = "asking fleet memory…";
  sqlBox.textContent = "";
  rowsBox.textContent = "";

  const body = { question };
  const subject = subjectRobot(ctx);
  if (question === "why_did_robot") body.robot_id = subject;
  if (question === "what_do_we_know") {
    // Centred on the selected robot when there is one: "what do we know around
    // here" is the question a commander actually asks, and (0,0) is a corner.
    const robot = ctx.getRobots().find((r) => r.id === subject);
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

/** Wire the six question buttons.
 *
 * `ctx` supplies the renderer state the questions depend on:
 *   getRobots()   -> the current robot list
 *   getSelected() -> the robot id whose provenance panel is open, or null
 */
export async function initConsole(ctx) {
  const holder = document.getElementById("console-questions");
  const status = document.getElementById("console-status");
  if (!holder) return;
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
      button.addEventListener("click", () => askConsole(q.id, ctx));
      holder.appendChild(button);
    }
  } catch {
    status.textContent = "console unreachable";
  }
}
