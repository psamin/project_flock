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

// The server owns this state. Both renderers receive it in snapshots and diffs,
// so opening a second view cannot produce a second start or leave destructive
// mission controls active after the run is over.
let simulationStarted = false;
let simulationRunning = false;
let simulationCoordinated = true;
let missionActionInFlight = false;

function syncMissionControls() {
  const disabled = !simulationRunning;
  const kill = document.getElementById("kill-robot");
  if (kill) kill.disabled = disabled;
  const radius = document.getElementById("intervene-radius");
  if (radius) radius.disabled = disabled;
  for (const button of document.querySelectorAll("#intervene-kinds button")) {
    button.disabled = disabled;
  }
  if (disabled && armed) disarm(true);
}

/** Reflect the authoritative mission lifecycle in every connected renderer. */
export function syncSimulationLifecycle(state) {
  if (!("started" in state) && !("running" in state)) return;
  simulationStarted = Boolean(state.started);
  simulationRunning = Boolean(state.running);
  const mode = state.mode ?? state.metrics?.mode;
  if (mode) simulationCoordinated = mode === "coordinated";

  const button = document.getElementById("start-simulation");
  if (button && !missionActionInFlight) {
    if (!simulationStarted) {
      button.textContent = "start";
      button.disabled = false;
      button.className = "ready";
      button.title = "Start mission";
    } else if (simulationRunning) {
      button.textContent = "running";
      button.disabled = false;
      button.className = "running";
      button.title = "Reset mission";
    } else {
      button.textContent = "reset";
      button.disabled = false;
      button.className = "reset";
      button.title = "Reset mission";
    }
  }

  const status = document.getElementById("status");
  // The button already communicates ready/running/reset. Reserve this
  // scarce header slot for connection, startup, and rendering failures.
  if (status) status.textContent = "";
  syncMissionControls();
}

/** Start the prepared mission, or rebuild an active/finished one in-place. */
export function initStartSimulation() {
  const button = document.getElementById("start-simulation");
  if (!button) return;
  button.addEventListener("click", async () => {
    if (missionActionInFlight) return;
    missionActionInFlight = true;
    const resetting = simulationStarted;
    const action = resetting ? "reset" : "start";
    const pending = resetting ? "resetting" : "starting";
    button.disabled = true;
    button.textContent = `${pending}…`;
    const status = document.getElementById("status");
    if (status) status.textContent = `${pending}…`;
    try {
      const response = await fetch(
        resetting ? "/api/mission/restart" : "/api/mission/start",
        resetting
          ? {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ coordinated: simulationCoordinated }),
            }
          : { method: "POST" },
      );
      const state = await response.json();
      if (!response.ok) throw new Error(state.detail || `${action} failed (${response.status})`);
      if (resetting) {
        const ticker = document.getElementById("ticker");
        if (ticker) ticker.innerHTML = "";
      }
      missionActionInFlight = false;
      syncSimulationLifecycle(state);
    } catch (error) {
      missionActionInFlight = false;
      button.disabled = false;
      button.textContent = resetting ? (simulationRunning ? "running" : "reset") : "start";
      button.className = resetting ? (simulationRunning ? "running" : "reset") : "ready";
      if (status) status.textContent = `${action} failed: ${error.message}`;
    }
  });
}

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
    // A run somebody broke is not a measurement. Interventions are a headline
    // feature, and the one thing they must not do is quietly poison §4.7's
    // number: a coordinated run whose fleet was killed scores like the
    // baseline, and publishing that as "coordination gain 0%" would be the
    // demo lying about its own central claim.
    const spoiled = [
      ["coordinated", co],
      ["baseline", base],
    ].filter(([, r]) => r.interfered);
    if (spoiled.length) {
      const what = spoiled
        .map(([mode, r]) => {
          const i = r.interference || {};
          const bits = [];
          if (i.interventions) bits.push(`${i.interventions} disruption(s)`);
          if (i.robots_killed) bits.push(`${i.robots_killed} robot(s) killed`);
          return `${mode}: ${bits.join(", ")}`;
        })
        .join(" · ");
      box.innerHTML =
        `<b>not a fair comparison</b> — an operator interfered with this run ` +
        `(${what}). Restart both modes to measure again.`;
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
  fleet_lost: (e) =>
    `the fleet is gone — ${(e.detail.robots || []).join(", ")} are down, mission over`,
  robot_lost: (e) => `SIGNAL LOST — silent ${e.detail.silent_for_seconds}s`,
  robot_recovered: () => "back on the air",
};

