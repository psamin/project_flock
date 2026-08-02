"""Bedrock adapter (§4.3): embeddings, planning, and the offline path.

No AWS credentials are needed to run these — that is the point. Replay mode is
also what makes `--seeded` demo runs reproducible.
"""

import json

import pytest

from bedrock.adapter import (
    EMBED_DIMS,
    LIVE,
    RECORD,
    REPLAY,
    BedrockAdapter,
    Plan,
    adapter_from_env,
)


# --- embeddings --------------------------------------------------------------


def test_embedding_matches_the_schema_width():
    """VECTOR(512) in schema/v1_1.sql; a mismatch fails only at INSERT time."""
    assert len(BedrockAdapter().embed("victim under rubble")) == EMBED_DIMS


def test_embedding_is_deterministic():
    a, b = BedrockAdapter(), BedrockAdapter()
    assert a.embed("victim at 14,9") == b.embed("victim at 14,9")


def test_embedding_is_normalized():
    vec = BedrockAdapter().embed("fire spreading in sector C")
    assert abs(sum(x * x for x in vec) ** 0.5 - 1.0) < 1e-9


def test_different_text_gives_a_different_vector():
    adapter = BedrockAdapter()
    assert adapter.embed("victim under rubble") != adapter.embed("fire in the street")


def test_replay_mode_makes_no_network_calls():
    adapter = BedrockAdapter(mode=REPLAY)
    adapter.embed("anything")
    adapter.plan("scout", "nothing seen", [])
    assert adapter.calls == 0


# --- plan parsing ------------------------------------------------------------


def test_parses_strict_json():
    plan = Plan.parse('{"action":"claim_task","task_id":"abc","rationale":"closest"}')
    assert (plan.action, plan.task_id, plan.rationale) == (
        "claim_task",
        "abc",
        "closest",
    )


def test_parses_json_wrapped_in_prose_or_fences():
    """Models add preambles. A plan that fails to parse would stall a robot
    mid-mission, so the parser is deliberately forgiving."""
    plan = Plan.parse(
        'Sure!\n```json\n{"action":"explore","sector":"C"}\n```\nHope that helps.'
    )
    assert plan.action == "explore"
    assert plan.sector == "C"


def test_unparseable_response_degrades_to_exploring():
    for junk in ["", "I cannot help with that", "{not json at all"]:
        assert Plan.parse(junk).action == "explore"


# --- offline planning --------------------------------------------------------


def test_offline_plan_picks_the_highest_priority_task():
    tasks = [
        {"id": "t1", "kind": "explore", "priority": 1},
        {"id": "t2", "kind": "deliver_kit", "priority": 9},
    ]
    plan = BedrockAdapter().plan("medic", "one victim reachable", tasks)
    assert plan.action == "claim_task"
    assert plan.task_id == "t2"


def test_offline_plan_explores_when_there_is_nothing_to_do():
    assert BedrockAdapter().plan("scout", "nothing seen", []).action == "explore"


def test_equal_priority_tasks_plan_identically_regardless_of_input_order():
    """Regression: the prompt text is the cassette key, and open_tasks() orders
    by priority alone. Two equal-priority tasks arriving in either order gave two
    different prompts for the same mission state — a cassette miss, and a seeded
    run that no longer replays."""
    tasks = [
        {"id": "aaa", "kind": "clear_debris", "priority": 5},
        {"id": "bbb", "kind": "clear_debris", "priority": 5},
    ]
    forward = BedrockAdapter().plan("lifter", "debris everywhere", tasks)
    reversed_ = BedrockAdapter().plan(
        "lifter", "debris everywhere", list(reversed(tasks))
    )
    assert forward.task_id == reversed_.task_id


def test_cassette_key_is_order_independent(tmp_path):
    from bedrock.adapter import _key, _plan_prompt

    tasks = [
        {
            "id": "aaa",
            "kind": "clear_debris",
            "priority": 5,
            "target_x": 1,
            "target_y": 1,
        },
        {
            "id": "bbb",
            "kind": "clear_debris",
            "priority": 5,
            "target_x": 2,
            "target_y": 2,
        },
    ]
    ordered = sorted(tasks, key=lambda t: (-t["priority"], str(t["id"])))
    ordered_reversed = sorted(
        list(reversed(tasks)), key=lambda t: (-t["priority"], str(t["id"]))
    )
    assert _key("plan", _plan_prompt("lifter", "d", ordered)) == _key(
        "plan", _plan_prompt("lifter", "d", ordered_reversed)
    )


# --- resilience --------------------------------------------------------------


def test_a_throttled_bedrock_call_falls_back_instead_of_stalling_a_robot(monkeypatch):
    """§5.1 lane 2 requires a rule-based fallback. A ThrottlingException mid-
    mission must degrade the plan, not kill the agent loop."""
    pytest.importorskip("boto3", reason="AWS extra not installed")
    from botocore.exceptions import ClientError

    class Throttled:
        def invoke_model(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "InvokeModel",
            )

    monkeypatch.setattr("boto3.client", lambda *a, **k: Throttled())
    adapter = BedrockAdapter(mode=LIVE)

    plan = adapter.plan(
        "lifter", "debris", [{"id": "t1", "kind": "clear_debris", "priority": 5}]
    )
    assert plan.action == "claim_task" and plan.task_id == "t1"

    assert len(adapter.embed("victim under rubble")) == EMBED_DIMS


