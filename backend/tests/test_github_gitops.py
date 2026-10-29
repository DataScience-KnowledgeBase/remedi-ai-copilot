import base64
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import AgentStep
from app.engine import Engine
from app.github_gitops import (
    ACTIONS_APP_ID,
    CI_CHECK,
    MANIFEST_PATH,
    GitHubGitOps,
    documents,
    manifest_change,
    target_document,
)
from app.main import create_app
from app.providers import Provider, ProviderError
from app.store import Store
from tests.test_kubernetes import deployment, observation

REPO = "DataScience-KnowledgeBase/remedi-ai-copilot"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        provider="ollama",
        operator_token="o" * 40,
        owner_token="w" * 40,
        database_path=str(tmp_path / "gitops.db"),
        kubernetes_enabled=True,
        github_gitops_enabled=True,
        github_app_id=5483871,
        github_installation_id=175563857,
    )


@pytest.fixture
def content():
    return (Path(__file__).parents[2] / "kubernetes" / "examples.yaml").read_text()


@pytest.fixture
async def checkpoint(settings, content, monkeypatch):
    engine = Engine(Store(settings.database_path), settings, Provider(settings))
    document = deployment()
    observed = observation(document)
    observed["pods"][0]["containers"][0]["last_state"] = {"terminated": {"reason": "Error"}}

    async def live(_scenario):
        return document

    async def main():
        return "base-sha"

    monkeypatch.setattr(engine.kubernetes, "deployment", live)
    monkeypatch.setattr(engine.github, "main_sha", main)
    change = await engine.kubernetes.prepare("k8s_crashloop", "fix_pod_startup", observed)
    plan = await engine.github.prepare(
        "k8s_crashloop",
        change,
        {"content": content, "blob_sha": "blob-sha", "commit_sha": "base-sha"},
    )
    yield engine, plan
    engine.store.close()


def test_manifest_edit_changes_only_approved_field(content):
    for scenario, pointer, before, after, operation in [
        (
            "k8s_crashloop",
            "/spec/template/spec/containers/0/env/0/value",
            "true",
            "false",
            "replace",
        ),
        (
            "k8s_oom",
            "/spec/template/spec/containers/0/resources/limits/memory",
            "32Mi",
            "128Mi",
            "replace",
        ),
        (
            "k8s_imagepull",
            "/spec/template/spec/containers/0/image",
            "python:remedi-missing-demo",
            "python:3.12-alpine",
            "replace",
        ),
        (
            "k8s_pending",
            "/spec/template/spec/nodeSelector/remedi.demo~1unavailable",
            "true",
            None,
            "remove",
        ),
    ]:
        updated = manifest_change(
            content,
            scenario,
            {
                "path": pointer,
                "before": before,
                "after": after,
                "operation": operation,
            },
        )
        original_docs = documents(content)
        new_docs = documents(updated)
        target = target_document(content, scenario)["metadata"]["name"]
        for original, new in zip(original_docs, new_docs, strict=True):
            if original["metadata"]["name"] != target:
                assert original == new
        assert original_docs != new_docs
    with pytest.raises(ProviderError, match="differs"):
        manifest_change(
            content,
            "k8s_crashloop",
            {
                "path": "/spec/template/spec/containers/0/env/0/value",
                "before": "not-the-baseline",
                "after": "false",
                "operation": "replace",
            },
        )


