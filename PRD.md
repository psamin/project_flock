# Fleet Coordination Layer — PRD v3

**Working codename:** Colony *(roaches survive disasters — on-brand, rename freely)*
**Hackathon:** CockroachDB × AWS "Build with Agentic Memory" (Devpost) — **deadline Aug 18, 5:00pm EDT**
**Owner:** Praneeth · **Team:** 5 · **Date:** Aug 1, 2026 · **Changes from v2:** hackathon rules deep dive, tool-by-tool CRDB integration plan, vector syntax validated against v26.2 docs, AWS service decisions locked, resilience-demo mechanics corrected, day-1 checklist added; sections reordered so sources and open questions close the doc. (v2 added market research, scenario spec, FRs, DDL, metrics, lane checklists, day-by-day plan.)

---

## 1. Executive summary

Robots are already autonomous. They are not yet teammates. Colony is a shared-memory coordination layer, built on CockroachDB, that lets a heterogeneous robot fleet run a mission as one team: shared beliefs about the world, transactional task claiming, automatic handoffs when one robot's step unblocks another's, and replanning when the world changes. We demonstrate it in a disaster-relief simulation where the fleet keeps rescuing people even while a database node is killed mid-mission.

The market research below supports three claims we'll make to judges:

1. **The gap is real.** Every layer of today's fleet software stack — vendor fleet managers, vendor-agnostic platforms, interoperability standards, even AWS's own discontinued RoboRunner — stops at telemetry, traffic deconfliction, and one-task-to-one-robot dispatch. Task-level teamwork between robots is the layer nobody ships.
2. **The demand is documented.** Users of Open-RMF, the leading open-source fleet framework, are literally asking its forums for inter-robot task dependency and handoffs. DARPA SubT identified operator overload and robot attrition as the top field problems — both are coordination problems.
3. **The timing is right.** Record capital is flooding into making individual robots smarter (foundation models, humanoids), while the "team brain" layer stays unclaimed. LLM agents plus distributed SQL with vector search make that layer buildable now — by five people in seventeen days.

---

## 2. Market research

### 2.1 Market size and growth

Three concentric markets matter to this project:

| Market | Size & trajectory | Source |
|---|---|---|
| **AMR/AGV fleet management software** (the direct category) | ~$1.58B in 2025 → projected $5.23B by 2032, 18.7% CAGR. Driven by warehouses, manufacturing, logistics hubs, e-commerce fulfillment. | MarketsandMarkets, Nov 2025 |
| **Search-and-rescue robotics** (our showcase domain) | Estimates cluster around $25–35B in 2025 with mid-teens CAGR; Mordor Intelligence puts it at $35.3B (2025) → $70.3B (2030) at 14.8% CAGR. Other firms (MRFR, SkyQuest, Research Nester) land in the $22–28B base range growing 13–20%. Autonomy is the fastest-growing operation segment. | Mordor Intelligence 2025; MRFR; SkyQuest; Research Nester |
| **Physical AI / robotics venture market** (the wave we ride) | Robotics and physical-AI startups raised a record ~$27.6B across ~1,009 deals in 2025 — more than double the prior year (PitchBook). SVB's 2026 report finds hardware now takes roughly a third of US VC excluding the OpenAI/Anthropic mega-rounds, and 2026 YTD had already eclipsed all of 2025 by mid-year. | PitchBook via Future Investments, May 2026; SVB Physical AI Report 2026 |

Takeaway for the pitch: the fleet-software category alone is a multi-billion-dollar market growing ~19% a year, and the disaster-robotics domain we demo in is an order of magnitude larger. We are not inventing demand; we're filling a documented hole in a funded stack.

### 2.2 Competitive landscape

The space sorts into five groups. None of them do what we're building.

| Category | Representative players | What they actually do | What they don't do |
|---|---|---|---|
| **Single-vendor fleet managers** | MiR Fleet, Geek+, Locus Robotics, KUKA | Centralized dashboards for one vendor's robots. MiR Fleet, for example, auto-assigns each task to the most suitable AMR by location and availability, and manages traffic for 100+ robots. | One task → one robot. No multi-robot task chains, no shared beliefs, locked to one hardware vendor. |
| **Vendor-agnostic fleet platforms** | Formant, InOrbit, SYNAOS | Observability, teleoperation, data ingestion, analytics across mixed fleets. SYNAOS orchestrates heterogeneous AGV/AMR fleets over the VDA 5050 standard. | These are monitoring and dispatch layers. Robots don't share a world model or trigger each other's work. The "intelligence" is dashboards for humans. |
| **Interoperability standards** | VDA 5050, MassRobotics interop standard, **Open-RMF** | Common message formats; traffic deconfliction. Open-RMF (backed by Intrinsic/OSRF) is the flagship: fleet adapters, map alignment, task allocation, and "mutex groups" — virtual locks so only one robot occupies a corridor or doorway at a time. | Traffic rules ≠ teamwork. In July 2025 a user on the official Open Robotics forum asked whether RMF supports actual inter-robot task dependency — robot A carries an item to a rendezvous, robot B takes it onward — as opposed to just multi-fleet scheduling. That handoff pattern, the exact core of our product, is what the community is asking for and not getting. |
| **The hyperscaler attempt** | **AWS IoT RoboRunner** (2021–~2024) | AWS previewed RoboRunner in Nov 2021 and made it GA in Nov 2022, built on Amazon fulfillment-center tech: a central repository standardizing robot status, location, facility and task data across vendors, plus "Shared Space Management" for corridor traffic. Task orchestration was left to customers to build on its APIs. Its client has since been removed from the AWS SDK, and sibling service RoboMaker hit end-of-support on Sept 10, 2025. | RoboRunner proves two things at once: a hyperscaler validated the exact need (multi-vendor fleets working together), and the product still stopped at data standardization + traffic — the teamwork layer was homework. Then it quietly went away. The need didn't. |
| **Research frontier** | DARPA SubT teams; USC's RobotFleet (Oct 2025) | SubT (2018–2021) fielded heterogeneous ground+aerial teams for underground search with one human operator; its post-mortems flag operator cognitive overload, robot attrition, and heterogeneous interoperability as the defining challenges, with degraded comms forcing robots to exchange data only when necessary. RobotFleet, an open-source USC framework, uses LLM planners over a shared declarative world state to coordinate heterogeneous robots — published October 2025. | Research is converging on exactly our architecture (LLM reasoning + shared world state) but ships as papers and prototypes, not as a durable, resilient memory substrate. Nobody in this row treats the shared state itself as production infrastructure that must survive failures. That's our wedge — and CockroachDB's whole thesis. |