export const TICKER_CLASS = {
  fleet_lost: "bad",
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

/** Put the shared answer card in view before either request begins. */
function beginConsoleAnswer(message) {
  const panel = document.getElementById("console-answer");
  const summary = document.getElementById("console-summary");
  panel.classList.add("active");
  panel.setAttribute("aria-busy", "true");
  summary.className = "";
  summary.textContent = message;
  requestAnimationFrame(() => {
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
}

function finishConsoleAnswer() {
  document.getElementById("console-answer").setAttribute("aria-busy", "false");
}

async function askConsole(question, ctx) {
  const summary = document.getElementById("console-summary");
  const sqlBox = document.getElementById("console-sql");
  const rowsBox = document.getElementById("console-rows");
  beginConsoleAnswer("asking fleet memory…");
  sqlBox.textContent = "";
  rowsBox.textContent = "";
  // Cleared here too: the two tiers share this panel, and leaving the agent's
  // tool trace under a canned answer would credit MCP for a plain psycopg read.
  const steps = document.getElementById("console-steps");
  if (steps) steps.textContent = "";

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
  } finally {
    finishConsoleAnswer();
  }
}

// --- the free-form tier (§6.2 tools #1 and #3) -------------------------------
//
// The six buttons above are the reliable path and stay the demo's spine. This
// is the tier over them: a Bedrock agent that reads the same cluster through
// the CockroachDB Managed MCP Server and loads CockroachDB Agent Skills as it
// works. Its steps are rendered rather than hidden, because "an agent answered"
// is a claim, and the tool calls underneath it are the evidence.

/** Render one agent answer: what it concluded, then how it got there. */
function renderAgentAnswer(answer) {
  const summary = document.getElementById("console-summary");
  const sqlBox = document.getElementById("console-sql");
  const rowsBox = document.getElementById("console-rows");
  const steps = document.getElementById("console-steps");

  if (answer.error) {
    summary.className = "err";
    summary.textContent = answer.error;
    if (steps) steps.textContent = "";
    finishConsoleAnswer();
    return;
  }
  summary.className = "";
  summary.textContent = answer.text;
  sqlBox.textContent = (answer.sql || []).join("\n\n");

  // The provenance line. A judge reading this can see the model chose a skill
  // and which tools it actually called — neither is inferable from the prose.
  const bits = [
    `${answer.turns} turn${answer.turns === 1 ? "" : "s"}`,
    `${answer.elapsed_s}s`,
    answer.model.replace(/^us\./, ""),
  ];
  if ((answer.skills_used || []).length) {
    bits.push(`skills: ${answer.skills_used.join(", ")}`);
  }
  rowsBox.textContent = bits.join(" · ");

  if (steps) {
    steps.innerHTML = "";
    for (const step of answer.steps || []) {
      const line = document.createElement("div");
      line.className = step.ok ? "ok" : "bad";
      const arg = step.argument.replace(/\s+/g, " ").slice(0, 90);
      line.textContent = `${step.ok ? "→" : "✕"} ${step.tool}${arg ? ` ${arg}` : ""}`;
      steps.appendChild(line);
    }
  }
  finishConsoleAnswer();
}

let agentQuestionInFlight = false;

async function askAgent(ctx) {
  const input = document.getElementById("console-ask");
  const button = document.getElementById("console-ask-go");
  const summary = document.getElementById("console-summary");
  const steps = document.getElementById("console-steps");
  if (agentQuestionInFlight) return;
  const question = input.value.trim();
  if (!question) return;

  // Capture first, then clear synchronously: the submitted prompt should leave
  // the composer immediately, not after the network/model round trip.
  input.value = "";
  agentQuestionInFlight = true;
  button.disabled = true;
  // Named rather than a generic spinner: the wait is several seconds and the
  // two services doing the work are the two tools being claimed.
  beginConsoleAnswer("asking Claude, reading the cluster over MCP…");
  document.getElementById("console-sql").textContent = "";
  document.getElementById("console-rows").textContent = "";
  if (steps) steps.textContent = "";

  try {
    const answer = await (
      await fetch("/api/console/ask-agent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      })
    ).json();
    renderAgentAnswer(answer);
  } catch (err) {
    summary.className = "err";
    summary.textContent = `agent error: ${err.message}`;
    finishConsoleAnswer();
  } finally {
    agentQuestionInFlight = false;
    button.disabled = false;
  }
}

/** Enable the ask row, or explain why it is off. */
async function initAgent(ctx) {
  const row = document.getElementById("console-agent");
  if (!row) return;
  const input = document.getElementById("console-ask");
  const button = document.getElementById("console-ask-go");
  const note = document.getElementById("console-agent-note");

  const send = () => askAgent(ctx);
  button.addEventListener("click", send);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") send();
  });

  try {
    const state = await (await fetch("/api/console/agent")).json();
    if (!state.available) {
      input.disabled = true;
      button.disabled = true;
      note.textContent = state.reason || "unavailable";
      return;
    }
    note.textContent = `Claude + MCP · ${state.skills} skills`;
  } catch {
    input.disabled = true;
    button.disabled = true;
    note.textContent = "unreachable";
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
  initAgent(ctx);
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

// --- operator interventions (issue #22) --------------------------------------
//
// Breaking the world is a two-step gesture on purpose: arm a disruption, then
// click where it goes. A one-click "drop fire" button beside the mission
// controls is one misclick away from ending a run somebody is recording, and
// the arm step also gives the cursor somewhere to say what it is about to do.

let armed = null; // {kind, radius} or null

/** The armed disruption, for a renderer deciding what a canvas click means. */
export function armedIntervention() {
  return armed;
}

function setStatus(text, className = "") {
  const status = document.getElementById("intervene-status");
  if (!status) return;
  status.className = className;
  status.textContent = text;
}

/** Un-arm. `keepStatus` is for the success path, which has something to say —
 * without it the confirmation is overwritten by the idle prompt in the same
 * frame and the operator sees no feedback at all. */
function disarm(keepStatus = false) {
  armed = null;
  for (const b of document.querySelectorAll("#intervene-kinds button")) {
    b.classList.remove("armed");
  }
  document.body.classList.remove("arming");
  if (!keepStatus) setStatus("pick a disruption, then click a tile");
}

/** Place the armed disruption at a tile. Returns true if it was accepted.
 *
 * The interesting branch is the 409: the world refusing to let an operator seal
 * off a victim for good. Surfacing that reason verbatim is the difference
 * between "nothing happened" and "you were about to kill v3". */
export async function placeIntervention(x, y) {
  if (!armed || !simulationRunning) return false;
  const { kind, radius } = armed;
  setStatus(`sending ${kind} at ${x},${y}…`);
  try {
    const reply = await fetch("/api/intervene", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, x, y, radius }),
    });
    const body = await reply.json();
    if (!reply.ok) {
      const reason = (body.detail && body.detail.reason) || `refused (${reply.status})`;
      setStatus(reason, "err");
      return false;
    }
    // Deliberately reports the transport. The whole point of routing this
    // through fleet memory is that the changefeed carries it, and a panel that
    // silently fell back to a 1 Hz poll should say so rather than just feel
    // slower.
    setStatus(`${body.label} at ${x},${y} · via ${body.transport}`, "ok");
    disarm(true);
    return true;
  } catch (err) {
    setStatus(`intervention failed: ${err.message}`, "err");
    return false;
  }
}