def test_jwt_is_signed_without_persisting_installation_token(settings, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "test-app.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    settings.github_private_key_file = str(path)
    token = GitHubGitOps(settings).app_jwt()
    header, payload, signature = token.split(".")
    key.public_key().verify(
        base64.urlsafe_b64decode(signature + "=="),
        f"{header}.{payload}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    assert claims["iss"] == "5483871" and claims["exp"] - claims["iat"] <= 600


async def test_legacy_owner_approval_is_blocked_before_cluster_or_repository_write(settings):
    with TestClient(create_app(settings)) as client:
        store = client.app.state.store
        job, _ = store.create("k8s_oom", "legacy-gitops-key", "ollama", "test", 25)
        store.transition(job["id"], {"queued"}, "awaiting_approval", "decision", "Legacy plan")
        response = client.post(
            f"/api/incidents/{job['id']}/approval",
            json={"decision": "approve", "reason": "Reviewing existing legacy plan"},
            headers={"Authorization": "Bearer " + "w" * 40},
        )
        assert response.status_code == 409 and "Legacy" in response.json()["detail"]
        assert store.get(job["id"])["status"] == "awaiting_approval"
        response = client.post(
            f"/api/incidents/{job['id']}/approval",
            json={
                "decision": "reject",
                "reason": "Reject legacy plan; repository analysis is required",
            },
            headers={"Authorization": "Bearer " + "w" * 40},
        )
        assert response.status_code == 200 and response.json()["status"] == "rejected"


async def test_missing_remote_manifest_has_no_local_fallback(settings, monkeypatch):
    github = GitHubGitOps(settings)

    async def api(*args, **kwargs):
        return None

    monkeypatch.setattr(github, "api", api)
    with pytest.raises(ProviderError, match="not published"):
        await github.file("main-sha")


async def test_pr_creation_is_exact_and_restart_idempotent(checkpoint, monkeypatch):
    engine, plan = checkpoint
    github = engine.github
    created = {"ref": False, "content": False, "pr": False}
    writes = []

    async def api(method, path, body=None, **kwargs):
        if method != "GET":
            writes.append((method, path))
        if "/git/ref/heads/remedi/" in path:
            return (
                {"object": {"sha": "head-sha" if created["content"] else "base-sha"}}
                if created["ref"]
                else None
            )
        if path.endswith("/git/refs"):
            created["ref"] = True
            return {}
        if "/contents/" in path and method == "PUT":
            assert base64.b64decode(body["content"]).decode() == plan["after_content"]
            assert "Co-authored-by: Copilot" in body["message"]
            created["content"] = True
            return {"commit": {"sha": "head-sha"}}
        if "/contents/" in path:
            return {
                "encoding": "base64",
                "size": len(plan["after_content"]),
                "content": base64.b64encode(plan["after_content"].encode()).decode(),
                "sha": "new-blob",
            }
        if "/compare/" in path:
            return {"ahead_by": 1, "files": [{"filename": MANIFEST_PATH, "status": "modified"}]}
        pr = {
            "number": 7,
            "html_url": f"https://github.com/{REPO}/pull/7",
            "head": {"sha": "head-sha"},
            "base": {"ref": "main"},
        }
        if "/pulls?" in path:
            return [pr] if created["pr"] else []
        if path.endswith("/pulls") and method == "POST":
            created["pr"] = True
            return pr
        pytest.fail(f"Unexpected API path: {path}")

    monkeypatch.setattr(github, "api", api)
    first = await github.open_pr("incident-id", "k8s_crashloop", plan)
    second = await github.open_pr("incident-id", "k8s_crashloop", plan)
    assert first == second
    assert len(writes) == 3  # Create ref, update manifest, create PR; replay adds no writes.
    assert not any("/merge" in path for _, path in writes)


@pytest.mark.parametrize("mutation", ["path", "repository", "content"])
async def test_tampered_checkpoint_cannot_create_a_pr(checkpoint, mutation):
    engine, original = checkpoint
    plan = deepcopy(original)
    if mutation == "path":
        plan["path"] = ".github/workflows/pipeline.yml"
    elif mutation == "repository":
        plan["repository"] = "other/repo"
    else:
        plan["after_content"] += "\n# unexpected change"
    with pytest.raises(ProviderError, match="outside"):
        engine.github.validate_checkpoint(plan, "k8s_crashloop")


@pytest.mark.parametrize(
    ("check", "expected"),
    [
        (None, False),
        ("in_progress", False),
        ("success", True),
        ("failure", "error"),
        ("cancelled", "error"),
        ("skipped", "error"),
        ("spoofed", False),
    ],
)
async def test_only_trusted_successful_merge_ci_allows_deployment(
    settings, monkeypatch, check, expected
):
    github = GitHubGitOps(settings)

    async def api(*args, **kwargs):
        return (
            {"total_count": 0, "check_runs": []}
            if check is None
            else {
                "total_count": 1,
                "check_runs": [
                    {
                        "id": 1,
                        "name": CI_CHECK,
                        "app": {"id": 999 if check == "spoofed" else ACTIONS_APP_ID},
                        "status": "in_progress" if check == "in_progress" else "completed",
                        "conclusion": check,
                    }
                ],
            }
        )

    monkeypatch.setattr(github, "api", api)
    if expected == "error":
        with pytest.raises(ProviderError, match="CI did not succeed"):
            await github.ci_passed("merged-sha")
    else:
        assert await github.ci_passed("merged-sha") is expected


async def test_approval_only_opens_pr_then_waits_for_human_merge_and_ci(checkpoint, monkeypatch):
    engine, plan = checkpoint
    store = engine.store
    job, _ = store.create("k8s_crashloop", "owner-pr-key", "ollama", "test", 25)
    agent_plan = AgentStep(
        tool="propose",
        action="fix_pod_startup",
        confidence=0.95,
        evidence_ids=["EV-METRICS-01", "RB-K8S-STARTUP-01"],
        summary="Live and repo startup flag.",
    )
    store.event(job["id"], "repository_plan", "Exact diff", {"plan": plan})
    store.transition(
        job["id"],
        {"queued"},
        "awaiting_approval",
        "decision",
        "Owner required",
        plan=agent_plan.model_dump(),
    )
    store.transition(
        job["id"],
        {"awaiting_approval"},
        "approved",
        "approval",
        "Reviewed diff",
        actor="owner",
        payload={"decision": "approve"},
    )
    pr = {"number": 7, "url": f"https://github.com/{REPO}/pull/7", "head_sha": "head-sha"}
    state = {"merged": None, "ci": False}
    deployments = []

    async def open_pr(*args):
        return pr

    async def merged(*args):
        return state["merged"]

    async def ci(*args):
        return state["ci"]

    async def snapshot():
        return {"content": plan["after_content"]}

    async def deploy(incident, *, repository_plan=None):
        deployments.append(repository_plan)

    monkeypatch.setattr(engine.github, "open_pr", open_pr)
    monkeypatch.setattr(engine.github, "merged", merged)
    monkeypatch.setattr(engine.github, "ci_passed", ci)
    monkeypatch.setattr(engine.github, "snapshot", snapshot)
    monkeypatch.setattr(engine, "execute_kubernetes", deploy)
    await engine.create_repository_pr(store.get(job["id"]))
    assert store.get(job["id"])["status"] == "awaiting_merge" and deployments == []
    await engine.reconcile_repository(store.get(job["id"]))
    assert deployments == []
    state["merged"] = "merge-sha"
    await engine.reconcile_repository(store.get(job["id"]))
    assert store.get(job["id"])["status"] == "awaiting_ci" and deployments == []
    assert len(store.pending_repository()) == 1
    state["ci"] = True
    await engine.reconcile_repository(store.get(job["id"]))
    assert deployments == [plan]


async def test_gitops_direct_kubernetes_execution_cannot_bypass_pr_gate(checkpoint):
    engine, plan = checkpoint
    job, _ = engine.store.create("k8s_crashloop", "bypass-key", "ollama", "test", 25)
    engine.store.event(job["id"], "kubernetes_plan", "Checkpoint", {"change": plan["change"]})
    engine.store.transition(
        job["id"],
        {"queued"},
        "approved",
        "approval",
        "Owner",
        actor="owner",
        payload={"decision": "approve"},
        plan={"action": "fix_pod_startup"},
    )
    with pytest.raises(ProviderError, match="Direct Kubernetes execution is disabled"):
        await engine.execute_kubernetes(engine.store.get(job["id"]))


async def test_auth_scopes_installation_token_to_expected_repository(settings, monkeypatch):
    github = GitHubGitOps(settings)
    monkeypatch.setattr(github, "app_jwt", lambda: "synthetic-jwt")
    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        assert request.url.host == "api.github.com"
        if request.method == "POST":
            assert json.loads(request.content)["repositories"] == ["remedi-ai-copilot"]
            return httpx.Response(201, json={"token": "synthetic-installation-token"})
        return httpx.Response(200, json={"object": {"sha": "base-sha"}})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)),
    )
    assert await github.main_sha() == "base-sha"
    assert await github.main_sha() == "base-sha"
    assert len([request for request in requests if request.method == "POST"]) == 1


