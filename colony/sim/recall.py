"""Semantic memory: tactics learned from one mission, applied in the next.

The four-memory taxonomy (§4.0) names `mission_memories` as SEMANTIC memory —
what we learned across runs. The question is what "learned" can honestly mean
for a fleet that will never see the same disaster twice.

Not where the victims were. The same collapse does not recur on the same tiles,
so a remembered coordinate transfers to nothing — and a fleet that recalls
victim positions is a fleet handed the answer, which is worse than useless in
front of anyone reading carefully.

What transfers is **technique**. That a victim behind rubble-heavy debris is
worth staging a medic for before the clear finishes. That fire adjacent to a
located victim outruns a medic dispatched on distance alone. Those hold on a map
nobody has walked yet, and they are the kind of thing a fleet can only learn by
having run missions before.

So the loop is:

    mission ends   ->  summarise what happened, in figures rather than places
                   ->  ask Claude for tactics that would generalise
                   ->  embed the *situation* each applies to, store both

    plan boundary  ->  describe what this robot is facing right now
                   ->  cosine-search for the situations most like it
                   ->  put those tactics in the planning prompt

The embedding is of the situation rather than the advice, because retrieval asks
"what does this moment resemble?" — which is the whole reason a vector index
belongs here and a keyword lookup does not.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# How many tactics to put in front of a planning robot. Small: the digest budget
# is ~1.5k tokens (§4.3), and a model given twelve maxims applies none of them.
RECALL_LIMIT = 3

# How many lessons to draw from one mission. A run that produced eight
# "insights" produced none; this cap is a quality filter, not a cost one.
LESSON_LIMIT = 3


# --- the write path: what one mission learned --------------------------------


def run_digest(mem: Any, mission_id: Any, world: Any, metrics: dict[str, Any]) -> str:
    """One mission's outcome, in figures a tactic could be derived from.

    Deliberately free of coordinates. The digest is the LLM's only view of the
    run, so anything place-shaped in here comes back as a place-shaped lesson —
    the prompt forbids that, and not offering the temptation is stronger than
    forbidding it.

    Everything is read from the event log and belief rows rather than simulator
    state, the same discipline sim/metrics.py opens with: the fleet may only
    learn from what it actually observed.
    """
    events = mem.events(mission_id)
    verbs = Counter(e["verb"] for e in events)
    beliefs = mem.get_beliefs(mission_id)
    victims = [b for b in beliefs if b.kind == "victim"]
    hazards = [b for b in beliefs if b.kind == "hazard"]

    # How much of the work was unblocking rather than rescuing — the ratio a
    # lesson about staging or ordering would key off.
    clears = verbs.get("debris_cleared", 0)
    rescues = verbs.get("victim_stabilized", 0)
    lost = verbs.get("victim_lost", 0)

    lines = [
        f"Fleet: {len(world.robots)} robots (scouts, one lifter, one medic) on a "
        f"{world.map.width}x{world.map.height} grid, {metrics.get('ticks', 0)} ticks.",
        f"Rescued {rescues} of {metrics.get('victims_total', 0)} victims; {lost} died "
        f"before a medic arrived.",
        f"Located {len(victims)} victims and {len(hazards)} hazards.",
        f"Cleared debris {clears} times to reach them.",
    ]
    median = metrics.get("median_time_to_stabilize")
    if median is not None:
        lines.append(f"Median ticks from mission start to a rescue: {median}.")
    if metrics.get("double_work_incidents"):
        lines.append(
            f"{metrics['double_work_incidents']} tasks were claimed by more than one "
            "robot over the run — wasted trips."
        )
    if metrics.get("duplicate_effort_index"):
        lines.append(
            f"{round(metrics['duplicate_effort_index'] * 100)}% of tile visits covered "
            "ground another robot had already seen."
        )
    if verbs.get("fire_spread"):
        lines.append(f"Fire spread {verbs['fire_spread']} times during the mission.")
    if verbs.get("aftershock"):
        lines.append("An aftershock changed the map mid-mission.")
    if verbs.get("returning_to_base"):
        lines.append(
            f"Robots broke off {verbs['returning_to_base']} times to recharge or restock."
        )
    return "\n".join(lines)


def learn(mem: Any, embedder: Any, mission_id: Any, world: Any, metrics: dict) -> list:
    """Derive tactics from a finished mission and write them down.

    Returns the ids written. Empty is a normal outcome: a run with nothing to
    generalise should add nothing, and a table of vacuous maxims is worse than
    an empty one — every lesson stored is a lesson retrieved into every similar
    situation from then on.
    """
    digest = run_digest(mem, mission_id, world, metrics)
    lessons = embedder.derive_lessons(digest, limit=LESSON_LIMIT)
    written = []
    for item in lessons:
        written.append(
            mem.remember_lesson(
                mission_id,
                item["situation"],
                item["lesson"],
                embedding=embedder.embed(item["situation"]),
                evidence={"run": digest, "metrics": metrics},
            )
        )
    return written


# --- the read path: what this moment resembles -------------------------------


def situation_of(robot: Any, beliefs: list, open_tasks: list) -> str:
    """What this robot is facing, as the text recall searches with.

    Written to describe conditions rather than places, so it lands near the
    `situation` half of stored lessons. Deterministic given the same state:
    counts and categories only, no ids and no coordinates, so the same
    predicament produces the same query vector — which is what lets one recorded
    embedding serve every rerun.
    """
    kinds = Counter(b.kind for b in beliefs)
    waiting = Counter(t.kind.split(":", 1)[0] for t in open_tasks)
    parts = [
        f"{getattr(robot, 'role', 'robot')} with battery {getattr(robot, 'battery', 0)}",
        f"{kinds.get('victim', 0)} victims and {kinds.get('hazard', 0)} hazards known",
    ]
    if waiting:
        parts.append(
            "open work: "
            + ", ".join(f"{n} {kind}" for kind, n in sorted(waiting.items()))
        )
    else:
        parts.append("no open work")
    if getattr(robot, "kits", None):
        parts.append(f"carrying {robot.kits} kits")
    return "; ".join(parts)


def recall(mem: Any, embedder: Any, situation: str, limit: int = RECALL_LIMIT) -> list:
    """The tactics most like this situation, nearest first."""
    embedding = embedder.embed(situation) if embedder is not None else None
    return mem.recall_lessons(embedding, limit=limit)


def as_prompt_lines(lessons: list) -> list[str]:
    """Render lessons for the planning prompt: the condition, then the advice."""
    return [f"when {m.situation} — {m.lesson}" for m in lessons]