### 2.3 Gap analysis: the coordination stack

Think of fleet software as a five-layer stack. The market has filled four:

```
 L5  Shared cognition & task teamwork      ← EMPTY. This is Colony.
     (shared beliefs, task chains, handoffs,
      reassignment, cross-mission learning)
 L4  Task dispatch                          MiR Fleet, Locus, RoboRunner samples
     (one task → best single robot)
 L3  Traffic deconfliction                  Open-RMF mutex groups, VDA 5050,
     (don't collide, share corridors)       RoboRunner Shared Space Mgmt
 L2  Telemetry & observability              Formant, InOrbit
 L1  Connectivity & fleet gateways          Vendor SDKs, ROS 2, VDA 5050
```

Every incumbent tops out at L3–L4. The evidence that L5 is wanted: the Open-RMF community explicitly requesting task dependency between robots; SubT post-mortems showing one operator can't be the fleet's brain; AWS building (then abandoning) the data substrate for it. L5 is also precisely what "agentic memory" means when the agents have bodies — which is why this project fits this hackathon so unusually well.

### 2.4 Why now

Four shifts make L5 buildable in 2026 when it wasn't in 2021:

1. **Robot brains are being commoditized.** At CES 2026, Nvidia's Jensen Huang declared robotics is having its "ChatGPT moment," and Boston Dynamics announced in January 2026 that Google DeepMind's Gemini Robotics models will power Atlas — the clearest sign yet that per-robot intelligence is becoming a purchasable layer. When every robot has a capable brain, the bottleneck moves to the team.
2. **Capital is funding the wrong layer (for now).** 2026 physical-AI funding is dominated by robotic foundation models and general-purpose robots — roughly three-quarters of disclosed capital by mid-2026 — i.e., individual capability. Fleet coordination software barely registers as a funded category. That's white space, not absence of need.
3. **LLM agents make task-level reasoning cheap.** Decomposing "rescue the person behind that rubble" into a scout→lifter→medic chain used to be a bespoke planning-systems problem. Now it's a prompt plus a well-designed state store.
4. **The state store finally exists.** Coordination state needs transactional integrity (no two robots claim one victim), semantic recall (is this the same victim seen from the other side?), event streams (completion triggers), and survival through infrastructure failure. Distributed SQL + native vectors + changefeeds is exactly that shape. Five years ago you'd have glued together four systems and the glue would be the weak point.

### 2.5 Positioning

**For** operators of heterogeneous robot fleets in high-stakes environments, **who** need robots to work as a team rather than a set of individually-clever units, **Colony** is a coordination layer **that** gives the fleet a shared, durable brain — beliefs, tasks, triggers, and learning — **unlike** fleet managers and interop standards that stop at dashboards and traffic rules, **because** it treats fleet memory as production infrastructure that survives failure, built on CockroachDB.

Wedge and expansion story (for the "real-world impact" criterion): disaster response is the showcase because coordination failures there cost lives and infrastructure failure is guaranteed — but the same layer applies unchanged to warehouse task chains (pick→transport→pack), hospital logistics, mining, and construction. The demo is a sim; the schema, claiming semantics, and reconcile gate are the real product and are robot-vendor-agnostic by design (any robot that can hit the `fleetmem` API can join the team — same "mixed levels of integration" philosophy that made Open-RMF adoptable).

### 2.6 What this means for the pitch

Lines the video and Devpost writeup should use, each backed by a source in §7:

- "Fleet management software is a $1.6B market growing 19% a year — and every product in it stops at traffic rules and single-robot dispatch."
- "Users of the leading open-source fleet framework are asking its forums for robot-to-robot handoffs. We built that."
- "AWS validated this need with RoboRunner, then sunset it. The teamwork layer is still homework. We did the homework."
- "DARPA SubT's own lessons: one human can't be the fleet's brain. So we gave the fleet a brain that can't die — and then we killed a database node on camera to prove it."
- "Search-and-rescue robotics is a $35B market. Individual autonomy is funded; team autonomy is the gap."

---

## 3. Product specification

### 3.1 Personas

- **Incident commander (human, primary demo persona).** Oversees the mission, doesn't micromanage robots. Needs situational awareness in seconds: who's unreached, what's blocking, what changed. Served by the MCP-powered console. Directly addresses the SubT operator-overload finding.
- **Robot integrator (developer persona, post-hackathon).** Wants any robot to join the team by implementing a small API. Served by the `fleetmem` SDK and schema contract.
- **Hackathon judge (meta-persona).** Needs to see, in under 3 minutes, that CockroachDB is load-bearing, the coordination is real, and the idea generalizes. Every design choice below is audited against this persona.

### 3.2 User stories (P0 unless marked)

- As a scout, when I spot a trapped person, my report merges with any existing sighting of the same person instead of creating a duplicate rescue effort.
- As a lifter, when a scout's find creates a debris-clearing task I'm suited for, I claim it exactly once, even if another lifter tries at the same instant.
- As a medic, the moment the lifter finishes clearing, my delivery task unblocks — without any robot messaging me directly.
- As the fleet, when a robot dies mid-task, its task returns to the pool and gets reassigned within seconds.
- As the fleet, when an aftershock changes the map, in-flight plans that are now invalid get replanned against current shared beliefs.
- As an incident commander, I can ask in plain English which victims are unreached and why, and get an answer computed from live fleet memory. (P0 for demo, read-only.)
- As the fleet, I keep coordinating without interruption when a database node is killed. (P0 — this is the thesis.)
- As a future mission, I can retrieve semantically similar situations from past missions and adapt. (P1 stretch.)

