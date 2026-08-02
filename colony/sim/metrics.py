"""Mission metrics, computed from the event log (PRD §4.7).

From `events`, deliberately — not from live objects. The event log is what
survives the mission, what the commander console queries, and what a judge can
be shown as rows in a database rather than numbers in a UI. Anything computed
another way would be a second source of truth that could disagree with the one
on screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any, Iterable

COVERAGE_AT_TICK = 500  # §4.7: Coverage@500


@dataclass
class Metrics:
    """One mission's numbers. `coordination_gain` needs two runs, so it lives on
    the comparison rather than here."""

    victims_total: int = 0
    victims_stabilized: int = 0
    victims_lost: int = 0
    rescue_rate: float = 0.0
    median_time_to_stabilize: float | None = None
    duplicate_effort_index: float = 0.0
    double_work_incidents: int = 0
    coverage_at_500: float = 0.0
    ticks: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "victims_total": self.victims_total,
            "victims_stabilized": self.victims_stabilized,
            "victims_lost": self.victims_lost,
            "rescue_rate": round(self.rescue_rate, 3),
            "median_time_to_stabilize": self.median_time_to_stabilize,
            "duplicate_effort_index": round(self.duplicate_effort_index, 3),
            "double_work_incidents": self.double_work_incidents,
            "coverage_at_500": round(self.coverage_at_500, 3),
            "ticks": self.ticks,
        }


def compute(
    events: Iterable[dict[str, Any]],
    victims_total: int,
    coverage_at_500: float = 0.0,
    ticks: int = 0,
    horizon: int | None = None,
) -> Metrics:
    """Derive §4.7's metrics from a mission's event stream.

    `horizon` is the mission length, used to censor victims who were never
    rescued. See `median_time_to_stabilize` for why that matters.
    """
    events = list(events)
    stabilized_at: dict[str, int] = {}
    lost: set[str] = set()
    visits: list[tuple[str, int, int]] = []
    claims: dict[str, list[str]] = {}

    for event in events:
        verb, detail = event["verb"], event.get("detail") or {}
        tick = event.get("tick") or detail.get("tick") or 0
        if verb == "victim_stabilized":
            stabilized_at.setdefault(detail.get("victim", ""), tick)
        elif verb == "victim_lost":
            lost.add(detail.get("victim", ""))
        elif verb == "tile_visited":
            visits.append((event["actor"], detail.get("x", 0), detail.get("y", 0)))
        elif verb == "task_claimed":
            claims.setdefault(detail.get("task", ""), []).append(event["actor"])

    metrics = Metrics(
        victims_total=victims_total,
        victims_stabilized=len(stabilized_at),
        victims_lost=len(lost),
        coverage_at_500=coverage_at_500,
        ticks=ticks,
    )
    if victims_total:
        metrics.rescue_rate = len(stabilized_at) / victims_total
    # Censored at the horizon: a victim never rescued counts as "not by the end
    # of the mission", not as absent from the sample. Taking the median over
    # only the rescued made a run that saved one easy victim quickly look
    # *better* than one that saved seven — the baseline scored a median of 12
    # against the coordinated run's 70, and §4.7's coordination gain came out at
    # -4.8 while the coordinated fleet rescued seven times as many people.
    if victims_total:
        limit = horizon or ticks or 0
        times = list(stabilized_at.values())
        times += [limit] * (victims_total - len(times))
        metrics.median_time_to_stabilize = float(median(times))

    metrics.duplicate_effort_index = duplicate_effort(visits)
    # A task claimed by more than one robot across the mission. Distinct from
    # the lease guarantee, which is about two robots holding it *at once*: this
    # counts the wasted approach when one robot walks to work another finished.
    metrics.double_work_incidents = sum(
        1 for actors in claims.values() if len(set(actors)) > 1
    )
    return metrics


def duplicate_effort(visits: list[tuple[str, int, int]]) -> float:
    """§4.7: redundant tile visits / total visits.

    Redundant means "a tile another robot had already visited". Revisiting your
    own ground is not duplicated effort — it is how you get anywhere — so a
    robot's own history does not count against it.
    """
    if not visits:
        return 0.0
    seen_by: dict[tuple[int, int], set[str]] = {}
    redundant = 0
    for actor, x, y in visits:
        others = seen_by.setdefault((x, y), set())
        if others and actor not in others:
            redundant += 1
        others.add(actor)
    return redundant / len(visits)


@dataclass
class Comparison:
    """Coordination ON against OFF — the §4.7 number the video ends on."""

    coordinated: Metrics
    baseline: Metrics
    coordination_gain: float = field(default=0.0)

    def __post_init__(self) -> None:
        self.coordination_gain = _gain(
            self.baseline.median_time_to_stabilize,
            self.coordinated.median_time_to_stabilize,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "coordinated": self.coordinated.to_json(),
            "baseline": self.baseline.to_json(),
            "coordination_gain": round(self.coordination_gain, 3),
            "rescue_rate_delta": round(
                self.coordinated.rescue_rate - self.baseline.rescue_rate, 3
            ),
        }


def _gain(baseline: float | None, coordinated: float | None) -> float:
    """(baseline - coordinated) / baseline, per §4.7.

    Zero when the baseline rescued nobody: there is no median to improve on, and
    dividing by nothing would report an infinite win — the most flattering
    possible number and the least defensible one in front of a judge.
    """
    if not baseline or coordinated is None:
        return 0.0
    return (baseline - coordinated) / baseline
