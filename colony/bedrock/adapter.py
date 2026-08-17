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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EMBED_MODEL = "amazon.titan-embed-text-v2:0"
PLAN_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
EMBED_DIMS = 512  # matches observations.embedding VECTOR(512) in schema/v1_1.sql

LIVE, RECORD, REPLAY = "live", "record", "replay"


def _boto_errors() -> tuple[type[BaseException], ...]:
    """Exception types the live path can raise; empty without boto3, in which
    case the live path is unreachable anyway."""
    try:
        from botocore.exceptions import BotoCoreError, ClientError

        return (ClientError, BotoCoreError)
    except ImportError:
        return ()


_BOTO_ERRORS = _boto_errors()

# Service-side failures a robot should ride out. Everything else is a bug in our
# configuration, not weather.
_TRANSIENT_CODES = frozenset(
    {
        "ThrottlingException",
        "ServiceQuotaExceededException",
        "InternalServerException",
        "ServiceUnavailableException",
        "ModelTimeoutException",
        "ModelNotReadyException",
    }
)


def _is_transient(exc: BaseException) -> bool:
    """Whether to fall back rather than raise.

    `ClientError` is also what Bedrock raises for AccessDeniedException,
    ValidationException and ResourceNotFoundException — a broken IAM policy, a
    revoked model-access grant, a typo'd modelId. Treating those as transient
    would mean the fleet quietly runs rule-based planning forever while looking
    perfectly healthy, and we would demo an AWS integration that never once
    called AWS. Those must surface. Transport-level BotoCoreError (timeouts,
    connection failures) is genuinely transient.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code") in _TRANSIENT_CODES
    return isinstance(exc, BotoCoreError)


@dataclass
class Plan:
    """Strict-JSON plan output (§4.3): {task_id | explore(sector) | return_to_base, rationale}."""

    action: str  # "claim_task" | "explore" | "return_to_base"
    task_id: str | None = None
    sector: str | None = None
    rationale: str = ""  # surfaced in the UI as the thought bubble

    @classmethod
    def parse(cls, raw: str) -> Plan:
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

        body = json.dumps(
            {
                "inputText": text,
                "dimensions": EMBED_DIMS,
                "normalize": True,
            }
        )
        try:
            response = self._client.invoke_model(modelId=EMBED_MODEL, body=body)
        except _BOTO_ERRORS as exc:
            if not _is_transient(exc):
                raise
            # A throttled embedding must not drop the observation entirely; the
            # reconcile gate degrades rather than the scout losing the sighting.
            return _offline_embedding(text)
        vector = json.loads(response["body"].read())["embedding"]
        self.calls += 1
        if self.mode == RECORD:
            self._remember(key, vector)
        return vector

    # --- planning ---------------------------------------------------------

    def plan(
        self,
        role_card: str,
        beliefs_digest: str,
        open_tasks: list[dict[str, Any]],
        tactics: Sequence[str] = (),
    ) -> Plan:
        """Ask for one decision. Prompt stays under ~1.5k tokens by design (§4.3)."""
        # Sorted before the prompt is built, because the prompt text is the
        # cassette key. `open_tasks()` orders by priority alone, so two equal
        # priority tasks can arrive in either order, producing two different
        # prompts for the same mission state. The tiebreak is the *handle*, not
        # the row id — see task_handle: ids are random per run, so tiebreaking
        # on one made the ordering random too.
        tasks = _ordered(open_tasks)
        prompt = _plan_prompt(role_card, beliefs_digest, tasks, tactics)
        key = _key("plan", prompt)

        if self.mode == REPLAY:
            cached = self._cassette.get(key)
            if cached is not None:
                return _resolve(Plan.parse(cached), tasks)
            return _offline_plan(tasks)

        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 300,
                "temperature": 0,  # determinism matters more than flair here
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        try:
            response = self._client.invoke_model(modelId=PLAN_MODEL, body=body)
        except _BOTO_ERRORS as exc:
            if not _is_transient(exc):
                raise
            # Throttling or a Bedrock hiccup must not stall a robot mid-mission;
            # §5.1 lane 2 requires a rule-based fallback path, and this is it.
            return _offline_plan(tasks)
        text = json.loads(response["body"].read())["content"][0]["text"]
        self.calls += 1
        if self.mode == RECORD:
            self._remember(key, text)
        return _resolve(Plan.parse(text), tasks)

    def derive_lessons(self, run_digest: str, limit: int = 3) -> list[dict[str, str]]:
        """Turn one mission's figures into tactics that transfer to other maps.

        The second place an LLM earns its keep here, and for the opposite reason
        to planning: this is not a decision under time pressure, it is the one
        job in the system that is genuinely about generalising from experience.
        Rules can rank tasks; rules cannot look at a run and notice that waiting
        for a clear to finish before dispatching the medic was what cost it.

        The prompt forbids coordinates and sector names explicitly. A "lesson"
        naming a tile is a fact about one map — it transfers nowhere, and a
        fleet recalling victim positions is a fleet handed the answer. That
        constraint is the whole reason this call exists rather than a template.

        Returns `[{"situation": ..., "lesson": ...}]`; empty on any failure,
        because a mission that ends without learning anything is a mission that
        still ended.
        """
        prompt = _lessons_prompt(run_digest, limit)
        key = _key("lessons", prompt)

        if self.mode == REPLAY:
            cached = self._cassette.get(key)
            return _parse_lessons(cached, limit) if cached is not None else []

        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 600,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        try:
            response = self._client.invoke_model(modelId=PLAN_MODEL, body=body)
        except _BOTO_ERRORS as exc:
            if not _is_transient(exc):
                raise
            return []
        text = json.loads(response["body"].read())["content"][0]["text"]
        self.calls += 1
        if self.mode == RECORD:
            self._remember(key, text)
        return _parse_lessons(text, limit)

    def knows_plan(
        self,
        role_card: str,
        beliefs_digest: str,
        open_tasks: list[dict[str, Any]],
        tactics: Sequence[str] = (),
    ) -> bool:
        """Whether the cassette can answer this prompt without inventing one.

        Lets a caller tell "a real decision was recorded for exactly this
        situation" from "replay would fall back to the offline rules". The agent
        loop uses it to keep fabricated rationales out of the UI: the rules are
        perfectly good at *deciding*, but a rule-based choice presented as a
        Bedrock rationale is a claim we cannot support in front of a judge.
        """
        tasks = _ordered(open_tasks)
        return _key("plan", _plan_prompt(role_card, beliefs_digest, tasks, tactics)) in (
            self._cassette
        )

    def status(self) -> dict[str, Any]:
        """What this adapter is actually doing, for `/health`.

        `calls` counts AWS round-trips and nothing else: a replay cassette hit is
        a real recorded decision but not a call, and `_offline_plan` is not one
        either. That is the distinction "are we demoing an AWS integration"
        actually turns on, so it is the one the counter reports.

        `cassette_entries` is here because replay with an empty cassette is the
        one configuration that looks healthy while never deciding anything: every
        `knows_plan` misses, so the fleet runs on rules and the mode alone does
        not say so.
        """
        return {
            "mode": self.mode,
            "calls": self.calls,
            "cassette_entries": len(self._cassette),
        }

    def _remember(self, key: str, value: Any) -> None:
        self._cassette[key] = value
        if self.cassette_path:
            self.cassette_path.parent.mkdir(parents=True, exist_ok=True)
            self.cassette_path.write_text(json.dumps(self._cassette, indent=2))


def _key(kind: str, payload: str) -> str:
    return f"{kind}:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _resolve(plan: Plan, open_tasks: list[dict[str, Any]]) -> Plan:
    """Turn the handle the model answered with back into a row id.

    The prompt names tasks by handle so it is reproducible, but every caller
    downstream matches on `tasks.id`. Unresolvable is not an error: a stale or
    invented handle costs one lost claim race and the robot's own ranking
    carries it, which is the same tolerance the id path always had.
    """
    if plan.task_id is None:
        return plan
    for task in open_tasks:
        if plan.task_id == task_handle(task):
            plan.task_id = str(task["id"])
            return plan
    return plan


def _ordered(open_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Total, content-derived order — the same list on every run.

    The row id is the last tiebreak rather than the first, and only decides
    between tasks whose handles are identical. Real missions do not produce
    those — one explore per sector, one clear per blocking tile, one delivery
    per victim — but an order that depends on input order is a non-determinism
    waiting to happen, so the ordering stays total either way.
    """
    return sorted(
        open_tasks,
        key=lambda t: (-t.get("priority", 1), task_handle(t), str(t["id"])),
    )