### 3.3 Demo scenario spec: "Aftershock"

**Map.** 40×30 tile grid, four zones: **Staging** (base + charging, top-left), **Street** (open, fast travel), **Collapsed residential block** (dense debris, most victims), **Office building** (multi-room interior, requires door tiles), plus a **courtyard** connecting them. Tile types: open, debris (blocks ground robots; cleared by lifter), rubble-heavy (2× clear time), fire (spreads; blocks everyone; extinguisher = P2 stretch role), unstable (half speed, scouts only until shored — P1), wall, door.

**Robot stat blocks (v0 — tune in playtesting):**

| Role | Count | Speed (tiles/tick) | Vision radius | Battery (ticks) | Abilities |
|---|---|---|---|---|---|
| Scout drone | 2 | 3 | 6 | 120 (recharge at base) | Flies over debris; cannot interact; senses victims/hazards |
| Lifter | 1 | 1 | 2 | 300 | Clears debris (3 ticks/tile; 6 for rubble-heavy) |
| Medic courier | 1 | 2 | 3 | 200 | Carries 2 supply kits; stabilize = 2 ticks adjacent to victim; restock at base |
| Relay (P1) | 1 | 2 | 3 | 250 | Extends uplink zone (see §5, comms-constrained sync) |

**Victims.** 8 total. Each has position (hidden until sensed), vitals countdown (400–700 ticks, visible once found), and a state machine: `unknown → located → access_blocked? → reachable → stabilized | lost`. Mix: 3 directly reachable (fast wins for demo pacing), 4 behind one debris wall, 1 behind two (forces a scout→lifter→lifter→medic chain).

**Dynamics.** Fire spreads to an adjacent flammable tile every 25 ticks. **Aftershock at tick 300:** re-blocks two previously cleared corridors, reveals 1 new victim, converts one street segment to unstable. This forces visible replanning mid-demo.

**Win/loss.** Mission ends when all victims are stabilized/lost or at tick 1200. Score = victims stabilized, median time-to-stabilize, and the §5.6 metrics.

**Baseline mode.** Identical map and robots, but shared memory is disabled: each robot keeps a private world model, picks its own tasks greedily, no claiming, no handoff triggers. This is the "coordination OFF" run — expect duplicated exploration, a double-teamed victim, and at least one victim lost to the clock. The side-by-side delta is the product.

### 3.4 Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-1 | Robots write observations, claims, and status exclusively through the `fleetmem` SDK; no robot-to-robot channels exist. | P0 |
| FR-2 | Task claiming is transactional: concurrent claims on one task yield exactly one winner. | P0 |
| FR-3 | Completing a task auto-unblocks dependents (`depends_on` gating); dependents become visible to eligible robots within 1 tick (poll) / near-instantly (changefeed, P1). | P0 |
| FR-4 | New observations pass a reconcile-before-broadcast gate: vector + spatial match against existing beliefs; merge or insert atomically. | P0 |
| FR-5 | Missed heartbeats (>10s) release the robot's claimed tasks back to `open`. | P0 |
| FR-6 | Orchestrator assigns open tasks by scored policy (role match, distance, battery, priority). | P0 |
| FR-7 | Aftershock event invalidates affected path/claim state and triggers replans. | P0 |
| FR-8 | Fog-of-war UI: per-robot vision, with the shared known-world overlay filling in for all as any robot explores. | P0 |
| FR-9 | Live scoreboard + coordination ON/OFF toggle and side-by-side metrics. | P0 |
| FR-10 | Commander console: natural-language questions answered from live memory via CockroachDB managed MCP Server, read-only credentials. | P0 |
| FR-11 | Node-kill resilience: killing 1 of 3 CRDB nodes mid-mission causes zero task loss and no fleet stall. | P0 |
| FR-12 | Mission event log supports full replay of a run. | P1 |
| FR-13 | Cross-mission memory: post-run summaries embedded; new missions retrieve top-k similar past situations at planning time. | P1 |
| FR-14 | Comms-constrained sync: robots only read/write shared memory inside uplink zones (base + relay radius), buffering otherwise. | P1 |
| FR-15 | Isometric visual upgrade. | P2 |

### 3.5 Non-functional requirements

- **Tick rate:** 4 Hz authoritative server tick; sim must sustain 6 robots + dynamics at <150ms/tick compute.
- **Memory latency:** `fleetmem` read/write p95 < 60ms from agents (same-region CRDB Cloud).
- **Planning latency:** Bedrock plan calls are async — a robot continues its current action while a plan is in flight; p95 plan turnaround < 3s; hard cap 4 plan calls/robot/minute with rule-based fallback.
- **Cost ceiling:** < $40 total Bedrock spend for the hackathon (Haiku for planning, Titan V2 512-dim for embeddings; both are pennies per mission at our call caps).
- **Demo reliability:** seeded RNG for deterministic recording runs; the public demo auto-restarts missions; every video beat has a pre-recorded backup take.
- **Security posture (judge-visible):** per-robot service credentials; commander MCP access read-only; least-privilege by construction.

### 3.6 Visual direction — the Simile/Smallville look

North star: the aesthetic of Stanford's "Smallville" generative-agents town — the research lineage behind Simile — applied to a disaster block. Cozy top-down pixel world, characters you instantly root for, readable at a glance in a 3-minute video.

