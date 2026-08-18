# Video script — 2:50, shootable as written

Rules this is built against (§6.1): **under 3 minutes**, must show the project
working **and the CockroachDB memory layer at work**, public on YouTube/Vimeo,
**no third-party trademarks and no copyrighted music**. Judges may score from
the video and description alone, so this carries more weight than any remaining
code.

Every number spoken here is measured and cited to where it was measured. Nothing
is rounded up, and nothing is claimed that an audit would not survive.

**Setup before recording**

```bash
cd colony && make dev          # cluster + schema, or the Cloud DSN
make demo                      # resets, seeds tactics, serves on :8000
```

Browser at 1920×1080, one tab. Do not open dev tools. Confirm the header reads
`Aftershock · 9 trapped · 6 units · 1200 ticks · seed 0` — the seed on screen is
what makes "run it again" answerable.

---

## 0:00–0:12 · Frame it

**On screen:** the map, mission just started, robots leaving spawn.

> "This is a magnitude 7.2 aftershock. Nine people are trapped. Six robots are
> going in, and they cannot see each other."

**Why this opening:** the constraint is the product. If the robots could all see
everything, none of what follows would be necessary.

## 0:12–0:35 · The memory, on screen

**On screen:** point at the **memory rail** as counts climb. Then the
**coordination feed**.

> "There is no radio between these robots. Every robot writes what it sees to
> one CockroachDB cluster, and reads what everyone else wrote. That's the whole
> coordination mechanism."
>
> *(point at the feed)*
>
> "So this is them talking. `m1` claimed a kit delivery at twelve-ten — because
> `s2` reported a victim there. Different robot, different sector. The medic
> never saw that person."

**Why:** this is the required "memory layer at work" beat, and it is literal —
those are rows, joined live, not a rendering of a message bus.

## 0:35–0:52 · Claude is deciding

**On screen:** the `claude` badges in the feed; click one robot for its panel.

> "The decisions are Claude Haiku on Bedrock, and the badge says which ones. The
> prompt is that robot's local slice of memory plus tactics the fleet learned in
> earlier missions — so the reasoning has sources, and they're stored."

**Backing:** `plans.chosen.source` distinguishes `bedrock` from `rules`, and 15
of 36 decisions in a full mission are Bedrock's. `plans.based_on` holds the
observation ids that were in the prompt. Both checkable in SQL.

## 0:52–1:15 · Off, then on

**On screen:** click **compare ON vs OFF**. Numbers land in ~2.5s.

> "Same map, same seed, coordination off: they rescue four of nine, and **five
> people die**. Coordination on: nine of nine, nobody dies."

**Then, immediately:**

> "That's one map, so we ran forty randomly generated disasters. Shared memory
> raises the rescue rate by thirty-one points — and that holds only when victims
> are trapped behind rubble and getting to them needs a handoff. Where every
> victim is reachable alone, it makes no difference. We measured that too."

**Backing:** X10, `audit/experiments.md` — +0.313, 95% CI [+0.234, +0.393],
n=40, paired. The scope condition is said out loud on purpose: it is the first
thing a careful judge would probe, and volunteering it is worth more than the
extra point it costs.

## 1:15–1:40 · Break it — the robot

**On screen:** click **kill a robot**. Fleet panel shows it `DOWN`, its task's
lease counting down.

> "Kill the robot holding a job. Nothing reassigns it. Its heartbeat stops, so
> its lease stops being renewed — and fifteen seconds later the row is claimable
> again and somebody else takes it."
>
> "Recovery here isn't a supervisor noticing. It's the absence of one."

**Backing:** FR-5. The claiming SQL's expiry predicate is the entire recovery
path — `claim_task` in `fleetmem/client.py`.

## 1:40–2:05 · Break it — the world

**On screen:** arm **fire**, click a tile on a claimed route. Watch the feed.

> "Now break the world. Drop fire across a route a lifter is already walking."
>
> *(feed updates)*
>
> "A scout sees it and writes it down. The lifter re-plans — and the feed marks
> the decisions that came after the fire, so you can watch the fleet notice."

**Honesty note for the narrator:** the marker says *after*, not *because of*.
Do not say "because" here. It is correlation and the UI says so.

## 2:05–2:25 · Break it — the database

**On screen:** cut to the terminal with the 3-node rig; kill a node on camera.

> "Three CockroachDB nodes. Kill one, mid-rescue."
>
> "Zero tasks lost. One task completed before it died, twelve after. The fleet
> never paused."

**Backing:** X5, re-run today against current code: **5/5 rehearsals survived**,
`audit/x5-node-kill.md`.

## 2:25–2:42 · Ask it a question

**On screen:** commander console, click **why did robot m1 do that**.

> "And because every decision stored its sources, you can ask. This is
> read-only SQL against live fleet memory — the query is shown next to the
> answer, so you can check it rather than trust it."

**Why show the SQL:** it is the difference between a console and a demo of a
console.

## 2:42–2:50 · Land it

**On screen:** back to the full dashboard, mission running.

> "Robots are already autonomous. They are not yet teammates. The database is
> what makes them a team — and it survives losing a robot, a route, and a
> machine."

---

## Shot list, in recording order

Record these separately and cut; do not attempt one take.

| # | shot | notes |
|---|---|---|
| 1 | cold start → first 60 ticks | needs `make demo` from clean |
| 2 | memory rail + coordination feed, close | zoom the browser to 150% for legibility |
| 3 | compare ON vs OFF | one click, ~2.5s |
| 4 | kill a robot → lease countdown → takeover | wait the full 15s; the pause is the point |
| 5 | arm fire → place on a claimed route | pick a route with a lifter on it |
| 6 | terminal: `cluster3.sh up`, `chaos.py --rehearsals 1` | full screen terminal, large font |
| 7 | console question with SQL visible | scroll so both answer and query are in frame |

## Do not

- **Do not** say "because of the fire" over the intervention beat. Say "after".
- **Do not** switch views mid-video. The twin is now `/` and is what the shot
  list assumes; `/2d` is the fallback to record on if WebGL misbehaves on the
  recording machine, and it carries every panel the twin does. Pick one before
  you start rolling — switching costs continuity.
- **Do not** play music with a licence you cannot name. Silence with clear
  narration scores better than a takedown.
- **Do not** show the AWS console, a DSN, or `~/.aws` on camera.
- **Do not** claim the console is an AI answering questions. It is six audited
  queries, and §6.2 now says so.

## The one-paragraph description (for Devpost and the video)

> Colony is a shared-memory coordination layer on CockroachDB that turns a
> heterogeneous robot fleet into a team. Robots have no channel to each other:
> they write beliefs, tasks and decisions to one cluster and read what everyone
> else wrote, so coordination is a property of the data model rather than of a
> message bus. Task ownership is a lease, so a dead robot's work frees itself
> with nobody on the recovery path. Claude on Amazon Bedrock decides at plan
> boundaries and every decision stores the memory rows that caused it, which is
> what lets a commander ask "why did that robot do that" and get a join instead
> of a story. Across 40 randomly generated disasters, shared memory raises the
> rescue rate by 31 points (95% CI 23–39) and cuts mean victims lost from 2.4 to
> 0.95 — an effect that requires scenarios where reaching a victim needs a
> handoff. Killing one of three database nodes mid-mission costs zero tasks and
> no stall, rehearsed 5/5.
