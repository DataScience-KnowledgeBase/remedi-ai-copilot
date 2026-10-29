import ssl
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import KUBE_RUNBOOKS, KUBE_TARGETS, RUNBOOKS, AgentStep, policy
from app.engine import Engine
from app.kubernetes import KubernetesAdapter, template_hash
from app.main import create_app
from app.providers import Completion, Provider, ProviderError, phase_schema
from app.store import Conflict, Store


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        provider="ollama",
        operator_token="o" * 40,
        owner_token="w" * 40,
        database_path=str(tmp_path / "kubernetes.db"),
        kubernetes_enabled=True,
        kubernetes_rollout_timeout_seconds=10,
        kubernetes_probe_interval_seconds=0.1,
    )


def deployment(scenario="k8s_crashloop"):
    name = KUBE_TARGETS[scenario]
    return {
        "metadata": {
            "name": name,
            "namespace": "remedi-demo",
            "uid": "deployment-uid",
            "resourceVersion": "12",
            "generation": 2,
            "labels": {"app.kubernetes.io/managed-by": "remedi-ai", "remedi.demo/target": name},
            "annotations": {"deployment.kubernetes.io/revision": "2"},
        },
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"labels": {"remedi.demo/target": name}},
                "spec": {
                    "containers": [
                        {
                            "name": "demo",
                            "image": "python:remedi-missing-demo"
                            if scenario == "k8s_imagepull"
                            else "python:3.12-alpine",
                            "env": [{"name": "CRASH", "value": "true"}],
                            "resources": {"limits": {"memory": "32Mi"}},
                        }
                    ],
                    "nodeSelector": {"remedi.demo/unavailable": "true"},
                },
            },
        },
    }