/** Wire the disruption buttons and the radius control. */
export async function initInterventions() {
  const holder = document.getElementById("intervene-kinds");
  if (!holder) return;
  let data;
  try {
    data = await (await fetch("/api/interventions")).json();
  } catch {
    setStatus("interventions unavailable", "err");
    return;
  }

  const radius = document.getElementById("intervene-radius");
  if (radius) {
    radius.max = String(data.max_radius);
    radius.disabled = !simulationRunning;
    radius.addEventListener("input", () => {
      if (armed) armed.radius = Number(radius.value);
      const label = document.getElementById("intervene-radius-label");
      if (label) label.textContent = radius.value;
    });
  }

  holder.innerHTML = "";
  for (const kind of data.kinds) {
    const button = document.createElement("button");
    button.textContent = kind.label;
    button.disabled = !simulationRunning;
    const tag = document.createElement("span");
    tag.className = "mem";
    // Which disruptions a lifter can undo, and which are permanent. It is the
    // one property that changes what an operator is actually doing.
    tag.textContent = kind.clearable ? "CLEARABLE" : "PERMANENT";
    button.appendChild(tag);
    button.addEventListener("click", () => {
      const wasArmed = armed && armed.kind === kind.id;
      disarm();
      if (wasArmed) return; // clicking the armed one disarms it
      armed = { kind: kind.id, radius: radius ? Number(radius.value) : 1 };
      button.classList.add("armed");
      document.body.classList.add("arming");
      setStatus(`click a tile to place ${kind.label}`);
    });
    holder.appendChild(button);
  }
  disarm();

  // Escape is the way out of an armed cursor. Without it the only way to
  // un-arm is to click a tile, which is exactly what the operator is trying
  // not to do.
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && armed) disarm();
  });
}

