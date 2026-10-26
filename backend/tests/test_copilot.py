import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from copilot.client import GetAuthStatusResponse
from copilot.session_events import AssistantMessageData, AssistantUsageData
from fastapi.testclient import TestClient

from app.config import Settings
from app.copilot_provider import CopilotInference, deny_permission, transport_schema
from app.main import create_app
from app.providers import Provider, ProviderError, completion_metadata, phase_schema
from tests.test_workflow import OPERATOR, OWNER

CONTEXT = {"synthetic": True, "service": "checkout", "step": 1, "observations": []}
STEP = {
    "tool": "inspect_metrics",
    "action": "escalate",
    "confidence": 0.95,
    "evidence_ids": [],
    "summary": "Inspect trusted synthetic metrics.",
}


@pytest.fixture
def config(tmp_path):
    return Settings(
        _env_file=None,
        operator_token=OPERATOR,
        owner_token=OWNER,
        provider="copilot",
        copilot_allow_egress=True,
        copilot_home=str(tmp_path / "home"),
        copilot_work_directory=str(tmp_path / "empty"),
        database_path=str(tmp_path / "test.db"),
        frontend_dist=str(tmp_path / "absent"),
        llm_timeout_seconds=1,
    )


@pytest.fixture
def sdk(monkeypatch):
    session = SimpleNamespace(
        send_and_wait=AsyncMock(
            return_value=SimpleNamespace(
                data=AssistantMessageData(content=json.dumps(STEP), message_id="test-message"),
            )
        ),
        abort=AsyncMock(),
        disconnect=AsyncMock(),
    )
    client = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        get_auth_status=AsyncMock(return_value=GetAuthStatusResponse(isAuthenticated=True)),
        list_models=AsyncMock(return_value=[SimpleNamespace(id="gpt-4.1")]),
        create_session=AsyncMock(return_value=session),
    )
    options = {}

    def factory(**kwargs):
        options.update(kwargs)
        return client

    monkeypatch.setattr("app.copilot_provider.CopilotClient", factory)
    return client, session, options


def test_egress_is_explicit():
    with pytest.raises(ValueError, match="COPILOT_ALLOW_EGRESS"):
        Settings(_env_file=None, operator_token=OPERATOR, owner_token=OWNER)


async def test_subscription_contract_denies_ambient_capabilities(config, sdk):
    client, session, options = sdk
    provider = Provider(config)
    try:
        result = await provider.complete(CONTEXT)
        assert result.step.tool == "inspect_metrics"
        assert options["mode"] == "empty"
        assert options["use_logged_in_user"] is True
        assert options["github_token"] is None
        call = client.create_session.call_args.kwargs
        assert call["available_tools"] == [] and call["tools"] == []
        for flag in [
            "enable_config_discovery",
            "enable_skills",
            "enable_file_hooks",
            "enable_host_git_operations",
            "enable_session_store",
            "enable_on_demand_instruction_discovery",
        ]:
            assert call[flag] is False
        assert call["mcp_servers"] == {} and call["custom_agents"] == []
        assert call["skip_custom_instructions"] is True
        assert deny_permission(None, {}).kind == "denied-by-rules"
        assert session.send_and_wait.call_args.kwargs["response_schema"] == phase_schema(CONTEXT)
        assert result.input_tokens is None and result.output_tokens is None
        assert result.estimated_usd is None
        metadata = completion_metadata(result, provider)
        assert metadata["usage_reported"] is False
        assert metadata["cost_basis"] == "copilot_subscription_not_usd"
        session.abort.assert_awaited_once()
        session.disconnect.assert_awaited_once()
    finally:
        await provider.close()
    client.stop.assert_awaited_once()


async def test_actual_usage_events_not_guessed(config, sdk):
    client, _, _ = sdk
    original = client.create_session.side_effect

    async def create(**kwargs):
        kwargs["on_event"](
            SimpleNamespace(
                data=AssistantUsageData(
                    model="gpt-4.1",
                    input_tokens=123,
                    output_tokens=45,
                )
            )
        )
        return client.create_session.return_value

    client.create_session.side_effect = create
    provider = Provider(config)
    try:
        result = await provider.complete(CONTEXT)
        assert result.input_tokens == 123 and result.output_tokens == 45
        assert result.usage_events == 1 and result.estimated_usd is None
    finally:
        client.create_session.side_effect = original
        await provider.close()


async def test_signed_out_is_fail_closed_without_inference(config, sdk):
    client, session, _ = sdk
    client.get_auth_status.return_value.isAuthenticated = False
    provider = Provider(config)
    try:
        with pytest.raises(ProviderError):
            await provider.complete(CONTEXT)
        client.create_session.assert_not_awaited()
        session.send_and_wait.assert_not_awaited()
    finally:
        await provider.close()


async def test_unavailable_model_is_not_silently_substituted(config, sdk):
    client, _, _ = sdk
    client.list_models.return_value = [SimpleNamespace(id="not-the-configured-model")]
    inference = CopilotInference(config)
    try:
        with pytest.raises(ProviderError, match="unavailable"):
            await inference.ready()
        client.create_session.assert_not_awaited()
    finally:
        await inference.close()


