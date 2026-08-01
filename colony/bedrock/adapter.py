"""AWS Bedrock: Titan Text Embeddings V2 for beliefs, Claude for planning (§4.3).

Three modes, chosen by `BedrockAdapter(mode=...)`:

  live      real Bedrock calls via boto3. Needs AWS credentials.
  record    live calls, but every response is written to a cassette file.
  replay    no network. Responses come from the cassette; anything not in it
            falls back to the deterministic offline path below.

`--seeded` demo runs use replay, which is what makes a mission reproducible
(§4.3 "LLM calls are recorded/replayed so demo runs are reproducible"). The
offline fallback also means lanes 2 and 4 are not blocked on credentials.

LLM discipline (§4.3): plans are requested only at plan boundaries — task
selection, replan-on-aftershock, conflict resolution — never per tick. This
module does not enforce that; the agent loop does. But `calls` is exposed so a
test can assert it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EMBED_MODEL = "amazon.titan-embed-text-v2:0"
PLAN_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
EMBED_DIMS = 512  # matches observations.embedding VECTOR(512) in schema/v0.sql

LIVE, RECORD, REPLAY = "live", "record", "replay"


@dataclass
class Plan:
    """Strict-JSON plan output (§4.3): {task_id | explore(sector) | return_to_base, rationale}."""

    action: str                      # "claim_task" | "explore" | "return_to_base"
    task_id: str | None = None
    sector: str | None = None
    rationale: str = ""              # surfaced in the UI as the thought bubble

    @classmethod
    def parse(cls, raw: str) -> "Plan":
        """Parse a model response, tolerating prose or fences around the JSON.

        A malformed plan must not stall a robot mid-mission, so anything
        unparseable degrades to exploring rather than raising."""
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match is None:
            return cls(action="explore", rationale="unparseable plan; exploring")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return cls(action="explore", rationale="malformed plan JSON; exploring")
        return cls(
            action=str(data.get("action", "explore")),
            task_id=data.get("task_id"),
            sector=data.get("sector"),
            rationale=str(data.get("rationale", "")),
        )


@dataclass
class BedrockAdapter:
    mode: str = REPLAY
    region: str = "us-east-1"
    cassette_path: Path | None = None
    calls: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._cassette: dict[str, Any] = {}
        if self.cassette_path and self.cassette_path.exists():
            self._cassette = json.loads(self.cassette_path.read_text())
        self._client = None
        if self.mode in (LIVE, RECORD):
            import boto3  # imported lazily so replay mode needs no AWS deps

            self._client = boto3.client("bedrock-runtime", region_name=self.region)

    # --- embeddings -------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """Embed a belief description. Returns EMBED_DIMS floats, L2-normalized.

        Titan V2 supports 256/512/1024 dims; we ask for 512 to match the schema.
        """
        key = _key("embed", text)
        if self.mode == REPLAY:
            cached = self._cassette.get(key)
            return cached if cached is not None else _offline_embedding(text)

        body = json.dumps({
            "inputText": text,
            "dimensions": EMBED_DIMS,
            "normalize": True,
        })
        response = self._client.invoke_model(modelId=EMBED_MODEL, body=body)
        vector = json.loads(response["body"].read())["embedding"]
        self.calls += 1
        if self.mode == RECORD:
            self._remember(key, vector)
        return vector

    # --- planning ---------------------------------------------------------

    def plan(self, role_card: str, beliefs_digest: str, open_tasks: list[dict[str, Any]]) -> Plan:
        """Ask for one decision. Prompt stays under ~1.5k tokens by design (§4.3)."""
        prompt = _plan_prompt(role_card, beliefs_digest, open_tasks)
        key = _key("plan", prompt)

        if self.mode == REPLAY:
            cached = self._cassette.get(key)
            if cached is not None:
                return Plan.parse(cached)
            return _offline_plan(open_tasks)

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 300,
            "temperature": 0,          # determinism matters more than flair here
            "messages": [{"role": "user", "content": prompt}],
        })
        response = self._client.invoke_model(modelId=PLAN_MODEL, body=body)
        text = json.loads(response["body"].read())["content"][0]["text"]
        self.calls += 1
        if self.mode == RECORD:
            self._remember(key, text)
        return Plan.parse(text)

    def _remember(self, key: str, value: Any) -> None:
        self._cassette[key] = value
        if self.cassette_path:
            self.cassette_path.parent.mkdir(parents=True, exist_ok=True)
            self.cassette_path.write_text(json.dumps(self._cassette, indent=2))


def _key(kind: str, payload: str) -> str:
    return f"{kind}:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _plan_prompt(role_card: str, beliefs_digest: str, open_tasks: list[dict[str, Any]]) -> str:
    tasks = "\n".join(
        f"- {t['id']} {t['kind']} at ({t.get('target_x')},{t.get('target_y')})"
        f" priority={t.get('priority', 1)}"
        for t in open_tasks
    ) or "- (none)"
    return (
        f"{role_card}\n\n"
        f"Shared beliefs:\n{beliefs_digest}\n\n"
        f"Open tasks:\n{tasks}\n\n"
        "Choose exactly one action. Reply with JSON only:\n"
        '{"action": "claim_task"|"explore"|"return_to_base", '
        '"task_id": "<id or null>", "sector": "<sector or null>", '
        '"rationale": "<one short sentence>"}'
    )


# --- offline fallbacks -------------------------------------------------------
#
# Deterministic, dependency-free, and good enough to run a mission without AWS.
# The rule-based path is also §5.1 lane 2's required fallback, so it is not
# throwaway scaffolding.


def _offline_embedding(text: str) -> list[float]:
    """Hash-based pseudo-embedding, L2-normalized.

    Same text always gives the same vector and similar text does NOT give a
    similar vector — so the reconcile gate's merge path is exercised, but any
    test asserting *semantic* similarity must use a live or recorded embedding.
    """
    digest = hashlib.sha512(text.encode()).digest()
    raw = [
        (digest[i % len(digest)] ^ digest[(i * 7 + 13) % len(digest)]) / 255.0 - 0.5
        for i in range(EMBED_DIMS)
    ]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def _offline_plan(open_tasks: list[dict[str, Any]]) -> Plan:
    """Highest-priority open task, else explore. This is the rule-based path the
    agent falls back to whenever Bedrock is unavailable or rate-capped."""
    if not open_tasks:
        return Plan(action="explore", sector="nearest-unexplored",
                    rationale="no open tasks; expanding the frontier")
    best = max(open_tasks, key=lambda t: t.get("priority", 1))
    return Plan(
        action="claim_task",
        task_id=str(best["id"]),
        rationale=f"highest-priority open task: {best['kind']}",
    )


def adapter_from_env() -> BedrockAdapter:
    """Live only when explicitly asked for AND credentials exist; replay otherwise.

    Defaulting to replay means a missing credential is a degraded demo, not a
    crashed one.
    """
    mode = os.environ.get("COLONY_BEDROCK_MODE", REPLAY)
    if mode in (LIVE, RECORD) and not (
        os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE")
    ):
        mode = REPLAY
    cassette = os.environ.get("COLONY_BEDROCK_CASSETTE")
    return BedrockAdapter(
        mode=mode,
        region=os.environ.get("AWS_REGION", "us-east-1"),
        cassette_path=Path(cassette) if cassette else None,
    )