def task_handle(task: dict[str, Any]) -> str:
    """A name for a task that is the same on every run of the same mission.

    Row ids cannot be used here, and this is the subtle one. `tasks.id` is
    `gen_random_uuid()`, so it differs on every run against a real cluster —
    which means a prompt containing ids can never match a recorded one, the
    cassette misses every single time, and the whole fleet silently runs on
    rules while `/health` reports a loaded cassette. Worse, the sort tiebreak
    was `str(id)` too, so even the *order* of the task list was random.

    Kind and target are content, not identity: one explore task per sector, one
    clear per blocking tile, one delivery per victim. So they name a task
    stably, and the model gets something more legible than a uuid into the
    bargain.
    """
    return f"{task['kind']}@{task.get('target_x')},{task.get('target_y')}"


def _plan_prompt(
    role_card: str,
    beliefs_digest: str,
    open_tasks: list[dict[str, Any]],
    tactics: Sequence[str] = (),
) -> str:
    tasks = (
        "\n".join(
            f"- {task_handle(t)} priority={t.get('priority', 1)}" for t in open_tasks
        )
        or "- (none)"
    )
    # Tactics come *before* the current situation on purpose: they are standing
    # knowledge, and the model should read the moment in their light rather than
    # decide first and rationalise afterwards. Omitted entirely when there are
    # none, so a fleet that has learned nothing produces the exact prompt it
    # always did — which is what keeps the pre-memory cassette valid.
    learned = (
        "What earlier missions learned:\n"
        + "\n".join(f"- {t}" for t in tactics)
        + "\n\n"
        if tactics
        else ""
    )
    return (
        f"{role_card}\n\n"
        f"{learned}"
        f"Shared beliefs:\n{beliefs_digest}\n\n"
        f"Open tasks:\n{tasks}\n\n"
        "Choose exactly one action. Reply with JSON only:\n"
        '{"action": "claim_task"|"explore"|"return_to_base", '
        '"task_id": "<id or null>", "sector": "<sector or null>", '
        '"rationale": "<one short sentence>"}'
    )


