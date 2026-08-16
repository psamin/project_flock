"""Semantic memory: what one mission learned, and how the next one uses it.

The four-memory taxonomy (§4.0) names `mission_memories` as SEMANTIC memory —
what we learned across runs — and it is the one table whose search deliberately
crosses missions. This module is both ends of that: the summarizer that writes a
finished mission down, and the recall that seeds the next one.

Two rules shape everything here.

**Facts come out of fleet memory, never off the simulator.** A summary built
from `World.victims` would describe victims the fleet never found, and the next
mission would then "remember" knowledge nobody ever earned. Everything below
reads `get_beliefs`, the same discipline `sim/metrics.py` opens with.

**The embedding is the retrieval key; the JSONB is the payload.** The summary is
prose so it embeds to something meaningful, and nothing downstream ever parses
it — the read path acts on `outcome["victim_sectors"]`. Asking a model to parse
English back out of its own summary would be a second place to go wrong for no
gain.

A consequence of the first rule that is easy to get wrong: **run-specific
numbers must stay out of the embedded text**. The cassette key is a hash of the
exact string (`bedrock/adapter.py:_key`), so a summary carrying "rescued 7 of 8
in 940 ticks" misses the cassette on every rerun and silently degrades to the
offline embedding, which is not semantically meaningful. Victim positions are map
data rather than run data, so a summary built from them is near-identical run to
run — which is what makes one recorded Titan embedding reusable. Counts and
timings go in `outcome`, where they belong anyway.
"""

from __future__ import annotations

from typing import Any

from world.map_format import WorldMap

# How many earlier missions to pull. Small on purpose: the read path only wants
# a prior on search order, and every extra memory dilutes it.
RECALL_LIMIT = 3

# Victim and hazard sites named in the summary. Enough to characterise the map,
# short enough to stay well inside the digest budget.
SITE_LIMIT = 8


def map_key(world_map: WorldMap) -> str:
    """One definition of "the same map".

    Name, not path and not seed: two different seeds on Aftershock are two runs
    of the same scenario and should share what was learned about it. Knowledge
    about this map's rubble does not transfer to a different map, which is why
    this is the vector index prefix rather than a filter.
    """
    return (world_map.name or "unknown").strip().lower().replace(" ", "_")


def query_text(world_map: WorldMap) -> str:
    """The text recall embeds to search with.

    Fixed per map, and it has to be: this string is hashed into the cassette
    key, so anything varying per run (a tick count, a mission id) would miss the
    cassette on every rerun and fall back to the offline embedding. Kept beside
    the summary template below so the two stay in sympathy — they are the query
    and the document of the same retrieval.
    """
    return (
        f"disaster-response mission on the {world_map.name or 'unknown'} map, "
        f"a {world_map.width}x{world_map.height} collapsed city block; "
        "locate and stabilize victims trapped behind debris"
    )


def summarize(mem: Any, mission_id: Any, world_map: WorldMap) -> tuple[str, dict]:
    """What this mission learned: prose to embed, and JSON to act on.

    Returns `(summary, outcome_fragment)`. The caller merges its metrics into
    the fragment — this function does not compute numbers, it reports places.
    """
    victims = sorted(
        mem.get_beliefs(mission_id, kind="victim"),
        key=lambda b: (-b.sightings, b.pos),
    )[:SITE_LIMIT]
    hazards = sorted({b.pos for b in mem.get_beliefs(mission_id, kind="hazard")})[
        :SITE_LIMIT
    ]

    victim_sites = [list(b.pos) for b in victims]
    hazard_sites = [list(p) for p in hazards]
    victim_sectors = sorted(
        {s for s in (world_map.sector_at(*b.pos) for b in victims) if s}
    )
    all_sectors = {s["id"] for s in world_map.sectors}
    empty_sectors = sorted(all_sectors - set(victim_sectors))

    lines = [
        f"Disaster-response mission on the {world_map.name or 'unknown'} map, "
        f"a {world_map.width}x{world_map.height} collapsed city block."
    ]
    if victim_sites:
        where = ", ".join(f"({x},{y})" for x, y in victim_sites)
        lines.append(f"Victims were found at {where}.")
    if victim_sectors:
        lines.append(f"Victims clustered in sectors {', '.join(victim_sectors)}.")
    if hazard_sites:
        where = ", ".join(f"({x},{y})" for x, y in hazard_sites)
        lines.append(f"Fire and hazards were reported near {where}.")
    if empty_sectors:
        lines.append(f"Sectors {', '.join(empty_sectors)} held no victims.")

    outcome = {
        "map": map_key(world_map),
        "victim_sites": victim_sites,
        "hazard_sites": hazard_sites,
        # The only field the read path consumes. Everything else is for the
        # console, the writeup, and anyone reading the table by hand.
        "victim_sectors": victim_sectors,
        "empty_sectors": empty_sectors,
    }
    return " ".join(lines), outcome


def write_memory(
    mem: Any,
    embedder: Any,
    mission_id: Any,
    world_map: WorldMap,
    metrics: dict[str, Any] | None = None,
) -> Any:
    """Deposit what this mission learned. Returns the row id, or None if already
    written (see `remember_mission`'s idempotence guard)."""
    summary, outcome = summarize(mem, mission_id, world_map)
    outcome["metrics"] = metrics or {}
    embedding = embedder.embed(summary) if embedder is not None else None
    return mem.remember_mission(
        mission_id,
        map_key(world_map),
        summary,
        embedding=embedding,
        outcome=outcome,
    )


def recall(mem: Any, embedder: Any, world_map: WorldMap, limit: int = RECALL_LIMIT):
    """Earlier missions on this map, nearest first."""
    embedding = embedder.embed(query_text(world_map)) if embedder is not None else None
    return mem.recall_missions(map_key(world_map), embedding, limit=limit)


def hot_sectors(memories) -> list[str]:
    """Sectors earlier missions found victims in.

    This is a prior on search *order* and nothing more. It does not tell the
    fleet where victims are — that would be sensing without a sensor, and the
    scoreboard counts located victims off belief rows, so a fleet that "knew"
    at tick 0 would be visibly cheating. Every victim still has to be found by a
    scout that flies over it.
    """
    return sorted(
        {s for m in memories for s in (m.outcome.get("victim_sectors") or [])}
    )
