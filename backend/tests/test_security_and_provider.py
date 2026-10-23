import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import ApprovalInput, redact
from app.main import create_app
from app.providers import Provider, ProviderError
from tests.test_workflow import OPERATOR, OWNER


def test_rate_limit_and_untrusted_input(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "rate.db"),
        provider="demo",
        operator_token=OPERATOR,
        owner_token=OWNER,
        requests_per_minute=2,
    )
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {OPERATOR}"}
        assert client.get("/api/config", headers=headers).status_code == 200
        assert client.get("/api/config", headers=headers).status_code == 200
        assert client.get("/api/config", headers=headers).status_code == 429
        assert (
            client.get("/api/config", headers={"Authorization": "Bearer invalid"}).status_code
            == 401
        )


def test_secret_redaction_and_invalid_config():
    assert "a@example.com" not in redact("Customer a@example.com token=supersecret")
    assert "supersecret" not in redact("token=supersecret")
    with pytest.raises(ValueError, match="Distinct"):
        Settings(_env_file=None, operator_token=OPERATOR, owner_token=OPERATOR)
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(
            _env_file=None,
            operator_token=OPERATOR,
            owner_token=OWNER,
            provider="demo",
            api_base_url="http://example.com",
        )
    with pytest.raises(ValueError):
        ApprovalInput(decision="approve", reason=" " * 20)


async def test_api_contract_uses_schema_and_records_usage(monkeypatch):
    settings = Settings(
        _env_file=None,
        operator_token=OPERATOR,
        owner_token=OWNER,
        provider="api",
        api_key="synthetic-test-key",
        allow_api_egress=True,
        input_usd_per_million=1,
        output_usd_per_million=2,
    )

    def respond(request):
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"] is True
        assert request.headers["Authorization"] == "Bearer synthetic-test-key"
        assert json.loads(body["messages"][1]["content"])["synthetic"] is True
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "tool": "inspect_metrics",
                                    "action": "escalate",
                                    "confidence": 0.9,
                                    "evidence_ids": [],
                                    "summary": "Inspect metrics before deciding.",
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)),
    )
    result = await Provider(settings).complete({"synthetic": True, "observations": []})
    assert result.estimated_usd == pytest.approx(0.0002)
    assert result.input_tokens == 100


async def test_invalid_model_output_opens_circuit(monkeypatch):
    settings = Settings(
        _env_file=None, operator_token=OPERATOR, owner_token=OWNER, provider="ollama"
    )
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "message": {"content": '{"tool":"execute_shell"}'},
                        "prompt_eval_count": 10,
                        "eval_count": 5,
                    },
                )
            ),
        ),
    )
    provider = Provider(settings)
    for _ in range(3):
        with pytest.raises(ProviderError, match="inference failed"):
            await provider.complete({"synthetic": True})
    with pytest.raises(ProviderError, match="circuit"):
        await provider.complete({"synthetic": True})