def observation(document):
    return {
        "id": "EV-METRICS-01",
        "source": "kubernetes-api-v1",
        "synthetic": False,
        "diagnostic_mode": True,
        "baseline": {
            "uid": document["metadata"]["uid"],
            "template_hash": template_hash(document["spec"]["template"]),
        },
        "revision": "2",
        "rollout": {
            "observedGeneration": 2,
            "replicas": 1,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "unavailableReplicas": 0,
        },
        "pods": [
            {
                "name": "demo-pod",
                "uid": "pod-uid",
                "deleting": False,
                "revision": "2",
                "phase": "Running",
                "conditions": [
                    {"type": "Ready", "status": "True", "reason": None},
                    {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"},
                ],
                "containers": [
                    {
                        "name": "demo",
                        "ready": True,
                        "restart_count": 0,
                        "state": {"running": {"startedAt": "now"}},
                        "last_state": {"terminated": {"reason": "OOMKilled"}},
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    ("scenario", "action", "path", "after"),
    [
        ("k8s_crashloop", "fix_pod_startup", "/env/0/value", "false"),
        ("k8s_oom", "increase_pod_memory", "/resources/limits/memory", "128Mi"),
        ("k8s_imagepull", "restore_pod_image", "/image", "python:3.12-alpine"),
        ("k8s_pending", "fix_pod_scheduling", "/nodeSelector/remedi.demo~1unavailable", None),
    ],
)
async def test_bounded_plans_require_live_evidence(
    settings, monkeypatch, scenario, action, path, after
):
    adapter = KubernetesAdapter(settings)
    document = deployment(scenario)

    async def get(_scenario):
        return document

    monkeypatch.setattr(adapter, "deployment", get)
    observed = observation(document)
    observed["pods"][0]["containers"][0]["state"] = {
        "waiting": {
            "reason": {
                "k8s_crashloop": "CrashLoopBackOff",
                "k8s_oom": "CrashLoopBackOff",
                "k8s_imagepull": "ImagePullBackOff",
                "k8s_pending": "ContainerCreating",
            }[scenario]
        }
    }
    plan = await adapter.prepare(scenario, action, observed)
    assert plan["path"].endswith(path)
    assert plan["after"] == after
    observed["pods"] = []
    with pytest.raises(ProviderError):
        await adapter.prepare(scenario, action, observed)
    observed["baseline"]["uid"] = "replacement-uid"
    with pytest.raises(ProviderError, match="changed during diagnosis"):
        await adapter.prepare(scenario, action, observed)


async def test_patch_is_cas_guarded_and_stale_approval_is_not_replayed(settings, monkeypatch):
    adapter = KubernetesAdapter(settings)
    document = deployment()
    observed = observation(document)
    observed["pods"][0]["containers"][0]["last_state"] = {"terminated": {"reason": "Error"}}
    writes = []

    async def request(method, path, body=None):
        assert path.endswith("/deployments/remedi-crashloop")
        if method == "PATCH":
            writes.append(body)
        return document

    monkeypatch.setattr(adapter, "request", request)
    plan = await adapter.prepare("k8s_crashloop", "fix_pod_startup", observed)
    await adapter.change("k8s_crashloop", plan)
    assert writes[0][:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "deployment-uid"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "12"},
    ]
    assert writes[0][2] == {
        "op": "replace",
        "path": "/spec/template/spec/containers/0/env/0/value",
        "value": "false",
    }
    document["spec"]["template"]["spec"]["containers"][0]["image"] = "externally-changed"
    with pytest.raises(ProviderError, match="Stale"):
        await adapter.change("k8s_crashloop", plan)
    assert len(writes) == 1


def test_policy_always_requires_owner_and_never_uses_scenario_action_label():
    for scenario in KUBE_TARGETS:
        for action, book in KUBE_RUNBOOKS.items():
            cited = {"EV-METRICS-01", book["id"]}
            plan = AgentStep(
                tool="propose",
                action=action,
                confidence=0.99,
                evidence_ids=sorted(cited),
                summary="An AI diagnosis based on actual pod evidence.",
            )
            assert policy(scenario, plan, cited)[0] == "awaiting_approval"
            assert policy(scenario, plan, {"EV-METRICS-01"})[0] == "escalated"
            assert policy("dynamic_cache", plan, cited)[0] == "escalated"
        for action in RUNBOOKS:
            plan.action = action
            assert policy(scenario, plan, set())[0] == "escalated"
    schema = phase_schema({"synthetic": False, "observations": [observation(deployment())]})
    assert set(schema["properties"]["action"]["enum"]) == {*KUBE_RUNBOOKS, "escalate"}
    assert set(schema["properties"]["tool"]["enum"]) == {"read_runbook", "propose"}


async def test_real_diagnosis_stops_at_owner_gate(settings, monkeypatch):
    store = Store(settings.database_path)
    provider = Provider(settings)
    engine = Engine(store, settings, provider)
    observed = observation(deployment())
    observed["pods"][0]["containers"][0]["last_state"] = {"terminated": {"reason": "Error"}}
    calls = []

    async def inspect(_scenario):
        return observed

    async def get(_scenario):
        return deployment()

    async def complete(context):
        calls.append(context)
        assert context["synthetic"] is False
        assert "expected_action" not in str(context)
        items = context["observations"]
        tool = "inspect_metrics" if not items else "read_runbook" if len(items) == 1 else "propose"
        if context.get("review_mode"):
            tool = "propose"
        return Completion(
            AgentStep(
                tool=tool,
                action="fix_pod_startup",
                confidence=0.95,
                evidence_ids=[item["id"] for item in items],
                summary="Startup failure evidence.",
            ),
            1,
            10,
            10,
            None,
            1,
        )

    async def forbidden(*args, **kwargs):
        pytest.fail("No Kubernetes patch may run before owner approval")

    monkeypatch.setattr(provider, "complete", complete)
    monkeypatch.setattr(engine.kubernetes, "observe", inspect)
    monkeypatch.setattr(engine.kubernetes, "deployment", get)
    monkeypatch.setattr(engine.kubernetes, "change", forbidden)
    job, _ = store.create("k8s_crashloop", "real-demo-key", "ollama", "test", 25)
    try:
        await engine.process(job)
        assert store.get(job["id"])["status"] == "awaiting_approval"
        assert len(calls) == 4 and calls[-1]["review_mode"]
        events = store.events(job["id"])
        assert any(event["stage"] == "kubernetes_plan" for event in events)
        assert not any(event["stage"] == "execute" for event in events)
        with pytest.raises(ProviderError, match="approval"):
            await engine.execute(job["id"])
        with pytest.raises(Conflict, match="already has an active"):
            store.create("k8s_crashloop", "another-key", "ollama", "test", 25)
    finally:
        store.close()


async def test_live_verification_reports_measured_consecutive_samples(settings, monkeypatch):
    adapter = KubernetesAdapter(settings)
    document = deployment()
    observed = observation(document)
    count, events = 0, []

    async def inspect(_scenario):
        nonlocal count
        count += 1
        sample = deepcopy(observed)
        if count in {1, 3}:
            sample["pods"][0]["containers"][0]["restart_count"] = 1
        return sample

    monkeypatch.setattr(adapter, "observe", inspect)
    assert await adapter.verify("k8s_crashloop", document, lambda *event: events.append(event))
    assert [event[2]["consecutive_passes"] for event in events] == [0, 1, 0, 1, 2, 3]
    observed["rollout"]["observedGeneration"] = 1
    assert not adapter.healthy(observed, 2)
    observed["rollout"]["observedGeneration"] = 2
    observed["pods"][0]["revision"] = "old"
    assert not adapter.healthy(observed, 2)


def test_disabled_integration_rejects_live_scenarios(tmp_path):
    settings = Settings(
        _env_file=None,
        provider="demo",
        operator_token="o" * 40,
        owner_token="w" * 40,
        database_path=str(tmp_path / "disabled.db"),
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/incidents",
            json={"scenario": "k8s_oom"},
            headers={"Authorization": "Bearer " + "o" * 40, "Idempotency-Key": "disabled-demo"},
        )
        assert response.status_code == 409
        assert not client.get(
            "/api/config",
            headers={"Authorization": "Bearer " + "o" * 40},
        ).json()["kubernetes"]["enabled"]


@pytest.mark.parametrize("namespace", ["default", "kube-system", "remedi-x/../../default"])
def test_namespace_scope_is_not_arbitrary(settings, namespace):
    with pytest.raises(ValueError, match="namespace"):
        Settings(**{**settings.model_dump(), "kubernetes_namespace": namespace}, _env_file=None)


def test_real_integration_cannot_use_deterministic_fixture(settings):
    with pytest.raises(ValueError, match="real inference"):
        Settings(**{**settings.model_dump(), "provider": "demo"}, _env_file=None)


@pytest.mark.parametrize("outcome", ["closed", "rolled_back", "failed"])
async def test_approved_execution_and_rollback_are_bounded(settings, monkeypatch, outcome):
    store = Store(settings.database_path)
    engine = Engine(store, settings, Provider(settings))
    document = deployment()
    observed = observation(document)
    observed["pods"][0]["containers"][0]["last_state"] = {"terminated": {"reason": "Error"}}

    async def get(_scenario):
        return document

    monkeypatch.setattr(engine.kubernetes, "deployment", get)
    change = await engine.kubernetes.prepare("k8s_crashloop", "fix_pod_startup", observed)
    plan = AgentStep(
        tool="propose",
        action="fix_pod_startup",
        confidence=0.95,
        evidence_ids=["EV-METRICS-01", "RB-K8S-STARTUP-01"],
        summary="Observed startup failure.",
    )
    job, _ = store.create("k8s_crashloop", "approved-test-key", "ollama", "test", 25)
    for item in [observed, KUBE_RUNBOOKS["fix_pod_startup"]]:
        store.event(job["id"], "observe", "Observed", {"observation": item})
    store.event(job["id"], "kubernetes_plan", "Checkpoint", {"change": change})
    store.transition(
        job["id"],
        {"queued"},
        "awaiting_approval",
        "decision",
        "Owner required",
        plan=plan.model_dump(),
    )
    store.transition(
        job["id"],
        {"awaiting_approval"},
        "approved",
        "approval",
        "Reviewed exact checkpoint",
        actor="owner",
        payload={"decision": "approve"},
    )
    writes = []

    async def apply(scenario, approved, *, applied_hash=None):
        assert scenario == "k8s_crashloop" and approved == change
        writes.append(applied_hash)
        result = deepcopy(document)
        if applied_hash is None:
            result["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "false"
            result["metadata"]["generation"] = 3
        return result

    async def verify(scenario, applied, record):
        if outcome == "failed":
            raise ProviderError("Connection lost after patch; inspect cluster")
        for count in range(1, 4):
            record("progress", "Measured sample", {"consecutive_passes": count})
        return outcome == "closed"

    monkeypatch.setattr(engine.kubernetes, "change", apply)
    monkeypatch.setattr(engine.kubernetes, "verify", verify)
    try:
        await engine.process(store.get(job["id"]))
        assert store.get(job["id"])["status"] == outcome
        assert len(writes) == (2 if outcome == "rolled_back" else 1)
        events = store.events(job["id"])
        if outcome == "rolled_back":
            rollback = next(event for event in events if event["stage"] == "rollback")
            assert rollback["payload"]["configuration_restored"]
            assert not rollback["payload"]["recovery_verified"]
        if outcome == "failed":
            assert "a change may have applied" in events[-1]["message"]
    finally:
        store.close()


async def test_collection_uses_owner_uids_and_does_not_export_environment(settings, monkeypatch):
    adapter = KubernetesAdapter(settings)
    document = deployment()
    document["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "PRIVATE_TEST_VALUE", "value": "never-export-this"}
    )
    own = {
        "metadata": {
            "name": "owned-pod",
            "uid": "own-pod-uid",
            "ownerReferences": [{"uid": "owned-rs", "controller": True}],
        },
        "status": {},
    }
    foreign = deepcopy(own)
    foreign["metadata"]["ownerReferences"][0]["uid"] = "foreign-rs"
    foreign["metadata"]["name"] = "foreign-pod"
    rs = {
        "metadata": {
            "uid": "owned-rs",
            "annotations": {"deployment.kubernetes.io/revision": "2"},
            "ownerReferences": [{"uid": "deployment-uid", "controller": True}],
        },
    }

    async def request(method, path, body=None):
        assert method == "GET"
        if "/deployments/" in path:
            return document
        if "/replicasets?" in path:
            return {"items": [rs]}
        if "/pods?" in path:
            return {"items": [own, foreign]}
        return {
            "items": [
                {
                    "metadata": {"creationTimestamp": "now"},
                    "involvedObject": {"uid": uid},
                    "reason": "BackOff",
                    "message": "operator a@b.com",
                }
                for uid in ("own-pod-uid", "foreign-pod-uid")
            ]
        }

    monkeypatch.setattr(adapter, "request", request)
    result = await adapter.observe("k8s_crashloop")
    assert [pod["name"] for pod in result["pods"]] == ["owned-pod"]
    assert len(result["events"]) == 1
    assert "a@b.com" not in result["events"][0]["message"]
    assert "never-export-this" not in str(result)


async def test_checkpoint_requires_real_owner_audit_event(settings):
    store = Store(settings.database_path)
    engine = Engine(store, settings, Provider(settings))
    job, _ = store.create("k8s_crashloop", "no-audit-owner", "ollama", "test", 25)
    try:
        with pytest.raises(ProviderError, match="owner"):
            await engine.execute_kubernetes(job)
    finally:
        store.close()


async def test_container_transport_preserves_verified_cluster_tls_identity(
    settings,
    monkeypatch,
    tmp_path,
):
    token_path = tmp_path / "token"
    token_path.write_text("synthetic-service-account-credential")
    settings.kubernetes_token_file = str(token_path)
    settings.kubernetes_api_url = "https://host.docker.internal:64276"
    settings.kubernetes_tls_server_name = "127.0.0.1"
    tls = ssl.create_default_context()
    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: tls)
    original = httpx.AsyncClient

    def respond(request):
        assert request.url.host == "host.docker.internal"
        assert request.extensions["sni_hostname"] == "127.0.0.1"
        assert tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)),
    )
    result = await KubernetesAdapter(settings).request(
        "GET",
        "/api/v1/namespaces/remedi-demo/pods",
    )
    assert result == {"items": []}