def _lessons_prompt(run_digest: str, limit: int) -> str:
    return (
        "You are reviewing a completed search-and-rescue mission run by a fleet "
        "of autonomous robots, to extract tactics that will help on FUTURE "
        "missions on DIFFERENT maps.\n\n"
        f"What happened:\n{run_digest}\n\n"
        f"Give at most {limit} lessons. Each must be a general tactic, not a "
        "fact about this map.\n"
        "Hard rules:\n"
        "- Never mention coordinates, tile positions, or sector names. A lesson "
        "naming a place is useless on the next map and will be discarded.\n"
        "- `situation` describes conditions a robot could recognise mid-mission "
        "(what it can see, what it is carrying, what is blocking it).\n"
        "- `lesson` is the action or ordering to prefer when they hold.\n"
        "- If the run shows nothing worth generalising, return an empty list.\n\n"
        "Reply with JSON only:\n"
        '{"lessons": [{"situation": "<when this applies>", '
        '"lesson": "<what to do>"}]}'
    )


def _parse_lessons(raw: str, limit: int) -> list[dict[str, str]]:
    """Strict-JSON parse that degrades to nothing rather than to garbage.

    A malformed lesson is worse than no lesson: it is written once and then
    retrieved into every similar situation forever, so this drops anything that
    is not a well-formed situation/lesson pair.
    """
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if match is None:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for item in (payload.get("lessons") or [])[:limit]:
        situation = str(item.get("situation", "")).strip()
        lesson = str(item.get("lesson", "")).strip()
        if situation and lesson:
            out.append({"situation": situation, "lesson": lesson})
    return out


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


def _offline_plan(open_tasks: list[dict[str, Any]]) -> Plan:  # noqa: D401
    """Highest-priority open task, else explore. This is the rule-based path the
    agent falls back to whenever Bedrock is unavailable or rate-capped."""
    if not open_tasks:
        return Plan(
            action="explore",
            sector="nearest-unexplored",
            rationale="no open tasks; expanding the frontier",
        )
    best = max(open_tasks, key=lambda t: t.get("priority", 1))
    return Plan(
        action="claim_task",
        task_id=str(best["id"]),
        rationale=f"highest-priority open task: {best['kind']}",
    )


def has_credentials() -> bool:
    """Whether boto3 can resolve credentials by any means.

    Checking AWS_ACCESS_KEY_ID and AWS_PROFILE is not enough: the agents deploy
    to ECS Fargate (§4.6), where credentials come from the task role and neither
    variable is set. An env-var check would downgrade the deployed fleet to
    offline planning while working perfectly on a laptop.
    """
    try:
        import botocore.session

        return botocore.session.get_session().get_credentials() is not None
    except Exception:
        return False


# The recorded golden run, committed so a replay demo needs no AWS at all.
DEFAULT_CASSETTE = Path(__file__).resolve().parents[1] / "cassettes" / "golden-run.json"


def adapter_from_env() -> BedrockAdapter:
    """Live only when explicitly asked for AND credentials resolve; replay otherwise.

    Defaulting to replay means a missing credential is a degraded demo, not a
    crashed one.

    **And replay defaults to the committed cassette.** It was recorded and
    checked in precisely so the demo could run Bedrock's decisions offline, but
    nothing set `COLONY_BEDROCK_CASSETTE` — not the server, not `make demo` —
    so every replay ran with an empty cassette, every lookup missed, and every
    robot fell through to rules. A full mission logged 34 plans and all 34 read
    `source: rules`, which made "Claude decides at the boundaries" false on the
    one path anybody would actually watch.

    The env var still wins, so recording a fresh cassette or pointing at a
    variant is unchanged. This only supplies the file that is already there.
    """
    mode = os.environ.get("COLONY_BEDROCK_MODE", REPLAY)
    if mode in (LIVE, RECORD) and not has_credentials():
        mode = REPLAY
    cassette = os.environ.get("COLONY_BEDROCK_CASSETTE")
    path = Path(cassette) if cassette else (DEFAULT_CASSETTE if DEFAULT_CASSETTE.exists() else None)
    return BedrockAdapter(
        mode=mode,
        region=os.environ.get("AWS_REGION", "us-east-1"),
        cassette_path=path,
    )