// --- the data layer, on screen (§3.6) ----------------------------------------

/** Live row counts per memory type, so CockroachDB is watchable not asserted.
 *
 * The data layer is the thesis and it was the one part of the demo with nothing
 * to look at: a viewer saw robots moving and had to take on faith that any of it
 * went through a database. Grouped by the four memory types the schema is
 * organised around, so the four-memory claim is legible at a glance.
 *
 * Zeros render dimmer rather than being hidden. A memory type with no rows is
 * worth seeing — that is how a gap gets closed rather than quietly shipped.
 */
export async function refreshMemoryRail() {
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

/** §3.6's first failure beat: kill a robot, watch its work get taken over.
 *
 * AUDIT B measured zero contended claims across a whole normal run, so the
 * lease-takeover branch — the entire "why a database and not a queue" argument
 * — never fires on its own. This is the button that fires it.
 *
 * The answer names the orphaned task and the remaining lease seconds, because
 * the fifteen seconds of nothing happening is the part that needs narrating.
 */
export function initKillRobot() {
  const button = document.getElementById("kill-robot");
  if (!button) return;
  button.disabled = !simulationRunning;
  button.addEventListener("click", async () => {
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
      if (!box) return;
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
      /* a demo aid; never let it break the render loop */
    } finally {
      button.disabled = !simulationRunning;
    }
  });
}

// --- the coordination feed (§3.6) --------------------------------------------

const VERB = {
  claim_task: "claimed",
  explore: "swept",
  return_to_base: "returned to base",
};

/** "m1 claimed deliver_kit at 12,10 — because s2 reported a victim there."
 *
 * Robots never message each other. Their only communication is rows, so a feed
 * of rows is the coordination feed, and naming both ends of each one is the
 * thesis stated in the demo's own words rather than in narration over it.
 *
 * Solo decisions are shown too, dimmed. Hiding them would make every visible
 * line look like teamwork and quietly overstate the thing being demonstrated.
 */
export async function refreshCoordination() {
  const box = document.getElementById("coordination");
  if (!box) return;
  try {
    const data = await (await fetch("/api/coordination?limit=14")).json();
    if (!data.lines?.length) {
      box.innerHTML = `<div class="co-empty">no decisions yet</div>`;
      return;
    }
    box.innerHTML = data.lines
      .map((ln) => {
        const act = ln.chosen?.kind ?? VERB[ln.chosen?.action] ?? ln.trigger;
        const at = ln.chosen?.target ? ` at ${ln.chosen.target.join(",")}` : "";
        const brain = ln.source === "bedrock" ? "claude" : "rules";
        const why = ln.cross_agent
          ? `because <b>${ln.informed_by.join(", ")}</b> reported ` +
            `${ln.lead_belief?.kind ?? "something"}` +
            (ln.lead_belief ? ` at ${ln.lead_belief.x},${ln.lead_belief.y}` : "") +
            (ln.lead_belief?.sightings > 1
              ? ` (${ln.lead_belief.sightings} sightings)`
              : "")
          : `on what it saw itself`;
        // "after", never "because of": a belief inside the blast radius written
        // after the disruption is correlation, and the robot may have re-planned
        // for its own reasons. Overclaiming here would be the one place this
        // panel could mislead.
        const after = ln.responds_to
          ? `<span class="co-after">after ${ln.responds_to.label} ` +
            `at ${ln.responds_to.x},${ln.responds_to.y}</span>`
          : "";
        return (
          `<div class="co-line ${ln.cross_agent ? "crossed" : "solo"}` +
          `${ln.responds_to ? " disrupted" : ""}">` +
          `<span class="co-who">${ln.robot}</span> ${act}${at}` +
          `<span class="co-why">${why}</span>` +
          after +
          `<span class="co-brain ${brain}">${brain}</span>` +
          `</div>`
        );
      })
      .join("");
  } catch {
    /* a panel; never let it break the render loop */
  }
}

