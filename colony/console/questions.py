"""The seven canned demo questions (§5.1 lane 4, FR-10, §6.2).

Canned rather than free-form on purpose. §5.4 puts demo reliability above
cleverness — a live model improvising SQL in front of judges is the one part of
the console that can fail in a way nobody can recover from on camera. These six
are the questions the demo actually asks, they are the ones the MCP server would
end up issuing, and each is a plain read anyone can run by hand.

They are chosen so that between them they interrogate all four memory systems
(§4.0), because that mapping is judging criterion #1:

    why_did_robot        PROVENANCE   plans x observations through based_on
    unreached_victims    WORKING      victims x the task graph blocking them
    what_do_we_know      EPISODIC     observations, with the merge count visible
    beliefs_30s_ago      EPISODIC     the same table, read 30s in the past
    who_holds_what       WORKING      tasks x robots, and the lease on each
    aftershock_response  PROVENANCE   what the fleet re-decided, and when
    what_did_we_learn    SEMANTIC     mission_memories, the tactics learned

`beliefs_30s_ago` is the only one that reads the past. CockroachDB retains
historical versions, so `AS OF SYSTEM TIME` asks the same rows what they held
earlier — the fleet's memory is time-travellable without a snapshot table, an
audit copy, or an event-sourcing replay. Asked next to `what_do_we_know`, the
pair shows what the fleet learned in the last half minute.

The last one carries no scope at all — not a mission, not a map. A tactic
learned clearing rubble on one map is meant to apply on the next, so scoping it
would defeat the point of having it.

"Why did robot X do Y" is the one §5.1 names explicitly, and it is the reason
`plans.based_on` exists: it answers with the rows that were in the prompt, not
with a plausible story about them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class Question:
    """One canned question: what it asks, the SQL it runs, how it reads back."""

    id: str
    prompt: str
    # What this question demonstrates, for the writeup and the console UI.
    memory: str
    sql: str
    # Parameter names in order, filled from the request. `mission_id` is always
    # first and always supplied by the server, never by the caller.
    params: tuple[str, ...] = ("mission_id",)
    render: Callable[[list[dict[str, Any]]], str] = field(
        default=lambda rows: f"{len(rows)} rows"
    )


@dataclass(frozen=True)
class Answer:
    question_id: str
    prompt: str
    memory: str
    sql: str
    rows: list[dict[str, Any]]
    summary: str


# --- PROVENANCE: the question §5.1 names ------------------------------------

WHY_DID_ROBOT = """
SELECT p.at,
       p.trigger,
       p.rationale,
       p.chosen->>'source' AS decided_by,
       p.chosen,
       coalesce(
         (SELECT array_agg(
                   o.kind || ' at ' || o.pos_x || ',' || o.pos_y ||
                   ' (' || o.sightings || ' sighting(s), confidence ' ||
                   round(o.confidence::numeric, 2) || ')')
            FROM observations o
           WHERE o.id = ANY(p.based_on)),
         ARRAY[]::STRING[]
       ) AS based_on
  FROM plans p
 WHERE p.mission_id = %s AND p.robot_id = %s
 ORDER BY p.at DESC
 LIMIT %s
"""


def _render_why(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "that robot has not logged a decision yet"
    latest = rows[0]
    sources = latest.get("based_on") or []
    because = (
        f" based on {len(sources)} memory row(s): " + "; ".join(sources)
        if sources
        else " with no shared beliefs in its digest"
    )
    return (
        f"most recently, on {latest['trigger']}, it decided: "
        f"{latest['rationale']} (decided by {latest['decided_by'] or 'rules'})"
        f"{because}"
    )


# --- WORKING: who is still waiting, and what is in the way ------------------

# Follows the dependency chain rather than matching on position, because the
# `clear_debris` tasks target the *blocking* tiles and not the victim's own
# (§4.2 step 3). Asking "what tasks sit on this victim's tile" finds the
# delivery and misses the rubble in front of it — which is the only part of the
# answer a commander cannot already see on the map.
UNREACHED_VICTIMS = """
WITH deliveries AS (
  SELECT t.id, t.target_x, t.target_y, t.depends_on
    FROM tasks t
   WHERE t.mission_id = %s AND t.kind = 'deliver_kit'
),
chain AS (
  SELECT d.target_x, d.target_y, t.kind, t.status, t.claimed_by
    FROM deliveries d JOIN tasks t ON t.id = d.id
   WHERE t.status != 'done'
  UNION ALL
  SELECT d.target_x, d.target_y, t.kind, t.status, t.claimed_by
    FROM deliveries d JOIN tasks t ON t.id = ANY(d.depends_on)
   WHERE t.status != 'done'
)
SELECT v.id,
       v.pos_x, v.pos_y, v.state, v.vitals_deadline, v.reported_by,
       coalesce(
         (SELECT array_agg(c.kind || ' [' || c.status || ']' ||
                           coalesce(' held by ' || c.claimed_by, ''))
            FROM chain c
           WHERE c.target_x = v.pos_x AND c.target_y = v.pos_y),
         ARRAY[]::STRING[]
       ) AS blocking_tasks
  FROM victims v
 WHERE v.mission_id = %s AND v.state != 'stabilized'
 ORDER BY v.vitals_deadline