async def test_phase_rejection_and_timeout_cleanup(config, sdk):
    _, session, _ = sdk
    session.send_and_wait.return_value.data.content = json.dumps({**STEP, "tool": "propose"})
    provider = Provider(config)
    try:
        with pytest.raises(ProviderError):
            await provider.complete(CONTEXT)
        session.abort.assert_awaited_once()
        session.disconnect.assert_awaited_once()
        session.send_and_wait.side_effect = TimeoutError("synthetic timeout")
        with pytest.raises(ProviderError):
            await provider.complete(CONTEXT)
        assert session.send_and_wait.await_count == 2  # No automatic paid-call retries.
        assert session.abort.await_count == 2
    finally:
        await provider.close()


def test_claude_transport_schema_preserves_local_contract_and_phase_enums():
    schema = phase_schema(CONTEXT)
    original = deepcopy(schema)
    adapted = transport_schema(schema, "claude-sonnet-5")
    assert schema == original
    assert adapted["required"] == schema["required"]
    assert adapted["additionalProperties"] is False
    assert adapted["properties"]["tool"] == schema["properties"]["tool"]
    assert adapted["properties"]["action"] == schema["properties"]["action"]
    assert "maximum" not in adapted["properties"]["confidence"]
    assert "minimum" not in adapted["properties"]["confidence"]
    assert "maxLength" not in adapted["properties"]["summary"]
    assert "minLength" not in adapted["properties"]["summary"]
    assert "maxItems" not in adapted["properties"]["evidence_ids"]
    assert "Locally enforced constraints" in adapted["properties"]["confidence"]["description"]
    assert transport_schema(schema, "gpt-4.1") == original


def test_claude_proposal_citation_minimum_is_transport_only():
    context = {
        **CONTEXT,
        "observations": [{"id": "EV-METRICS-01"}, {"id": "RB-CACHE-01"}],
    }
    schema = phase_schema(context)
    assert schema["properties"]["evidence_ids"]["minItems"] == 2
    adapted = transport_schema(schema, "claude-sonnet-5")
    assert "minItems" not in adapted["properties"]["evidence_ids"]
    assert schema["properties"]["evidence_ids"]["minItems"] == 2
    assert adapted["properties"]["evidence_ids"]["items"] == (
        schema["properties"]["evidence_ids"]["items"]
    )
    schema["properties"]["evidence_ids"]["minItems"] = 1
    adapted = transport_schema(schema, "claude-sonnet-5")
    assert adapted["properties"]["evidence_ids"]["minItems"] == 1


@pytest.mark.parametrize(
    "citations",
    [[], ["EV-METRICS-01"], ["EV-METRICS-01", "unobserved"]],
)
async def test_claude_proposal_enforces_original_phase_citations(config, sdk, citations):
    client, session, _ = sdk
    config.copilot_model = "claude-sonnet-5"
    client.list_models.return_value = [SimpleNamespace(id=config.copilot_model)]
    context = {
        **CONTEXT,
        "observations": [{"id": "EV-METRICS-01"}, {"id": "RB-CACHE-01"}],
    }
    session.send_and_wait.return_value.data.content = json.dumps({
        **STEP,
        "tool": "propose",
        "action": "clear_cache",
        "evidence_ids": citations,
    })
    provider = Provider(config)
    try:
        with pytest.raises(ProviderError, match="workflow schema"):
            await provider.complete(context)
        session.send_and_wait.assert_awaited_once()
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "invalid",
    [
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"summary": ""},
        {"summary": "x" * 601},
        {"evidence_ids": ["id"] * 6},
        {"tool": "propose"},
        {"action": "delete_service"},
    ],
)
async def test_claude_responses_still_fail_original_validation(config, sdk, invalid):
    client, session, _ = sdk
    config.copilot_model = "claude-sonnet-5"
    client.list_models.return_value = [SimpleNamespace(id=config.copilot_model)]
    session.send_and_wait.return_value.data.content = json.dumps({**STEP, **invalid})
    provider = Provider(config)
    try:
        with pytest.raises(ProviderError):
            await provider.complete(CONTEXT)
        session.send_and_wait.assert_awaited_once()
        assert session.send_and_wait.call_args.kwargs["response_schema"] == transport_schema(
            phase_schema(CONTEXT), config.copilot_model
        )
        session.abort.assert_awaited_once()
        session.disconnect.assert_awaited_once()
    finally:
        await provider.close()


def test_auth_required_status_visible_and_no_queue_admission(config, sdk):
    client, _, _ = sdk
    client.get_auth_status.return_value.isAuthenticated = False
    with TestClient(create_app(config)) as app:
        headers = {"Authorization": f"Bearer {OPERATOR}", "Idempotency-Key": "auth-test-key"}
        assert app.get("/healthz").status_code == 200
        assert app.get("/readyz").status_code == 503
        status = app.get("/api/provider-status", headers=headers)
        assert status.status_code == 503
        assert "sign-in required" in status.json()["message"]
        assert (
            app.post(
                "/api/incidents", json={"scenario": "cache_pressure"}, headers=headers
            ).status_code
            == 503
        )
        assert app.get("/api/incidents", headers=headers).json() == []
        assert app.get("/api/telemetry", headers=headers).json()["estimated_usd"] is None
        assert (
            app.get("/api/config", headers=headers).json()["billing_basis"]
            == "copilot_subscription"
        )
    client.stop.assert_awaited_once()
