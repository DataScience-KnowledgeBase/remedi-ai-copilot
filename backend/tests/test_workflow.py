import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import AgentStep, policy
from app.engine import Engine
from app.main import create_app
from app.providers import Completion, Provider, ProviderError
from app.store import Conflict, Store

OPERATOR = "operator-" + "x" * 40
OWNER = "owner-" + "y" * 40


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "test.db"),
        provider="demo",
        operator_token=OPERATOR,
        owner_token=OWNER,
    )


@pytest.fixture
def store(settings):
    instance = Store(settings.database_path)
    yield instance
    instance.close()


def create(store, scenario):
    return store.create(scenario, f"test-key-{scenario}", "demo", "fixture", 25)[0]


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("cache_pressure", "closed"),
        ("dynamic_cache", "closed"),
        ("dynamic_worker", "awaiting_approval"),
        ("dynamic_capacity", "awaiting_approval"),
        ("worker_stall", "awaiting_approval"),
        ("unknown_signal", "escalated"),
        ("verification_failure", "rolled_back"),
        ("prompt_injection", "escalated"),
    ],
)
async def test_workflows(settings, store, scenario, expected):
    engine = Engine(store, settings, Provider(settings))
    job = create(store, scenario)
    await engine.process(job)
    assert store.get(job["id"])["status"] == expected
    events = store.events(job["id"])
    adk = [event["payload"] for event in events if event["stage"] == "adk"]
    assert adk
    assert any("inspect_metrics" in event["function_calls"] for event in adk)
    if scenario.startswith("dynamic"):
        assert sum(event["stage"] == "judge" for event in events) == 1
        assert any(event["agent"] == "resolution_reviewer" for event in adk)
        assert len({event["invocation_id"] for event in adk}) == 2
    assert len([event for event in events if event["stage"] == "reason"]) <= 4
    if expected in {"closed", "rolled_back"}:
        check = next(event for event in events if event["stage"] in {"close", "rollback"})
        assert len(check["payload"]["samples"]) == 3
        assert check["payload"]["healthy"] == (expected == "closed")
        assert store.feedback()[0]["outcome"] == expected
    if expected == "awaiting_approval":
        assert not any(event["stage"] == "execute" for event in events)
        store.transition(
            job["id"],
            {"awaiting_approval"},
            "approved",
            "approval",
            "Approved in test.",
            actor="owner",
        )
        await engine.process(store.get(job["id"]))
        assert store.get(job["id"])["status"] == "closed"
        with pytest.raises(Conflict):
            store.transition(job["id"], {"awaiting_approval"}, "approved", "approval", "Replay")


def test_policy_rejects_fabricated_evidence_and_wrong_actions():
    plan = AgentStep(
        tool="propose",
        action="clear_cache",
        confidence=0.99,
        evidence_ids=["EV-METRICS-01", "RB-CACHE-01", "fake"],
        summary="Model invented a citation",
    )
    assert policy("cache_pressure", plan, {"EV-METRICS-01", "RB-CACHE-01"})[0] == "escalated"
    plan.action = "restart_worker"
    assert policy("cache_pressure", plan, set())[0] == "escalated"


def test_dynamic_action_is_not_selected_by_offline_labels():
    from app.domain import RUNBOOKS, evidence
    from app.providers import phase_schema

    metrics = evidence("dynamic_cache")
    assert "expected_action" not in metrics
    schema = phase_schema({"observations": [metrics]})
    assert set(schema["properties"]["action"]["enum"]) == set(RUNBOOKS)
    for action, book in RUNBOOKS.items():
        cited = {"EV-METRICS-01", book["id"]}
        plan = AgentStep(
            tool="propose",
            action=action,
            confidence=0.99,
            evidence_ids=sorted(cited),
            summary="A dynamic proposal assessed for risk, not a label.",
        )
        assert policy("dynamic_cache", plan, cited)[0] in {"ready", "awaiting_approval"}
    final = phase_schema({"observations": [metrics, RUNBOOKS["clear_cache"]]})
    assert final["properties"]["evidence_ids"]["minItems"] == 2
    assert set(final["properties"]["evidence_ids"]["items"]["enum"]) == {
        "EV-METRICS-01",
        "RB-CACHE-01",
    }