"""


def _render_unreached(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "every victim the fleet has found is stabilized"
    waiting = ", ".join(
        f"({r['pos_x']},{r['pos_y']}) {r['state']}"
        + (
            f" — waiting on {', '.join(r['blocking_tasks'])}"
            if r["blocking_tasks"]
            else " — no task yet"
        )
        for r in rows[:5]
    )
    return f"{len(rows)} victim(s) not yet stabilized: {waiting}"


# --- EPISODIC, through time: what the fleet believed a moment ago -----------

# The one question that reads the past rather than the present. CockroachDB
# keeps historical versions, so `AS OF SYSTEM TIME` asks the same table what it
# held at an earlier timestamp — no snapshot table, no audit copy, no
# event-sourcing replay. Memory is time-travellable because the database is.
#
# The interval is a literal on purpose, and it has to be: v26.2 accepts only a
# constant expression there ("only constant expressions, with_min_timestamp,
# with_max_staleness, or follower_read_timestamp"), so a placeholder is a
# syntax error rather than a choice. That suits a console which is canned for
# demo reliability anyway — it also means there is no string interpolation here
# and so no injection surface.
#
# AS OF SYSTEM TIME binds the whole statement, so a subquery cannot read `now`
# alongside it. The contrast is drawn by asking this and `what_do_we_know` back
# to back, not by one cleverer query.
BELIEFS_30S_AGO = """
SELECT o.kind,
       o.pos_x, o.pos_y,
       o.sightings,
       round(o.confidence::numeric, 2) AS confidence,
       o.robot_id AS first_reported_by
  FROM observations o
  AS OF SYSTEM TIME '-30s'
 WHERE o.mission_id = %s
 ORDER BY o.sightings DESC, o.kind, o.pos_x, o.pos_y
 LIMIT 20
"""


def _render_recent_past(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (
            "thirty seconds ago the fleet had recorded nothing for this mission "
            "— either it had just started, or nobody had reported yet"
        )
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    tally = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    lead = ", ".join(
        f"{r['kind']} at {r['pos_x']},{r['pos_y']} ({r['sightings']}x)"
        for r in rows[:4]
    )
    return (
        f"thirty seconds ago the fleet held {tally} (top {len(rows)} shown): {lead}. "
        "Ask 'what does the fleet know' now to see what has changed since — this "
        "row set is read from the same table at an earlier timestamp."
    )


# --- EPISODIC: what the fleet believes, and how it was merged ---------------

WHAT_DO_WE_KNOW = """
SELECT o.kind,
       o.pos_x, o.pos_y,
       o.sightings,
       round(o.confidence::numeric, 2) AS confidence,
       o.robot_id AS first_reported_by,
       o.observed_at
  FROM observations o
 WHERE o.mission_id = %s
   AND abs(o.pos_x - %s) <= %s
   AND abs(o.pos_y - %s) <= %s
 ORDER BY o.sightings DESC, o.confidence DESC
 LIMIT 20
"""


def _render_known(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "nothing has been observed near there"
    merged = [r for r in rows if (r["sightings"] or 1) > 1]
    lead = ", ".join(
        f"{r['kind']} at {r['pos_x']},{r['pos_y']} ({r['sightings']}x)"
        for r in rows[:5]
    )
    tail = (
        f" — {len(merged)} of these were merged from multiple sightings by the "
        f"reconcile gate rather than duplicated"
        if merged
        else ""
    )
    return f"{len(rows)} belief(s) near there: {lead}{tail}"


# --- WORKING: ownership, and the lease under it -----------------------------

WHO_HOLDS_WHAT = """
SELECT t.kind,
       t.target_x, t.target_y,
       t.status,
       t.claimed_by,
       r.role,
       t.lease_expires_at,
       (t.lease_expires_at < now()) AS lease_expired
  FROM tasks t
  LEFT JOIN robots r ON r.id = t.claimed_by
 WHERE t.mission_id = %s
   AND t.status IN ('claimed', 'in_progress')
 ORDER BY t.lease_expires_at
"""


def _render_holdings(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "nothing is claimed right now"
    expired = [r for r in rows if r["lease_expired"]]
    held = ", ".join(
        f"{r['claimed_by']} ({r['role'] or '?'}) holds {r['kind']} at "
        f"{r['target_x']},{r['target_y']}"
        for r in rows[:6]
    )
    tail = (
        f" — {len(expired)} lease(s) have lapsed and that work is already "
        f"claimable by anyone, with nothing needing to notice"
        if expired
        else ""
    )
    return f"{len(rows)} task(s) in flight: {held}{tail}"


# --- PROVENANCE: the aftershock beat ----------------------------------------

AFTERSHOCK_RESPONSE = """
SELECT p.robot_id,
       p.at,
       p.trigger,
       p.rationale
  FROM plans p
 WHERE p.mission_id = %s AND p.trigger = 'aftershock'
 ORDER BY p.at