- **World:** 32px pixel-art tiles, top-down. Warm, slightly desaturated base palette with a small set of accent hues reserved for meaning: hazard orange-red, victim amber, role colors. Soft and cohesive over high-saturation — the map should feel like one place, not a rainbow.
- **Robots:** chunky, readable silhouettes per role — hovering scout drone with a soft shadow, tracked lifter, wheeled medic cart. Name tag + small role icon above each.
- **Thought bubbles (the Smallville signature):** every robot shows a live status bubble — 🔍 "scanning sector C", 🧱 "clearing debris", 📦 "kit en route" — and clicking a robot expands its latest Bedrock plan rationale. This is §4.3's "rationale surfacing" given its visual form; it's also how judges *see* agents thinking.
- **Coordination made visible:** an event ticker ("S1 found victim → task created → L1 claimed") plus brief path-ghost lines when a task is claimed. The shared map filling in through fog of war stays the hero visual.
- **Life details, cheap:** 2–4 frame walk/hover cycles, 3-frame fire loop, gentle pulse on located victims, screen shake on the aftershock. No lighting engine, no particles beyond fire.
- **Assets:** CC0 packs only (Kenney.nl and CC0 pixel tilesets on itch.io), or hand-drawn. Do not lift art from the Smallville repo, The Sims, or any licensed pack — the submission video must contain no third-party copyrighted material (§6.1). Lane 5 keeps an `ASSETS.md` crediting every source.

Style test for every screen: would it look at home in a generative-agents demo video? If it reads as "engineering debug view," it fails.

---

## 4. Architecture

### 4.1 Component view

```
 Browser ── PixiJS renderer · fog-of-war · scoreboard · ON/OFF toggle
    ▲ websocket (state frames, 4 Hz)
    │
 Sim server (Python 3.12 / FastAPI) — authoritative world
    · tick loop: apply actions → world dynamics → broadcast
    · validates all robot actions
    ▲ actions / local percepts (in-process queues)
    │
 Robot agents — one asyncio task per robot
    sense → sync → think → act → report
       │            │
       │ Bedrock    └── fleetmem SDK ── CockroachDB Cloud (3 nodes)
       │ (Claude Haiku plans,               robots · tasks · observations(VECTOR)
       │  Titan V2 embeddings)              victims · hazards · events
       │                                        ▲              ▲
 Orchestrator (async service) ──────────────────┘              │
    · allocation · dependency unblocking · reassignment        │
 Commander console ── Claude + CRDB managed MCP Server (read-only)
 Chaos rig ── kills/restores a CRDB node on cue
```

### 4.2 One rescue chain, end to end

1. Scout S1's vision covers tile (14,9); the sim hands it a percept: heat signature.
2. S1 forms belief "victim, (14,9), behind debris"; embeds the description (Titan V2).
3. **Reconcile gate:** `fleetmem.report_observation()` runs one transaction — vector top-k within 5 tiles; a match ≥0.82 cosine merges (bump confidence, add sighting); otherwise insert belief, create victim row, and create the task chain `clear_debris(14,8) → deliver_kit(14,9)` with `depends_on` linking them.
4. Orchestrator sees `clear_debris` open; scores eligible robots; Lifter L1 wins; `claim_task` flips it `open→claimed` transactionally.
5. L1 paths (A*) using shared hazard beliefs, clears for 3 ticks, calls `complete_task`.
6. Completion trigger: `deliver_kit` unblocks (poll at MVP; changefeed at P1). Medic M1 claims, delivers, victim `stabilized`. Every transition lands in `events`.
7. If L1's heartbeat lapses mid-clear, the claim releases and the next lifter (or replan) takes over. No human touched anything.

### 4.3 Agent design

Loop (runs every tick unless noted):

```python
async def agent_loop(robot):
    while mission.active:
        percepts = sense()                      # from sim, local vision only
        beliefs  = fleetmem.get_beliefs(area)   # shared world, cached 1s
        if needs_plan(robot, beliefs):          # idle, task done, world changed
            plan = await bedrock_plan(robot, beliefs)   # async, rate-capped
        action = next_action(plan)              # A* movement, rule-based acts
        submit(action)
        fleetmem.report(percepts_delta, status) # via reconcile gate
        fleetmem.heartbeat()
```

- **LLM discipline:** Bedrock (Claude Haiku) is called only on plan boundaries — task selection, replan-on-aftershock, conflict resolution — never per tick. Prompt = role card + current beliefs digest (≤1.5k tokens) + open tasks; output = strict JSON `{task_id | explore(sector) | return_to_base, rationale}`. Rationale strings surface in the UI — judges see the fleet thinking.
- **Role behaviors:** scouts run frontier-exploration bias (prefer unexplored sectors weighted by victim priors); lifters idle-stage near the densest blocked-victim cluster; medics pre-position between base and reachable victims. Each is ~50 lines of rules; the LLM chooses *among* behaviors, rules execute them.
- **Determinism:** with `--seeded`, LLM calls are recorded/replayed so demo runs are reproducible.

### 4.4 Coordination mechanics

- **Task lifecycle:** `blocked → open → claimed → in_progress → done | failed(→open)`. `blocked` tasks hold unmet `depends_on`; completion of the last dependency flips them `open` in the same transaction.
- **Claiming (the judge-friendly line of SQL):**

```sql
UPDATE tasks SET status='claimed', claimed_by=$robot, claimed_at=now()
WHERE id=$task AND status='open'
RETURNING id;   -- serializable isolation: exactly one winner, always
```

- **Allocation score:** `2.0·role_match + 1.2·priority + 1.0·(1/(1+dist)) + 0.5·battery_norm`, greedy per open task. (Auction/CBBA-style allocation from the multi-robot task-allocation literature is a P2 talking point, not a build item.)
- **Reassignment:** orchestrator scans heartbeats every 2s; >10s stale ⇒ release claims, mark robot `lost`, log event. Robot attrition — SubT's #2 field problem — becomes a 20-line query.
- **Reconcile-before-broadcast:** the gate in §4.2 step 3. Prior-work note for the writeup: per-agent self-reflection (Reflexion-style) doesn't catch cross-agent conflicts; gating writes against shared state does. Novel, cheap, and demoable (show the merged-duplicate event in the log).
- **Handoff triggers:** MVP polls open tasks at 1 Hz. P1 swaps in a CRDB changefeed on `tasks` → orchestrator/agents wake instantly. Same contract, faster push.

