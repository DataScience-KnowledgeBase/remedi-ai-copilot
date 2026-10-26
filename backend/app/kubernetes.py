import asyncio
import hashlib
import json
import ssl
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.domain import KUBE_RUNBOOKS, KUBE_TARGETS, redact
from app.providers import ProviderError

GOOD_IMAGE = "python:3.12-alpine"
BAD_IMAGE = "python:remedi-missing-demo"
MANAGED_LABEL = "app.kubernetes.io/managed-by"
TARGET_LABEL = "remedi.demo/target"


def template_hash(template: dict) -> str:
    return hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()


class KubernetesAdapter:
    """Namespace-scoped REST adapter; models never supply URLs, patches or commands."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.namespace = settings.kubernetes_namespace

    async def request(self, method: str, path: str, body: list[dict] | None = None) -> dict:
        if not self.settings.kubernetes_enabled:
            raise ProviderError("Kubernetes integration is disabled")
        try:
            token = Path(self.settings.kubernetes_token_file).read_text().strip()
            if not token:
                raise ProviderError("Kubernetes service-account token file is empty")
            tls = ssl.create_default_context(cafile=self.settings.kubernetes_ca_file)
            async with httpx.AsyncClient(
                base_url=self.settings.kubernetes_api_url.rstrip("/"),
                verify=tls,
                timeout=15,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=body,
                    extensions={
                        "sni_hostname": self.settings.kubernetes_tls_server_name,
                    }
                    if self.settings.kubernetes_tls_server_name
                    else {},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json-patch+json",
                    },
                )
            if not response.is_success:
                raise ProviderError(
                    f"Kubernetes API {method} failed (HTTP {response.status_code}); "
                    "check namespace RBAC, token expiry and stale approval."
                )
            return response.json()
        except (OSError, ssl.SSLError, httpx.HTTPError, ValueError) as exc:
            raise ProviderError(
                f"Kubernetes connection/configuration failed ({type(exc).__name__}); "
                "no simulation fallback."
            ) from exc

    def deployment_path(self, scenario: str) -> str:
        if scenario not in KUBE_TARGETS:
            raise ProviderError("Unapproved Kubernetes target")
        return f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{KUBE_TARGETS[scenario]}"

    def validate_target(self, deployment: dict, scenario: str):
        metadata = deployment["metadata"]
        labels = metadata.get("labels", {})
        spec = deployment["spec"]
        pod_spec = spec["template"]["spec"]
        containers = pod_spec.get("containers", [])
        if (
            metadata["name"] != KUBE_TARGETS[scenario]
            or metadata.get("namespace") != self.namespace
            or labels.get(MANAGED_LABEL) != "remedi-ai"
            or labels.get(TARGET_LABEL) != KUBE_TARGETS[scenario]
            or spec.get("replicas") != 1
            or len(containers) != 1
            or containers[0].get("name") != "demo"
            or pod_spec.get("hostNetwork")
            or pod_spec.get("hostPID")
            or pod_spec.get("hostIPC")
            or pod_spec.get("volumes")
            or containers[0].get("securityContext", {}).get("privileged")
        ):
            raise ProviderError("Target is not an authorized isolated single-container demo")

    async def deployment(self, scenario: str) -> dict:
        result = await self.request("GET", self.deployment_path(scenario))
        self.validate_target(result, scenario)
        return result

    async def items(self, path: str) -> list[dict]:
        result = await self.request("GET", path)
        if result.get("metadata", {}).get("continue"):
            raise ProviderError("Kubernetes evidence exceeds the bounded collection limit")
        return result["items"]

    async def observe(self, scenario: str) -> dict:
        deployment = await self.deployment(scenario)
        target = KUBE_TARGETS[scenario]
        selector = f"{MANAGED_LABEL}=remedi-ai,{TARGET_LABEL}={target}"
        replicasets = await self.items(
            f"/apis/apps/v1/namespaces/{self.namespace}/replicasets"
            f"?labelSelector={selector}&limit=100"
        )
        owned = {
            item["metadata"]["uid"]: item["metadata"]
            .get("annotations", {})
            .get("deployment.kubernetes.io/revision")
            for item in replicasets
            if any(
                owner.get("uid") == deployment["metadata"]["uid"] and owner.get("controller")
                for owner in item["metadata"].get("ownerReferences", [])
            )
        }
        pod_items = await self.items(
            f"/api/v1/namespaces/{self.namespace}/pods?labelSelector={selector}&limit=100"
        )
        pods = []
        pod_uids = set()
        for pod in pod_items:
            owners = pod["metadata"].get("ownerReferences", [])
            owner = next(
                (
                    owner
                    for owner in owners
                    if owner.get("uid") in owned and owner.get("controller")
                ),
                None,
            )
            if owner is None:
                continue
            pod_uids.add(pod["metadata"]["uid"])
            status = pod.get("status", {})
            pods.append(
                {
                    "name": pod["metadata"]["name"],
                    "uid": pod["metadata"]["uid"],
                    "revision": owned[owner["uid"]],
                    "deleting": bool(pod["metadata"].get("deletionTimestamp")),
                    "phase": status.get("phase"),
                    "conditions": [
                        {key: condition.get(key) for key in ("type", "status", "reason")}
                        for condition in status.get("conditions", [])
                    ],
                    "containers": [
                        {
                            "name": container["name"],
                            "ready": container.get("ready", False),
                            "restart_count": container.get("restartCount", 0),
                            "state": self.container_state(container.get("state", {})),
                            "last_state": self.container_state(container.get("lastState", {})),
                        }
                        for container in status.get("containerStatuses", [])
                    ],
                }
            )
        events = await self.items(f"/api/v1/namespaces/{self.namespace}/events?limit=500")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        metadata = deployment["metadata"]
        return {
            "id": "EV-METRICS-01",
            "source": "kubernetes-api-v1",
            "synthetic": False,
            "diagnostic_mode": True,
            "namespace": self.namespace,
            "deployment": target,
            "baseline": {
                "uid": metadata["uid"],
                "template_hash": template_hash(deployment["spec"]["template"]),
            },
            "generation": metadata["generation"],
            "revision": metadata.get("annotations", {}).get("deployment.kubernetes.io/revision"),
            "rollout": {
                key: deployment.get("status", {}).get(key, 0)
                for key in (
                    "observedGeneration",
                    "replicas",
                    "updatedReplicas",
                    "readyReplicas",
                    "availableReplicas",
                    "unavailableReplicas",
                )
            },
            "configuration": {
                "image": container["image"],
                "memory_limit": container.get("resources", {}).get("limits", {}).get("memory"),
                "demo_crash": next(
                    (
                        item.get("value")
                        for item in container.get("env", [])
                        if item["name"] == "CRASH"
                    ),
                    None,
                ),
                "demo_unschedulable_selector": deployment["spec"]["template"]["spec"]
                .get("nodeSelector", {})
                .get("remedi.demo/unavailable"),
            },
            "pods": pods,
            "events": [
                {
                    "reason": item.get("reason"),
                    "type": item.get("type"),
                    "message": redact(item.get("message", ""))[:1000],
                    "count": item.get("count", 1),
                    "at": item.get("lastTimestamp") or item["metadata"].get("creationTimestamp"),
                    "pod_uid": item.get("involvedObject", {}).get("uid"),
                }
                for item in events
                if item.get("involvedObject", {}).get("uid") in pod_uids
            ][-40:],
            "candidate_runbooks": {
                action: book["description"] for action, book in KUBE_RUNBOOKS.items()
            },
        }

    @staticmethod
    def container_state(state: dict) -> dict:
        return {
            kind: {
                key: value
                for key, value in detail.items()
                if key
                in {
                    "reason",
                    "exitCode",
                    "startedAt",
                    "finishedAt",
                }
            }
            for kind, detail in state.items()
        }

    async def prepare(self, scenario: str, action: str, observation: dict) -> dict:
        deployment = await self.deployment(scenario)
        baseline = observation["baseline"]
        if (
            deployment["metadata"]["uid"] != baseline["uid"]
            or template_hash(deployment["spec"]["template"]) != baseline["template_hash"]
        ):
            raise ProviderError("Deployment changed during diagnosis; create a fresh incident")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        prefix = "/spec/template/spec/containers/0"
        reasons = {
            detail.get("reason")
            for pod in observation["pods"]
            for item in pod["containers"]
            for state in (item["state"], item["last_state"])
            for detail in state.values()
        }
        if action == "fix_pod_startup":
            flags = container.get("env", [])
            index = next((i for i, item in enumerate(flags) if item["name"] == "CRASH"), -1)
            if (
                index < 0
                or flags[index].get("value") != "true"
                or not reasons
                & {
                    "CrashLoopBackOff",
                    "Error",
                }
            ):
                raise ProviderError("Startup remedy is not supported by current pod evidence")
            path, before, after = f"{prefix}/env/{index}/value", "true", "false"
        elif action == "increase_pod_memory":
            before = container.get("resources", {}).get("limits", {}).get("memory")
            if before != "32Mi" or "OOMKilled" not in reasons:
                raise ProviderError("Memory remedy requires an observed OOM at the demo limit")
            path, after = f"{prefix}/resources/limits/memory", "128Mi"
        elif action == "restore_pod_image":
            before = container["image"]
            if before != BAD_IMAGE or not reasons & {"ImagePullBackOff", "ErrImagePull"}:
                raise ProviderError("Image remedy requires the known missing demo image failure")
            path, after = f"{prefix}/image", GOOD_IMAGE
        elif action == "fix_pod_scheduling":
            before = (
                deployment["spec"]["template"]["spec"]
                .get("nodeSelector", {})
                .get("remedi.demo/unavailable")
            )
            failed = any(
                condition["type"] == "PodScheduled"
                and condition["status"] == "False"
                and condition["reason"] == "Unschedulable"
                for pod in observation["pods"]
                for condition in pod["conditions"]
            )
            if before != "true" or not failed:
                raise ProviderError("Scheduling remedy requires the impossible demo selector")
            path, after = "/spec/template/spec/nodeSelector/remedi.demo~1unavailable", None
        else:
            raise ProviderError("Unavailable Kubernetes action")
        return {
            "namespace": self.namespace,
            "deployment": KUBE_TARGETS[scenario],
            "baseline": baseline,
            "action": action,
            "path": path,
            "before": before,
            "after": after,
            "operation": "remove" if after is None else "replace",
            "rollback": "Restore only this field if the applied template is still unchanged.",
        }

    async def change(self, scenario: str, plan: dict, *, applied_hash: str | None = None) -> dict:
        deployment = await self.deployment(scenario)
        if plan["namespace"] != self.namespace or plan["deployment"] != KUBE_TARGETS[scenario]:
            raise ProviderError("Approved Kubernetes scope no longer matches configuration")
        expected_hash = applied_hash or plan["baseline"]["template_hash"]
        if (
            deployment["metadata"]["uid"] != plan["baseline"]["uid"]
            or template_hash(deployment["spec"]["template"]) != expected_hash
        ):
            raise ProviderError("Stale Kubernetes approval/checkpoint; no overwrite was attempted")
        rollback = applied_hash is not None
        operation = (
            "add"
            if rollback and plan["operation"] == "remove"
            else "replace"
            if rollback
            else plan["operation"]
        )
        patch: list[dict[str, Any]] = [
            {"op": "test", "path": "/metadata/uid", "value": plan["baseline"]["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": deployment["metadata"]["resourceVersion"],
            },
            {"op": operation, "path": plan["path"]},
        ]
        if operation != "remove":
            patch[-1]["value"] = plan["before"] if rollback else plan["after"]
        return await self.request("PATCH", self.deployment_path(scenario), patch)

    @staticmethod
    def healthy(observation: dict, generation: int) -> bool:
        rollout = observation["rollout"]
        pods = [pod for pod in observation["pods"] if not pod["deleting"]]
        return (
            rollout["observedGeneration"] >= generation
            and rollout["replicas"]
            == rollout["updatedReplicas"]
            == rollout["readyReplicas"]
            == rollout["availableReplicas"]
            == 1
            and rollout["unavailableReplicas"] == 0
            and len(pods) == 1
            and pods[0]["revision"] == observation["revision"]
            and pods[0]["phase"] == "Running"
            and any(
                item["type"] == "Ready" and item["status"] == "True"
                for item in pods[0]["conditions"]
            )
            and len(pods[0]["containers"]) == 1
            and all(
                item["ready"] and item["restart_count"] == 0 and "running" in item["state"]
                for item in pods[0]["containers"]
            )
        )

    async def verify(
        self,
        scenario: str,
        applied: dict,
        record: Callable[[str, str, dict], None],
    ) -> bool:
        deadline = time.monotonic() + self.settings.kubernetes_rollout_timeout_seconds
        consecutive = 0
        expected = template_hash(applied["spec"]["template"])
        pod_identity = None
        while time.monotonic() < deadline:
            observation = await self.observe(scenario)
            if observation["baseline"] != {
                "uid": applied["metadata"]["uid"],
                "template_hash": expected,
            }:
                raise ProviderError("Deployment changed during rollout; operator review required")
            current = tuple(
                sorted(pod["uid"] for pod in observation["pods"] if not pod["deleting"])
            )
            if current != pod_identity:
                consecutive = 0
                pod_identity = current
            healthy = self.healthy(observation, applied["metadata"]["generation"])
            consecutive = consecutive + 1 if healthy else 0
            record(
                "progress",
                f"Live Kubernetes recovery checks: {consecutive}/3 consecutive passes.",
                {
                    "phase": "verification",
                    "consecutive_passes": consecutive,
                    "required_passes": 3,
                    "healthy": healthy,
                    "rollout": observation["rollout"],
                    "pods": observation["pods"],
                    "namespace": self.namespace,
                },
            )
            if consecutive >= 3:
                return True
            await asyncio.sleep(self.settings.kubernetes_probe_interval_seconds)
        return False
