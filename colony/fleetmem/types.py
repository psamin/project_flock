"""Shared types for the fleetmem SDK (PRD §5.2 contract 1).

Frozen Aug 3 — changes after that need a team ping, not a silent commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

# Task lifecycle (§4.4): blocked -> open -> claimed -> in_progress -> done | failed(->open)
BLOCKED, OPEN, CLAIMED, IN_PROGRESS, DONE, FAILED = (
    "blocked",
    "open",
    "claimed",
    "in_progress",
    "done",
    "failed",
)


@dataclass(frozen=True)
class Belief:
    """A shared observation, as other robots see it."""

    id: UUID
    kind: str
    pos: tuple[int, int]
    payload: dict[str, Any]
    confidence: float
    sightings: int
    robot_id: str | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class Match:
    """An existing belief the reconcile gate considers the same thing."""

    belief_id: UUID
    distance: float


@dataclass(frozen=True)
class Lesson:
    """A tactic an earlier mission learned (semantic memory, §4.0).

    Deliberately not a fact about a place. The same disaster does not recur on
    the same tiles, so a remembered coordinate transfers to nothing — and a
    fleet recalling where victims were is a fleet handed the answer. What
    transfers is technique, so a lesson is a `situation` it applies to and what
    to do when that situation holds.

    `situation` is what gets embedded, because retrieval asks "what does this
    moment resemble?": the agent embeds what it is facing and the index returns
    the tactics learned in moments like it.

    `distance` is cosine distance from the query vector, carried out of the SDK
    deliberately — the claim is that CockroachDB's vector index found this row,
    and a number the console can print is what makes that checkable rather than
    asserted.
    """

    id: UUID
    mission_id: UUID | None
    situation: str
    lesson: str
    evidence: dict[str, Any]
    confidence: float = 0.5
    times_recalled: int = 0
    distance: float = 0.0
    created_at: datetime | None = None


@dataclass(frozen=True)
class Task:
    id: UUID
    mission_id: UUID
    kind: str
    target: tuple[int | None, int | None]
    status: str
    priority: int = 1
    depends_on: list[UUID] = field(default_factory=list)
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None


# Plan trigger vocabulary (§4.5 DDL comment).
IDLE_TRIGGER, TASK_DONE, WORLD_CHANGED, AFTERSHOCK = (
    "idle",
    "task_done",
    "world_changed",
    "aftershock",
)


@dataclass(frozen=True)
class Plan:
    """A decision plus the memories that caused it (FR-17, provenance memory).

    Two kinds of memory, kept in separate columns because they resolve against
    different tables: `based_on` holds `observations` rows — what the fleet
    could see — and `recalled_from` holds `mission_memories` rows — what it had
    learned. Merging them into one UUID[] would mean ids that silently resolve
    to nothing, which is how a decision trace turns back into a plausible story.
    """

    id: UUID
    mission_id: UUID
    robot_id: str
    trigger: str
    chosen: dict[str, Any]
    rationale: str
    based_on: list[UUID] = field(default_factory=list)
    recalled_from: list[UUID] = field(default_factory=list)
    at: datetime | None = None