// --- the fleet panel (§3.6) --------------------------------------------------

/** Every unit, what it holds, its lease countdown, and what it last decided.
 *
 * The map says where robots are and the ticker says what happened. Neither
 * answers "what is each unit doing, and why" without clicking through them one
 * at a time — six clicks for a question an operator asks continuously.
 *
 * The lease countdown is the column that earns its place: a held task is
 * invisible in `open_tasks` until its lease lapses, so this is the only place
 * the takeover mechanism can be watched *before* it fires. It is what turns the
 * kill-a-robot beat from fifteen seconds of nothing into a visible timer.
 */
export async function refreshFleet() {
  const box = document.getElementById("fleet");
  if (!box) return;
  try {
    const data = await (await fetch("/api/fleet")).json();
    box.innerHTML = (data.robots || [])
      .map((r) => {
        const job = r.task
          ? `${r.task.kind} @ ${r.task.target.join(",")}` +
            // "renewing" while the robot is alive, an approximate countdown
            // once it is not. The tilde is load-bearing: this is derived from
            // when the robot stopped, not read off the lease column.
            (r.task.lease_seconds_left != null
              ? ` <span class="lease ${r.task.lease_seconds_left <= 5 ? "low" : ""}">` +
                `lapses ~${r.task.lease_seconds_left}s</span>`
              : ` <span class="lease">lease renewing</span>`)
          : `<span class="idle">idle</span>`;
        const d = r.last_decision;
        const why = d
          ? `<span class="fl-why" title="${d.rationale.replace(/"/g, "&quot;")}">` +
            `${d.rationale}</span>` +
            `<span class="co-brain ${d.source === "bedrock" ? "claude" : "rules"}">` +
            `${d.source === "bedrock" ? "claude" : "rules"}</span>`
          : "";
        return (
          `<div class="fl-row ${r.down ? "down" : ""}">` +
          `<span class="fl-id ${r.role}">${r.id}</span>` +
          `<span class="fl-job">${r.down ? "<b>DOWN</b>" : job}</span>` +
          why +
          `</div>`
        );
      })
      .join("");
  } catch {
    /* a panel; never let it break the render loop */
  }
}


/** §4.7's comparison, without waiting three and a half minutes for it.
 *
 * Watching both modes play out costs a mission per arm at 4 Hz — coordinated
 * finishes near tick 312, baseline runs to ~560 *because it fails* — which is
 * longer than the entire video. The same two missions run headless in about
 * two and a half seconds.
 *
 * Labelled as headless on screen. It is the same code path and the same seed as
 * the mission being watched, but it is not the run on screen, and a number
 * presented as though it were would be the demo overclaiming about itself.
 */
export function initCompare() {
  const button = document.getElementById("compare");
  if (!button) return;
  button.addEventListener("click", async () => {
    const box = document.getElementById("comparison");
    button.disabled = true;
    if (box) box.textContent = "running both modes on this seed…";
    try {
      const d = await (await fetch("/api/compare", { method: "POST" })).json();
      const co = d.coordinated;
      const base = d.baseline;
      const lives = base.victims_lost - co.victims_lost;
      if (box) {
        box.innerHTML =
          (lives > 0
            ? `<b>${lives} more people died without shared memory</b> ` +
              `(${co.victims_lost} vs ${base.victims_lost}) · `
            : "") +
          `rescued ${co.victims_stabilized}/${co.victims_total} coordinated ` +
          `vs ${base.victims_stabilized}/${base.victims_total} baseline · ` +
          `gain ${Math.round((d.coordination_gain ?? 0) * 100)}% ` +
          `<span class="co-why">seed ${d.seed}, both modes run headless</span>`;
      }
    } catch {
      if (box) box.textContent = "comparison failed — the mission is unaffected";
    } finally {
      button.disabled = false;
    }
  });
}