### 4.5 Schema DDL (v0 — lane 1 validates against CRDB docs on day 1)

```sql
CREATE TABLE robots (
  id STRING PRIMARY KEY, role STRING NOT NULL,
  pos_x INT, pos_y INT, battery INT, status STRING,
  current_task UUID, heartbeat_at TIMESTAMPTZ
);

CREATE TABLE tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id UUID NOT NULL, kind STRING NOT NULL,
  target_x INT, target_y INT, priority INT DEFAULT 1,
  status STRING NOT NULL DEFAULT 'blocked',
  depends_on UUID[],           -- unblock when all done
  claimed_by STRING, claimed_at TIMESTAMPTZ, done_at TIMESTAMPTZ,
  INDEX (mission_id, status)
);

CREATE TABLE observations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id UUID, robot_id STRING, kind STRING,
  pos_x INT, pos_y INT, payload JSONB,
  embedding VECTOR(512),       -- Titan V2 @ 512 dims
  confidence FLOAT, sightings INT DEFAULT 1,
  observed_at TIMESTAMPTZ DEFAULT now()
);
CREATE VECTOR INDEX obs_embedding_idx ON observations (embedding);

CREATE TABLE victims (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id UUID, pos_x INT, pos_y INT,
  state STRING NOT NULL DEFAULT 'located',
  vitals_deadline INT, reported_by STRING, confidence FLOAT
);

CREATE TABLE hazards (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id UUID, kind STRING, area JSONB, severity INT, active BOOL
);

CREATE TABLE events (          -- append-only mission log; powers replay
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mission_id UUID, at TIMESTAMPTZ DEFAULT now(),
  actor STRING, verb STRING, detail JSONB
);

CREATE TABLE mission_memories ( -- P1 cross-mission learning
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  summary STRING, embedding VECTOR(512), outcome JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

### 4.6 Hackathon tooling map (summary — full deep dive in §6)

- **CRDB tools (≥2 required):** ✅ Distributed Vector Indexing — reconcile gate + cross-mission recall. ✅ Managed MCP Server — commander console, read-only mode. ➕ Agent Skills Repo — run the resilience/production-readiness skills against our cluster and ship the outputs (§6.2). ➕ ccloud CLI — optional fourth.
- **AWS (≥1 required):** ✅ Bedrock — Claude (Haiku-class) planning + Titan V2 embeddings. ✅ S3 + CloudFront — frontend + replays. ✅ ECS Fargate — sim server + agents. ✅ Lambda — post-mission memory summarizer.
- **Chaos rig:** see §6.5 — the node-kill segment runs on a self-hosted 3-node cluster (the Cloud free tier doesn't expose node control); primary fleet memory stays on CockroachDB Cloud.

### 4.7 Metrics (computed from `events`, shown on scoreboard)

- **Rescue rate** = stabilized / total victims.
- **Median time-to-stabilize** (ticks from mission start).
- **Duplicate-effort index** = redundant tile visits / total visits (visits to tiles already explored by another robot), plus count of same-target double-work incidents.
- **Coverage@500** = explored reachable tiles / all reachable tiles at tick 500.
- **Coordination gain** = (baseline median time − coordinated median time) / baseline. One number the video ends on.

### 4.8 Sim engine build spec (lane 3's blueprint)

How to actually build the Smallville-style engine described in §3.6 — server-authoritative, client-pretty:

- **Split:** the server simulates at 4 Hz and owns truth; the client renders at 60 fps. The client **interpolates** — tween each entity from its previous tick position to its new one over the 250ms window (simple lerp is fine). This one technique is what makes a 4 Hz sim look like The Sims instead of a chess clock. Never render raw tick jumps.
- **World format:** `map.json` — `{width: 40, height: 30, tile_size: 32, layers: {ground[], objects[]}, zones[], spawn_points{}, victims[], escalations[]}`. Lane 5 authors it; lane 3 loads it; the server treats it as the initial world state.
- **Tick pipeline (server, in order):** ingest queued robot actions → validate against world rules → apply movement/work → run dynamics (fire spread, vitals countdown, scheduled aftershock) → derive per-robot percepts (vision radius) → append state frame to the websocket broadcast. Deterministic given a seed: same seed + same action log = same mission, which is what makes the golden demo run reproducible.
- **State frame (websocket, per tick):** `{tick, robots:[{id, x, y, facing, status, bubble}], victims:[…], tiles_changed:[…], events:[…], metrics:{…}}` — send diffs (`tiles_changed`), not the whole grid, after the initial full snapshot.
- **Render layers (PixiJS, bottom → top):** 1 ground tilemap · 2 debris/hazards (animated fire) · 3 entities (victims, then robots) · 4 fog-of-war mask (alpha overlay; explored-but-stale tiles dimmed, unexplored dark) · 5 floaters (name tags, role icons, thought bubbles, path ghosts) · 6 HUD (scoreboard, ON/OFF toggle, event ticker). Bubbles and tags live in a separate container so they never sort-fight with sprites.
- **Sprites:** one atlas per category (tiles, robots, effects), plain grid spritesheets — no packer tooling needed at this scale. 2–4 frame cycles driven by elapsed time, not tick count, so animation stays smooth between ticks.
- **Camera:** none. 40×30 at 32px = 1280×960; render the whole map, letterbox to fit. Optional ×2 zoom-on-click as polish, not plumbing.
- **Fog of war:** client keeps an `explored` bitset per mode — in coordinated mode it's the *shared* explored set (any robot's vision reveals for all, straight from shared memory); in baseline mode each robot's private set, with the viewer seeing the union dimmed. The visual difference between the two modes is itself the demo.
- **Build order for lane 3:** static map render → one moving sprite with lerp → websocket live feed → fog of war → bubbles/ticker → scoreboard → juice (shake, pulses). Ship each step; don't gold-plate step 1.

---

## 5. Delivery plan

### 5.1 Lanes and subtask checklists (5 people)

**Lane 1 — Memory & data layer · Praneeth (starting today)**
- [ ] CRDB Cloud cluster (3 nodes) + local `cockroach demo` dev recipe
- [ ] Schema v0 → validated v1 (incl. `VECTOR` + vector index syntax check)
- [ ] `fleetmem` Python SDK: `report_observation, claim_task, complete_task, get_beliefs, heartbeat, log_event`
- [ ] Claiming txn + concurrency test (two fake robots, 1,000 races, zero double-claims)
- [ ] Reconcile gate (embed → search → merge/insert txn) + unit tests
- [ ] Changefeed spike (P1) · node-kill chaos script · per-robot credentials

**Lane 2 — Robot agents · TBD**
- [ ] Agent loop skeleton + sense/sync/think/act/report contract
- [ ] A* over shared belief map; battery/return-to-base logic
- [ ] Role behavior modules: scout / lifter / medic (+ relay P1)
- [ ] Bedrock planning: prompt cards, strict-JSON parsing, rate caps, recorded-replay mode
- [ ] Rationale surfacing to UI · rule-based fallback path

**Lane 3 — Sim world & rendering · TBD**
- [ ] Tick server: world state, action validation, dynamics (fire, vitals, aftershock) — pipeline per §4.8
- [ ] Tile map loader (map.json authored by lane 5)
- [ ] PixiJS renderer: layered per §4.8, CC0 sprite atlases, client-side lerp between ticks
- [ ] Fog of war (shared vs baseline modes) + thought bubbles, name tags, event ticker (§3.6)
- [ ] Websocket state protocol (diff frames) + reconnect · scoreboard & ON/OFF toggle UI

**Lane 4 — Orchestration & missions · TBD**
- [ ] Task-graph definitions + `depends_on` unblocking
- [ ] Allocation scorer + reassignment-on-heartbeat-loss
- [ ] Baseline (coordination-OFF) mode
- [ ] Metrics pipeline from `events` → scoreboard
- [ ] Commander console: MCP Server hookup, read-only role, 5 canned demo questions

**Lane 5 — Scenario, demo & submission · TBD**
- [ ] "Aftershock" map JSON + art/sprite set + escalation script
- [ ] Playtest & tune stat blocks (twice: Aug 8, Aug 12)
- [ ] AWS deploy: S3/CloudFront frontend, ECS backend, public URL
- [ ] Video: script (§6 of v1), record beats + backups, edit to <3 min
- [ ] Repo hygiene: README, MIT license visible, setup instructions, architecture diagram
- [ ] Devpost writeup incl. tools-used section + CRDB feedback

Pairing note: lanes 2↔4 sync daily (agents consume orchestration). Lane 5 owns the deadline and holds scope veto from Aug 13.

### 5.2 Interface contracts — freeze Aug 3

1. `fleetmem` SDK signatures (above) — lane 1 publishes stubs day 1 so lanes 2/4 build against fakes.
2. Agent↔sim action API: `move(dir) | act(verb, target) | idle`; server validates.
3. Websocket state frame JSON (lane 3 publishes).
4. Task JSON + `depends_on` semantics (lane 4 publishes).
Changes after Aug 3 need a team ping, not a silent commit.

### 5.3 Day-by-day

| Date | Milestone |
|---|---|
| Aug 1 (today) | Repo + CI, cluster up, schema v0, SDK stubs published, map JSON format agreed |
| Aug 2–3 | **Walking skeleton:** one scout moves, writes a belief, renders in browser. Contracts frozen. |
| Aug 4–6 | Claiming + dependencies live; lifter & medic behaviors; fog-of-war |
| Aug 7–8 | **MVP:** full scout→lifter→medic chain on Aftershock v1 map; playtest #1 |
| Aug 9–10 | Reconcile gate on all writes; baseline mode; metrics on scoreboard |
| Aug 11–12 | Aftershock replanning; MCP console; changefeed handoffs; playtest #2 |
| Aug 13–14 | Node-kill rig + rehearsal; AWS deploy; public URL live |
| Aug 15 | **Feature freeze.** Bug bash, determinism pass, seed the golden demo run |
| Aug 16–17 | Video record/edit; README; Devpost writeup; diagram; CRDB feedback |
| Aug 18 | Submit by **noon EDT** — five hours of buffer, on purpose |

### 5.4 Testing & demo reliability

- Concurrency: claim-race test in CI (lane 1).
- Sim: golden-seed regression run nightly from Aug 8; diff the events log.
- Chaos: node-kill rehearsed ≥5 times before recording; fallback = pre-recorded take.
- LLM: recorded-replay mode for demos; live mode for the deployed URL with rule fallback.

### 5.5 Risks (delta from v1)

| Risk | Mitigation |
|---|---|
| ~~CRDB `VECTOR`/index syntax differs from draft DDL~~ | **Resolved Aug 1** — syntax validated against v26.2 docs (§6.3); cosine `<=>` confirmed for the reconcile gate |
| Bedrock latency spikes wreck pacing | Async planning, rate caps, rule fallback, recorded-replay for the video |
| Five-way integration hell | Contracts frozen Aug 3; SDK stubs + fakes from day 1; walking skeleton before features |
| Sim balance makes coordination look weak | Two scheduled playtests; map designed so ≥2 victims are unreachable without handoffs |
| Baseline mode is accidentally too dumb (judges smell a strawman) | Baseline robots keep full individual autonomy + greedy search — only *sharing* is removed; state this explicitly in the video |
| Scope creep | P0/P1/P2 labels above; lane 5 veto from Aug 13 |

---

## 6. Hackathon deep dive & tooling decisions

Everything below is from the official Devpost rules/resources pages (fetched Aug 1) and the CockroachDB v26.2 docs. Links in §7.

### 6.1 Rules that change how we operate

| Rule | What it means for us |
|---|---|
| Teams of **up to 5** individuals | We're exactly at the cap. Lock the roster now — nobody else can be added later. Appoint one **Representative** (suggest: Praneeth) who registers the team, submits, and receives/distributes any prize. Everyone joins via Devpost and agrees to Devpost ToS + AWS Event Terms. |
| Submission window: Jun 30 – **Aug 18, 5pm ET**. Judging: **Aug 19 – Sep 15**. Winners ~Sep 21. | The demo URL and repo must stay live, working, and free-to-access through **Sept 15**, not just Aug 18. Budget the deployed stack to idle cheaply for a month (see §6.4). |
| **New projects only**, built during the submission period; standard frameworks + AI coding assistants explicitly allowed; any other pre-existing code must be disclosed | Fresh repo, first commit dated in-window. We disclose libraries and AI-assisted development in the README. Don't import old project code silently. |
| Stage One is **pass/fail**: fits theme + reasonably applies the required tools. Stage Two: five equally weighted criteria | Passing Stage One is table stakes — the writeup must make the two CRDB tools and AWS usage unmissable in the first paragraph. |
| **Tie-breaks follow criteria order**, starting with Agentic Memory Design | If we're tied with anyone, memory design wins the tie. It's already our strongest axis; over-invest there deliberately. |
| Judges **may judge from the video + description alone** and are not required to test | The 3-minute video and text description carry most of the weight. Treat lane 5's deliverables as first-class engineering, not garnish. |
| Video: <3 min, must show the project functioning **and the CockroachDB memory layer at work**, public on YouTube/Vimeo, **no third-party trademarks or copyrighted music** | Show live SQL/table views of tasks flipping states during the rescue — that's "memory layer at work," literally. Royalty-free or no music. Watch stray logos in screen recordings. |
| Repo must be public with an OSI license **visible in the About section** | MIT, added day 1, pinned in repo About — not just a LICENSE file buried in the tree. |
| "All required CockroachDB and AWS components must be **meaningfully integrated — not just initialized**" | Their words. Our writeup answers "what did the agent actually do with each tool" per tool, one paragraph each (§6.2, §6.4 give the answers). |

### 6.2 CockroachDB tooling — tool-by-tool integration plan

| Tool | What it actually is (per docs) | How Colony uses it | Status |
|---|---|---|---|
| **Managed MCP Server** | Managed endpoint (`cockroachlabs.cloud/mcp`); config snippet copied from Cloud Console into Claude Code/Cursor/VS Code. Tools: list databases/tables, describe schemas & indexes, inspect cluster health and running queries, run read-only SQL + `EXPLAIN`; writes only when explicitly enabled. | **Runtime:** the commander console — a human asks natural-language questions ("which victims are unreached and why?") and the AI answers by querying live fleet memory, read-only. **Dev-time:** every teammate wires the MCP config into Claude Code/Cursor for schema inspection while building. | Required tool #1 ✅ |
| **Distributed Vector Indexing** | Native `VECTOR` column type; `CREATE VECTOR INDEX`; similarity operators `<->` (L2), `<#>` (inner product), `<=>` (cosine); vectors, JSONB, and relational data in the same table, same transaction, serializable by default. | The reconcile-before-broadcast gate (cosine `<=>` search over `observations.embedding` inside the insert transaction) and `mission_memories` cross-mission recall. One system for beliefs + tasks + vectors = the "no consistency gap" story from their own docs, demonstrated. | Required tool #2 ✅ |
| **Agent Skills Repo** | Open-source repo (`cockroachlabs/cockroachdb-skills`) of machine-executable skills per the agentskills.io spec — including domains for resilience & disaster recovery, observability, security/governance, and specific skills like validating production readiness and auditing user privileges. | Two uses. (1) Dev: schema/query design skills during lane 1's build. (2) **Judge-visible:** run the production-readiness, privilege-audit, and backup/DR-posture skills against our cluster before submission and commit the outputs to `/ops-audit` in the repo. A disaster-relief fleet that audited its own disaster-recovery posture with the sponsor's skills repo — that paragraph writes itself. | Strong tool #3 ✅ |
| **ccloud CLI** | Agent-ready CLI for the Cloud control plane: create clusters, manage IP allowlists, SQL users, connection info; JSON output; service-account RBAC. | Cluster provisioning + per-robot SQL user creation scripted via ccloud in `infra/` (reproducible setup, shown in README). Optional — nice fourth tool, zero extra architecture. | Optional #4 |
| Docs MCP server, Claude Code plugin, LangChain integrations | Docs-only MCP endpoint; editor plugins; LangChain provider/vector store/chat history. | Dev accelerators only. LangChain's CRDB vector store is a fallback if lane 1's raw-SQL vector path hits friction; core stays raw SQL for transactional control. | Dev aids |