@pytest.mark.parametrize(
    "case",
    ["open", "closed", "head_changed", "bot_merge", "extra_file", "content_changed", "merged"],
)
async def test_merge_requires_human_and_exact_approved_file(checkpoint, monkeypatch, case):
    engine, plan = checkpoint
    github = engine.github
    pr = {"number": 7, "head_sha": "head-sha"}
    result = {
        "state": "open" if case == "open" else "closed",
        "merged": case not in {"open", "closed"},
        "merged_by": {"type": "Bot" if case == "bot_merge" else "User"},
        "merge_commit_sha": "merged-sha",
        "head": {
            "sha": "changed" if case == "head_changed" else "head-sha",
            "repo": {"full_name": REPO},
        },
        "base": {"ref": "main", "repo": {"full_name": REPO}},
    }

    async def api(method, path, *args, **kwargs):
        if "/files?" in path:
            return [{"filename": MANIFEST_PATH}] + (
                [{"filename": "backend/app/engine.py"}] if case == "extra_file" else []
            )
        return result

    async def file(sha):
        return {
            "content": plan["before_content"]
            if case == "content_changed"
            else plan["after_content"]
        }

    monkeypatch.setattr(github, "api", api)
    monkeypatch.setattr(github, "file", file)
    if case == "open":
        assert await github.merged(pr, plan) is None
    elif case == "merged":
        assert await github.merged(pr, plan) == "merged-sha"
    else:
        with pytest.raises(ProviderError):
            await github.merged(pr, plan)


@pytest.mark.parametrize(
    "yaml_text", ["---\nnull\n", "metadata: null\n---\n{}\n---\n{}\n---\n{}\n"],
)
def test_invalid_repository_document_shape_is_an_explicit_error(yaml_text):
    with pytest.raises(ProviderError, match="exactly the four"):
        documents(yaml_text)


def test_simulator_approval_does_not_claim_to_create_a_pr_in_github_mode(settings):
    with TestClient(create_app(settings)) as client:
        store = client.app.state.store
        job, _ = store.create("worker_stall", "simulator-approval", "ollama", "test", 25)
        store.transition(job["id"], {"queued"}, "awaiting_approval", "decision", "Simulation")
        response = client.post(
            f"/api/incidents/{job['id']}/approval",
            json={"decision": "approve", "reason": "Approve the simulated worker recovery"},
            headers={"Authorization": "Bearer " + "w" * 40},
        )
        assert response.status_code == 200
        approval = next(event for event in store.events(job["id"]) if event["stage"] == "approval")
        assert approval["payload"]["action"] == "execute"