"""


def _render_aftershock(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no aftershock has forced a replan in this mission yet"
    robots = sorted({r["robot_id"] for r in rows})
    return (
        f"{len(rows)} replan(s) triggered by the aftershock, across "
        f"{len(robots)} robot(s) ({', '.join(robots)}). "
        f"First: {rows[0]['rationale']}"
    )


# --- SEMANTIC: the only question no single mission can answer ----------------

# No mission scope and no map scope: a tactic learned on one map is meant to
# apply on the next, so scoping this would defeat it. `times_recalled` is the
# column worth reading — a lesson nothing ever retrieves is dead weight, and the
# difference between "we wrote this down" and "the fleet leans on this" is the
# difference between a table and a memory.
WHAT_DID_WE_LEARN = """
SELECT m.created_at,
       m.situation,
       m.lesson,
       m.times_recalled,
       round(m.confidence::numeric, 2) AS confidence
  FROM mission_memories m
 ORDER BY m.times_recalled DESC, m.created_at DESC
 LIMIT 10
"""


def _render_learned(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "nothing yet — no mission has finished and had tactics derived from it"
    used = [r for r in rows if (r["times_recalled"] or 0) > 0]
    lead = f"{len(rows)} tactic(s) learned across earlier missions"
    if used:
        top = used[0]
        return (
            f"{lead}; {len(used)} have been retrieved into a live decision. "
            f"Most used ({top['times_recalled']}x): when {top['situation']} — "
            f"{top['lesson']}"
        )
    return f"{lead}, none retrieved into a decision yet. First: {rows[0]['lesson']}"


QUESTIONS: tuple[Question, ...] = (
    Question(
        id="why_did_robot",
        prompt="Why did robot {robot_id} do what it did?",
        memory="provenance",
        sql=WHY_DID_ROBOT,
        params=("mission_id", "robot_id", "limit"),
        render=_render_why,
    ),
    Question(
        id="unreached_victims",
        prompt="Which victims are still unreached, and what is blocking them?",
        memory="working",
        sql=UNREACHED_VICTIMS,
        params=("mission_id", "mission_id"),
        render=_render_unreached,
    ),
    Question(
        id="beliefs_30s_ago",
        prompt="What did the fleet believe thirty seconds ago?",
        memory="episodic",
        sql=BELIEFS_30S_AGO,
        params=("mission_id",),
        render=_render_recent_past,
    ),
    Question(
        id="what_do_we_know",
        prompt="What does the fleet know about the area around {x},{y}?",
        memory="episodic",
        sql=WHAT_DO_WE_KNOW,
        params=("mission_id", "x", "radius", "y", "radius"),
        render=_render_known,
    ),
    Question(
        id="who_holds_what",
        prompt="Who is working on what right now, and are any leases lapsed?",
        memory="working",
        sql=WHO_HOLDS_WHAT,
        params=("mission_id",),
        render=_render_holdings,
    ),
    Question(
        id="aftershock_response",
        prompt="How did the fleet respond to the aftershock?",
        memory="provenance",
        sql=AFTERSHOCK_RESPONSE,
        params=("mission_id",),
        render=_render_aftershock,
    ),
    Question(
        id="what_did_we_learn",
        prompt="What tactics has the fleet learned from earlier missions?",
        memory="semantic",
        sql=WHAT_DID_WE_LEARN,
        params=(),
        render=_render_learned,
    ),
)

BY_ID = {q.id: q for q in QUESTIONS}

# Defaults for every parameter a caller may leave out. `mission_id` is not here:
# it is supplied by the server from the running mission, never defaulted, so a
# missing one is an error rather than a query against the wrong mission.
DEFAULTS: dict[str, Any] = {"limit": 5, "radius": 5, "robot_id": "s1", "x": 0, "y": 0}


def catalog() -> list[dict[str, Any]]:
    """The question list, for the console UI and the writeup."""
    return [
        {"id": q.id, "prompt": q.prompt, "memory": q.memory, "params": list(q.params)}
        for q in QUESTIONS
    ]


class UnknownQuestion(KeyError):
    """Asked something outside the canned set."""


def answer(reader: Any, question_id: str, mission_id: UUID, **kwargs: Any) -> Answer:
    """Run one canned question and render it.

    Parameters are positional in the SQL and named here, so a question can use
    the same value twice — `what_do_we_know` binds `radius` for both axes —
    without the caller having to know the order.
    """
    question = BY_ID.get(question_id)
    if question is None:
        raise UnknownQuestion(
            f"{question_id!r} is not one of {sorted(BY_ID)}; the console is canned"
        )

    supplied = {**DEFAULTS, **kwargs, "mission_id": mission_id}
    values = tuple(supplied[name] for name in question.params)
    rows = reader.read(question.sql, values)
    return Answer(
        question_id=question.id,
        prompt=question.prompt.format(**supplied),
        memory=question.memory,
        sql=question.sql.strip(),
        rows=rows,
        summary=question.render(rows),
    )