### 6.3 Validated technical facts (so lane 1 doesn't rediscover them)

- Schema DDL in §4.5 is consistent with v26.2 docs: `VECTOR` type + `CREATE VECTOR INDEX` are the documented syntax. Use **cosine (`<=>`)** for the reconcile gate.
- The docs' own showcase pattern is exactly ours: vector search combined with relational filters in one query, one transaction — e.g. `ORDER BY embedding <=> $query LIMIT 5` with `WHERE` filters. Reuse that shape in the gate.
- Serializable isolation is the default — the claiming `UPDATE … WHERE status='open'` needs no extra locking ceremony.
- CockroachDB Cloud free tier: no credit card, hackathon-eligible per the FAQ. Free-tier limits are ours to watch; embeddings at 512-dim × a few thousand observations is nothing.
- MCP server is read-only by default; write tools are opt-in. Leave writes off — it *is* our access-control story.

### 6.4 AWS services — decisions and rationale

| Service | Role in Colony | Why this one |
|---|---|---|
| **Amazon Bedrock** (core) | Robot planning calls (Claude Haiku-class — pick the current fast/cheap Claude model listed in the region; enable model access in the Bedrock console on day 1, it's not instant) + **Titan Text Embeddings V2 at 512 dims** for observations and mission memories. | The listed AWS service that touches the agent's brain directly; pay-per-token fits our capped call budget; keeps the whole AI path on AWS as the hackathon intends. |
| **ECS Fargate** (core) | Sim server + agent processes as one container service. | The sim is a long-lived 4 Hz tick loop with persistent websockets — Lambda's execution model is wrong for that. Fargate = containers without cluster management. One small task (0.5 vCPU) idles cheaply through the Sept 15 judging window. |
| **S3 + CloudFront** (core) | Static frontend hosting; mission replay JSON artifacts. | Free-tier friendly, zero maintenance, survives judging month at ~$0. |
| **AWS Lambda** (supporting) | Post-mission summarizer: mission ends → Lambda embeds the run summary via Bedrock → writes `mission_memories`. | A genuinely event-shaped job — the one place serverless is the right tool, and it makes "cross-mission learning" a named AWS integration rather than a cron job. |
| SageMaker — **not used** | — | No custom training/inference; using it would be padding, and judges reward meaningful integration, not service count. |
| Bedrock Agents — **deliberately not used** | — | Our thesis is that coordination lives in *durable shared memory*, not in a hosted orchestration runtime. Outsourcing the task graph to Bedrock Agents would blur the CockroachDB-as-brain story. Say this in the writeup — it preempts the "why didn't you just use Bedrock Agents?" question and shows insight into agentic-system design (the Creativity criterion's own wording). |

Note on credits: the hackathon provides **AWS Free Tier only** — no special credit grant. Total projected spend at our caps: single-digit dollars for Bedrock, low tens for a month of Fargate idle. Lane 5 sets a billing alarm day 1.

### 6.5 Resilience demo — corrected mechanics

The Cloud free tier doesn't hand you nodes to kill. So the plan is two clusters, one schema:

1. **Primary fleet memory:** CockroachDB Cloud (free tier) — powers the deployed demo, the MCP console, and the vector work. This is what judges touch.
2. **Chaos segment (video only):** the identical stack pointed at a **self-hosted 3-node CockroachDB cluster** (Docker Compose, or `cockroach demo --nodes 3` in rehearsal). Mid-rescue, kill node 2's container on camera; the fleet keeps claiming, completing, and handing off. Narrate honestly: "same software, self-hosted so we can murder a node live."

This is stronger than pretending: it shows we understand the deployment models, and the survive-a-node-loss behavior is a property of the database, not of the hosting tier. FR-11 and the §5.3 Aug 13–14 milestone now mean this. If ccloud/Cloud tiers turn out to allow a scale-down demo on a paid Advanced cluster, lane 1 may substitute — but don't spend money to prove what Docker proves free.

### 6.6 Day-1 setup checklist (do today, before code)

1. All five: create Devpost accounts, join the hackathon, form the team; Praneeth registered as Representative.
2. Praneeth: CockroachDB Cloud signup (free, no card) → create cluster → copy MCP config snippet into Claude Code/Cursor for everyone → create per-robot SQL users (via ccloud if adopting tool #4).
3. AWS account: enable **Bedrock model access** (Claude + Titan V2) in the target region immediately — approval isn't always instant. Set the billing alarm.
4. Repo: init public, MIT license visible in About, README skeleton with the tools-used section stubbed, first commit today (in-window timestamp).
5. Clone `cockroachlabs/cockroachdb-skills`; lane 1 skims the schema-design and resilience/DR skills before writing the migration.


---

## 7. Sources

Market sizing: MarketsandMarkets, AMR/AGV Fleet Management Software Market (Nov 2025) · Mordor Intelligence, Search and Rescue Robots Market (2025) · MRFR, SkyQuest, Research Nester SAR reports · SVB, Physical AI & the Future of Robotics Report (2026) · PitchBook 2025 robotics funding via Future Investments (May 2026).
Competitive: MiR Fleet product pages · CB Insights robot-fleet-management ESP (Formant, Geek+) · SYNAOS MRFM (VDA 5050) · Open-RMF docs + JobToRob milestone coverage (mutex groups) · Open Robotics Discourse, "SOTA for Multi Robot Cooperation in RMF" (Jul 2025) · AWS IoT RoboRunner preview/GA announcements (Nov 2021 / Nov 2022), @aws-sdk/client-iot-roborunner removal notice, AWS RoboMaker end-of-support notice (Sept 10, 2025).
Research: Team Explorer, "Modular, Resilient, and Scalable System Design… Lessons after DARPA SubT" (arXiv 2404.17759) · CTU-CRAS-NORLAB SubT field reports · Gupta et al., "RobotFleet" (arXiv 2510.10379, USC) · Cockroach Labs engineering blog on C-SPANN vector indexing and agent memory.


Hackathon & tooling: Devpost hackathon overview, resources, and official rules pages (cockroachdb-ai.devpost.com, fetched Aug 1 2026) · CockroachDB docs v26.2: "CockroachDB and AI" (VECTOR type, CREATE VECTOR INDEX, similarity operators, MCP server tooling, Agent Skills, ccloud) · cockroachlabs/cockroachdb-skills (GitHub) · Cloud MCP quickstart + ccloud get-started docs (cockroachlabs.com/docs/cockroachcloud).

## 8. Open questions for the team

1. Name: keep "Colony," or vote on alternatives before the repo goes public (repo rename later is annoying).
2. Relay/uplink-zone mechanic (FR-14): great realism + SubT tie-in, but it's the riskiest P1. In or out by Aug 9?
3. Live LLM in the deployed demo, or recorded-replay only with live mode behind a flag?
4. Who takes which TBD lane? Decide today — lane 3 needs the strongest frontend hand, lane 4 the strongest distributed-systems hand.