@pytest.mark.parametrize("mode", ["disagree", "uncertain", "failure"])
async def test_dynamic_review_is_a_separate_fail_closed_gate(settings, store, monkeypatch, mode):
    provider = Provider(settings)
    original = provider.complete

    async def review(context):
        assert "expected_action" not in json.dumps(context)
        if context.get("review_mode") and mode == "failure":
            raise ProviderError("Reviewer unavailable")
        result = await original(context)
        if context.get("review_mode"):
            if mode == "disagree":
                result.step.action = "escalate"
            else:
                result.step.confidence = 0.2
        return result

    monkeypatch.setattr(provider, "complete", review)
    job = create(store, "dynamic_cache")
    await Engine(store, settings, provider).process(job)
    assert store.get(job["id"])["status"] == (
        "failed" if mode == "failure" else "awaiting_approval"
    )
    assert not any(e["stage"] == "execute" for e in store.events(job["id"]))
    if mode != "failure":
        store.transition(
            job["id"],
            {"awaiting_approval"},
            "approved",
            "approval",
            "Owner reviewed the disagreement and exact plan.",
            actor="owner",
        )
        await Engine(store, settings, provider).process(store.get(job["id"]))
        assert store.get(job["id"])["status"] == "closed"


async def test_dynamic_execution_requires_persisted_review(settings, store):
    from app.domain import RUNBOOKS, evidence

    job = create(store, "dynamic_cache")
    plan = AgentStep(
        tool="propose",
        action="clear_cache",
        confidence=0.99,
        evidence_ids=["EV-METRICS-01", "RB-CACHE-01"],
        summary="A cited but unreviewed plan.",
    )
    for observation in [evidence("dynamic_cache"), RUNBOOKS["clear_cache"]]:
        store.event(job["id"], "observe", "Test evidence", {"observation": observation})
    store.transition(
        job["id"], {"queued"}, "ready", "decision", "Bypass attempt", plan=plan.model_dump()
    )
    with pytest.raises(ProviderError, match="Independent review"):
        await Engine(store, settings, Provider(settings)).execute(job["id"])
    assert not any(e["stage"] == "execute" for e in store.events(job["id"]))


def test_store_idempotency_capacity_and_audit(store):
    one = create(store, "cache_pressure")
    two, created = store.create("cache_pressure", "test-key-cache_pressure", "demo", "fixture", 25)
    assert two == one and not created
    with pytest.raises(Conflict):
        store.create("worker_stall", "test-key-cache_pressure", "demo", "fixture", 25)
    with pytest.raises(Conflict):
        store.create("worker_stall", "different-key", "demo", "fixture", 1)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.db.execute("DELETE FROM events")
    store.event(
        one["id"],
        "test",
        "email a@example.com",
        {"nested": {"secret": "Bearer abcdef"}, "text": 'token=abc "value"'},
    )
    event = store.events(one["id"])[-1]
    assert "[EMAIL]" in event["message"]
    assert event["payload"]["nested"]["secret"] == "[SECRET]"
    assert isinstance(event["payload"], dict)


def test_crash_recovery_is_not_blind_replay(settings, store):
    job = create(store, "cache_pressure")
    store.transition(job["id"], {"queued"}, "triaging", "triage", "Started")
    store.recover()
    assert store.get(job["id"])["status"] == "failed"
    assert store.next_job() is None