@pytest.mark.parametrize(
    "code",
    ["AccessDeniedException", "ValidationException", "ResourceNotFoundException"],
)
def test_a_misconfiguration_surfaces_instead_of_falling_back(monkeypatch, code):
    """Bedrock raises ClientError for a broken IAM policy, a revoked model-access
    grant and a typo'd modelId too. Swallowing those would leave the fleet
    running rule-based planning forever while looking healthy — and we would
    demo an AWS integration that never once called AWS."""
    pytest.importorskip("boto3", reason="AWS extra not installed")
    from botocore.exceptions import ClientError

    class Broken:
        def invoke_model(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": code, "Message": "nope"}}, "InvokeModel"
            )

    monkeypatch.setattr("boto3.client", lambda *a, **k: Broken())
    adapter = BedrockAdapter(mode=LIVE)

    with pytest.raises(ClientError):
        adapter.plan(
            "lifter", "debris", [{"id": "t1", "kind": "clear_debris", "priority": 5}]
        )
    with pytest.raises(ClientError):
        adapter.embed("victim under rubble")


def test_a_transport_failure_still_falls_back(monkeypatch):
    """Connection timeouts are weather, not misconfiguration."""
    pytest.importorskip("boto3", reason="AWS extra not installed")
    from botocore.exceptions import ConnectTimeoutError

    class Timeout:
        def invoke_model(self, **kwargs):
            raise ConnectTimeoutError(
                endpoint_url="https://bedrock-runtime.amazonaws.com"
            )

    monkeypatch.setattr("boto3.client", lambda *a, **k: Timeout())
    adapter = BedrockAdapter(mode=LIVE)
    assert adapter.plan("scout", "nothing", []).action == "explore"


def test_plan_always_carries_a_rationale():
    """Rationales are surfaced as thought bubbles (§3.6) — an empty one is a
    blank bubble in the demo video."""
    assert BedrockAdapter().plan("scout", "nothing", []).rationale
    assert (
        BedrockAdapter()
        .plan("lifter", "debris", [{"id": "t1", "kind": "clear_debris", "priority": 5}])
        .rationale
    )


# --- cassettes ---------------------------------------------------------------


def test_replay_prefers_the_cassette_over_the_offline_path(tmp_path):
    cassette = tmp_path / "run.json"
    from bedrock.adapter import _key

    recorded = [0.5] * EMBED_DIMS
    cassette.write_text(json.dumps({_key("embed", "victim at 14,9"): recorded}))

    adapter = BedrockAdapter(mode=REPLAY, cassette_path=cassette)
    assert adapter.embed("victim at 14,9") == recorded
    assert adapter.embed("something not recorded") != recorded  # falls back


def test_missing_credentials_downgrade_to_replay(monkeypatch):
    """A missing credential should mean a degraded demo, not a crashed one."""
    monkeypatch.setenv("COLONY_BEDROCK_MODE", LIVE)
    monkeypatch.setattr("bedrock.adapter.has_credentials", lambda: False)
    assert adapter_from_env().mode == REPLAY


def test_credentials_from_a_task_role_still_count(monkeypatch):
    """Regression: checking AWS_ACCESS_KEY_ID/AWS_PROFILE alone downgraded the
    ECS Fargate deployment (§4.6) to offline planning, since task-role
    credentials set neither variable."""
    pytest.importorskip("boto3", reason="AWS extra not installed")
    monkeypatch.setenv("COLONY_BEDROCK_MODE", LIVE)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr("bedrock.adapter.has_credentials", lambda: True)
    monkeypatch.setattr("boto3.client", lambda *a, **k: object())
    assert adapter_from_env().mode == LIVE


def test_explicit_mode_is_honoured_when_credentials_exist(monkeypatch):
    """Exercises the real credential resolution rather than stubbing it — an
    access key with no secret does not resolve, which is why this needs both."""
    pytest.importorskip("boto3", reason="AWS extra not installed")
    monkeypatch.setenv("COLONY_BEDROCK_MODE", RECORD)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setattr("boto3.client", lambda *a, **k: object())
    assert adapter_from_env().mode == RECORD


def test_live_mode_builds_a_bedrock_runtime_client(monkeypatch):
    """The one thing worth asserting about the live path without credentials:
    that it reaches for bedrock-runtime in the configured region."""
    pytest.importorskip("boto3", reason="AWS extra not installed")
    seen = {}

    def fake_client(service, region_name=None):
        seen["service"], seen["region"] = service, region_name
        return object()

    monkeypatch.setattr("boto3.client", fake_client)
    BedrockAdapter(mode=LIVE, region="us-west-2")
    assert seen == {"service": "bedrock-runtime", "region": "us-west-2"}
