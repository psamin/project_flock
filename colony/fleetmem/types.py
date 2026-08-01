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
    "blocked", "open", "claimed", "in_progress", "done", "failed",
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
class Task:
    id: UUID
    mission_id: UUID
    kind: str
    target: tuple[int | None, int | None]
    status: str
    priority: int = 1
    depends_on: list[UUID] = field(default_factory=list)
    claimed_by: str | None = None