def test_api_auth_validation_approval_and_replay(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/incidents").status_code == 401
        operator = {"Authorization": f"Bearer {OPERATOR}", "Idempotency-Key": "api-key-1234"}
        owner = {"Authorization": f"Bearer {OWNER}"}
        assert (
            client.post("/api/incidents", json={"scenario": "shell"}, headers=operator).status_code
            == 422
        )
        assert (
            client.post(
                "/api/incidents",
                json={"scenario": "worker_stall", "command": "x"},
                headers=operator,
            ).status_code
            == 422
        )
        response = client.post(
            "/api/incidents", json={"scenario": "worker_stall"}, headers=operator
        )
        assert response.status_code == 202
        incident_id = response.json()["incident"]["id"]
        assert (
            client.post(
                "/api/incidents", json={"scenario": "worker_stall"}, headers=operator
            ).json()["created"]
            is False
        )
        for _ in range(100):
            item = client.get(f"/api/incidents/{incident_id}", headers=operator).json()
            if item["status"] == "awaiting_approval":
                break
            import time

            time.sleep(0.02)
        assert item["status"] == "awaiting_approval"
        body = {"decision": "approve", "reason": "Reviewed exact runbook and impact."}
        path = f"/api/incidents/{incident_id}/approval"
        assert client.post(path, json=body, headers=operator).status_code == 403
        assert client.post(path, json=body, headers=owner).status_code == 200
        assert client.post(path, json=body, headers=owner).status_code == 409
        assert "operator_token" not in client.get("/api/config", headers=owner).text
        assert client.get("/api/metrics", headers=owner).status_code == 200
        assert client.get("/readyz").status_code == 200


async def test_provider_failure_is_visible(settings, store, monkeypatch):
    async def fail(_context):
        raise ProviderError("Unavailable model")

    provider = Provider(settings)
    monkeypatch.setattr(provider, "complete", fail)
    job = create(store, "cache_pressure")
    await Engine(store, settings, provider).process(job)
    assert store.get(job["id"])["status"] == "failed"
    assert store.get(job["id"])["error"] == "Unavailable model"
    assert not any(event["stage"] == "execute" for event in store.events(job["id"]))


async def test_low_confidence_requires_approval(settings, store, monkeypatch):
    provider = Provider(settings)
    original = provider.complete

    async def uncertain(context):
        result = await original(context)
        result.step.confidence = 0.4
        return result

    monkeypatch.setattr(provider, "complete", uncertain)
    job = create(store, "cache_pressure")
    await Engine(store, settings, provider).process(job)
    assert store.get(job["id"])["status"] == "awaiting_approval"


async def test_step_budget_is_enforced(settings, store, monkeypatch):
    provider = Provider(settings)

    async def loop(_context):
        return Completion(
            AgentStep(
                tool="inspect_metrics",
                action="escalate",
                confidence=1,
                evidence_ids=[],
                summary="Read again",
            ),
            1,
            1,
            1,
            0,
            1,
        )

    monkeypatch.setattr(provider, "complete", loop)
    job = create(store, "cache_pressure")
    await Engine(store, settings, provider).process(job)
    assert store.get(job["id"])["status"] == "failed"
    assert len([e for e in store.events(job["id"]) if e["stage"] == "reason"]) == 4


def test_api_egress_requires_explicit_opt_in():
    with pytest.raises(ValueError, match="explicit"):
        Settings(_env_file=None, operator_token=OPERATOR, owner_token=OWNER, provider="api")


async def test_ollama_contract(settings, monkeypatch):
    settings.provider = "ollama"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "tool": "inspect_metrics",
                            "action": "escalate",
                            "confidence": 0.9,
                            "evidence_ids": [],
                            "summary": "Inspect metrics first",
                        }
                    )
                },
                "prompt_eval_count": 100,
                "eval_count": 40,
            },
        )
    )
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=transport)
    )
    result = await Provider(settings).complete({"observations": [], "synthetic": True})
    assert result.input_tokens == 100 and result.output_tokens == 40
    assert result.latency_ms > 0
