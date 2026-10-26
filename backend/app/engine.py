import asyncio
import logging
import time
from contextlib import suppress

from prometheus_client import Counter, Histogram

from app.adk_runtime import run_agent
from app.config import Settings
from app.domain import (
    KUBE_TARGETS,
    RUNBOOK_VERSION,
    SCENARIOS,
    AgentStep,
    policy,
)
from app.github_gitops import GitHubGitOps, target_document
from app.kubernetes import KubernetesAdapter, template_hash
from app.providers import Completion, Provider, ProviderError, completion_metadata
from app.simulator import SimulatedService, healthy
from app.store import Store

log = logging.getLogger("remedi")
LLM_LATENCY = Histogram("remedi_llm_seconds", "Inference latency", ["provider"])
LLM_TOKENS = Counter("remedi_llm_tokens", "LLM tokens", ["provider", "direction"])
LLM_COST = Counter("remedi_estimated_usd", "Configured estimated inference cost", ["provider"])
OUTCOMES = Counter("remedi_outcomes", "Workflow outcomes", ["status"])
FAILURES = Counter("remedi_failures", "Workflow failures", ["type"])


class Engine:
    def __init__(self, store: Store, settings: Settings, provider: Provider):
        self.store, self.settings, self.provider = store, settings, provider
        self.task: asyncio.Task | None = None
        self.stopping = False
        self.kubernetes = KubernetesAdapter(settings)
        self.github = GitHubGitOps(settings)

    def start(self):
        self.store.recover()
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def run(self):
        next_repository_poll = 0.0
        while not self.stopping:
            job = self.store.next_job()
            if job:
                await self.process(job)
            else:
                await asyncio.sleep(0.25)
            if self.settings.github_gitops_enabled and time.monotonic() >= next_repository_poll:
                next_repository_poll = time.monotonic() + 10
                for pending in self.store.pending_repository():
                    await self.process(pending)

    async def process(self, job: dict):
        incident_id = job["id"]
        try:
            if job["status"] in {"awaiting_merge", "awaiting_ci"}:
                await self.reconcile_repository(job)
                return
            if job["status"] == "approved":
                await self.execute(incident_id)
                return
            self.store.transition(
                incident_id, {"queued"}, "triaging", "triage", "Google ADK bounded triage started."
            )

            def record(result: Completion, index: int):
                meta = completion_metadata(result, self.provider)
                LLM_LATENCY.labels(self.settings.provider).observe(result.latency_ms / 1000)
                if result.input_tokens is not None:
                    LLM_TOKENS.labels(self.settings.provider, "input").inc(result.input_tokens)
                if result.output_tokens is not None:
                    LLM_TOKENS.labels(self.settings.provider, "output").inc(result.output_tokens)
                if result.estimated_usd is not None:
                    LLM_COST.labels(self.settings.provider).inc(result.estimated_usd)
                self.store.event(
                    incident_id,
                    "reason",
                    result.step.summary,
                    {**meta, "step": index, "tool": result.step.tool, "agent": agent_role},
                )

            def event(stage: str, message: str, payload: dict):
                self.store.event(incident_id, stage, message, payload)

            agent_role = "triage"
            kubernetes = job["scenario"] in KUBE_TARGETS
            if kubernetes:
                event(
                    "progress",
                    "Collecting real pod states and Kubernetes events.",
                    {"phase": "diagnosis", "namespace": self.settings.kubernetes_namespace},
                )

            repository_snapshot = None

            async def inspect():
                nonlocal repository_snapshot
                observation = await self.kubernetes.observe(job["scenario"])
                if self.settings.github_gitops_enabled:
                    repository_snapshot = await self.github.snapshot()
                    document = target_document(repository_snapshot["content"], job["scenario"])
                    container = document["spec"]["template"]["spec"]["containers"][0]
                    observation["repository_configuration"] = {
                        "repository": self.github.repo,
                        "path": "kubernetes/examples.yaml",
                        "commit_sha": repository_snapshot["commit_sha"],
                        "deployment": document["metadata"]["name"],
                        "image": container["image"],
                        "memory_limit": container.get("resources", {})
                        .get("limits", {})
                        .get("memory"),
                        "demo_crash": next(
                            (
                                item.get("value")
                                for item in container.get("env", [])
                                if item["name"] == "CRASH"
                            ),
                            None,
                        ),
                        "demo_unschedulable_selector": document["spec"]["template"]["spec"]
                        .get(
                            "nodeSelector",
                            {},
                        )
                        .get("remedi.demo/unavailable"),
                    }
                    event(
                        "repository",
                        "Read the approved repository manifest at an immutable commit.",
                        observation["repository_configuration"],
                    )
                return observation

            step, observations = await run_agent(
                self.provider,
                self.settings,
                job,
                record,
                event,
                inspect_evidence=inspect if kubernetes else None,
            )
            observed = {str(item["id"]) for item in observations}
            status, reason = policy(job["scenario"], step, observed)
            if SCENARIOS[job["scenario"]].get("dynamic") and status != "escalated":
                agent_role = "reviewer"
                if kubernetes:
                    event(
                        "progress",
                        "A fresh AI invocation is reviewing the live-evidence proposal.",
                        {"phase": "review"},
                    )
                review, _ = await run_agent(
                    self.provider,
                    self.settings,
                    job,
                    record,
                    event,
                    observations=observations,
                    review_of=step,
                )
                accepted = (
                    review.action == step.action
                    and review.confidence >= self.settings.confidence_threshold
                    and policy(job["scenario"], review, observed)[0] != "escalated"
                )
                event(
                    "judge",
                    review.summary,
                    {
                        "accepted": accepted,
                        "action": review.action,
                        "confidence": review.confidence,
                        "evidence_ids": review.evidence_ids,
                    },
                )
                if not accepted:
                    status, reason = (
                        "awaiting_approval",
                        "Independent LLM review requires owner judgment.",
                    )
            if status == "ready" and step.confidence < self.settings.confidence_threshold:
                status, reason = "awaiting_approval", "Confidence below automatic threshold."
            if kubernetes and status == "awaiting_approval":
                metrics = next(item for item in observations if item["id"] == "EV-METRICS-01")
                change = await self.kubernetes.prepare(job["scenario"], step.action, metrics)
                event(
                    "kubernetes_plan",
                    "Exact Kubernetes change and rollback checkpoint saved for owner review.",
                    {"change": change},
                )
                if self.settings.github_gitops_enabled:
                    if repository_snapshot is None:
                        raise ProviderError("Repository evidence is required before approval")
                    repository_plan = await self.github.prepare(
                        job["scenario"],
                        change,
                        repository_snapshot,
                    )
                    event(
                        "repository_plan",
                        "Exact repository diff saved. Approval creates a PR, not a cluster change.",
                        {"plan": repository_plan},
                    )
            self.store.transition(
                incident_id,
                {"triaging"},
                status,
                "decision",
                reason,
                plan=step.model_dump(),
                payload={"runbook_version": RUNBOOK_VERSION},
            )
            if status == "ready":
                await self.execute(incident_id)
            elif status == "escalated":
                OUTCOMES.labels(status).inc()
        except (ProviderError, ValueError, KeyError) as exc:
            FAILURES.labels(type(exc).__name__).inc()
            log.exception("workflow_failed incident_id=%s", incident_id)
            current = self.store.get(incident_id)
            self.store.transition(
                incident_id,
                {current["status"]},
                "failed",
                "error",
                "Workflow stopped; a change may have applied. Check cluster state before retrying."
                if current["status"] in {"executing", "verifying"}
                and current["scenario"] in KUBE_TARGETS
                else "Workflow failed closed; operator review required.",
                error=str(exc),
            )
            OUTCOMES.labels("failed").inc()

    async def execute(self, incident_id: str):
        incident = self.store.get(incident_id)
        plan = AgentStep.model_validate(incident["plan"])
        observations = {
            str(event["payload"]["observation"]["id"])
            for event in self.store.events(incident_id)
            if event["stage"] == "observe"
        }
        permitted, _ = policy(incident["scenario"], plan, observations)
        approved = incident["status"] == "approved"
        if permitted not in {"ready", "awaiting_approval"}:
            raise ProviderError("Execution policy recheck failed")
        if permitted == "awaiting_approval" and not approved:
            raise ProviderError("An owner approval is required")
        if plan.confidence < self.settings.confidence_threshold and not approved:
            raise ProviderError("Low-confidence execution requires owner approval")
        if SCENARIOS[incident["scenario"]].get("dynamic") and not approved:
            judgments = [
                event for event in self.store.events(incident_id) if event["stage"] == "judge"
            ]
            if not judgments or not judgments[-1]["payload"]["accepted"]:
                raise ProviderError("Independent review or owner approval is required")
        if incident["scenario"] in KUBE_TARGETS:
            if not approved:
                raise ProviderError("Every real Kubernetes change requires owner approval")
            if self.settings.github_gitops_enabled:
                await self.create_repository_pr(incident)
            else:
                await self.execute_kubernetes(incident)
            return
        service = SimulatedService(incident["scenario"])
        before = service.snapshot()
        self.store.transition(
            incident_id,
            {"ready", "approved"},
            "executing",
            "execute",
            f"SIMULATED {plan.action}: checkpoint saved; allowlisted adapter invoked.",
            payload={"action": plan.action, "checkpoint": before, "adapter": "simulator-v1"},
        )
        service.apply(plan.action)
        await asyncio.sleep(0.15)
        self.store.transition(
            incident_id,
            {"executing"},
            "verifying",
            "verify",
            "Independent synthetic health probes started; LLM cannot assert recovery.",
        )
        samples = []
        for _ in range(3):
            await asyncio.sleep(0.05)
            samples.append(service.probe())
        recovered = healthy(samples)
        restored = False
        if recovered:
            outcome = "closed"
            message = "Three independent simulated probes passed; incident closed."
            stage = "close"
        else:
            restored = service.restore(before)
            if not restored:
                raise ProviderError("Simulated rollback could not restore its checkpoint")
            outcome = "rolled_back"
            message = "Recovery checks failed. Simulated checkpoint restored; escalated to owner."
            stage = "rollback"
        self.store.transition(
            incident_id,
            {"verifying"},
            outcome,
            stage,
            message,
            payload={
                "samples": samples,
                "healthy": recovered,
                "restored_checkpoint": service.probe() if restored else None,
                "rollback_verified": restored,
            },
        )
        OUTCOMES.labels(outcome).inc()
        self.store.learn(incident_id, outcome)

    def approved_repository_plan(self, incident: dict) -> dict:
        events = self.store.events(incident["id"])
        plans = [event for event in events if event["stage"] == "repository_plan"]
        approvals = [
            event
            for event in events
            if event["stage"] == "approval"
            and event["actor"] == "owner"
            and event["payload"].get("decision") == "approve"
        ]
        if not plans or not approvals or approvals[-1]["seq"] <= plans[-1]["seq"]:
            raise ProviderError(
                "Owner approval of the repository diff is required; reanalyse legacy proposals"
            )
        plan = plans[-1]["payload"]["plan"]
        self.github.validate_checkpoint(plan, incident["scenario"])
        if plan["change"]["action"] != incident["plan"]["action"]:
            raise ProviderError("Approved repository diff does not match the proposed action")
        return plan

    async def create_repository_pr(self, incident: dict):
        plan = self.approved_repository_plan(incident)
        current = await self.kubernetes.deployment(incident["scenario"])
        if (
            current["metadata"]["uid"] != plan["change"]["baseline"]["uid"]
            or template_hash(current["spec"]["template"])
            != plan["change"]["baseline"]["template_hash"]
        ):
            raise ProviderError(
                "Live target changed after diagnosis; reanalyse before creating a PR"
            )
        self.store.event(
            incident["id"],
            "progress",
            "Creating an owner-approved manifest PR; no cluster patch.",
            {"phase": "pull_request"},
        )
        pr = await self.github.open_pr(incident["id"], incident["scenario"], plan)
        self.store.transition(
            incident["id"],
            {"approved"},
            "awaiting_merge",
            "github_pr",
            "PR created. A human must merge it; deployment remains blocked.",
            payload=pr,
        )

    async def reconcile_repository(self, incident: dict):
        plan = self.approved_repository_plan(incident)
        events = self.store.events(incident["id"])
        pr = next(event["payload"] for event in events if event["stage"] == "github_pr")
        sha = await self.github.merged(pr, plan)
        if sha is None:
            if incident["status"] == "awaiting_ci":
                raise ProviderError("PR merge state changed unexpectedly; deployment stopped")
            return
        if incident["status"] == "awaiting_merge":
            self.store.transition(
                incident["id"],
                {"awaiting_merge"},
                "awaiting_ci",
                "merge",
                "Human merge detected. Waiting for successful CI on this exact merge commit.",
                payload={"merge_sha": sha, "pr_url": pr["url"]},
            )
        else:
            recorded = next(
                event["payload"]["merge_sha"] for event in events if event["stage"] == "merge"
            )
            if sha != recorded:
                raise ProviderError("Merge commit changed after being recorded")
        if not await self.github.ci_passed(sha):
            return
        if (await self.github.snapshot())["content"] != plan["after_content"]:
            raise ProviderError(
                "Current main configuration differs from the approved merged manifest"
            )
        self.store.event(
            incident["id"],
            "ci",
            "Trusted merge-commit CI passed; deploying the merged approved field.",
            {"merge_sha": sha, "pr_url": pr["url"]},
        )
        await self.execute_kubernetes(self.store.get(incident["id"]), repository_plan=plan)

    async def execute_kubernetes(self, incident: dict, *, repository_plan: dict | None = None):
        incident_id, scenario = incident["id"], incident["scenario"]
        events = self.store.events(incident_id)
        approvals = [
            event
            for event in events
            if event["stage"] == "approval"
            and event["actor"] == "owner"
            and event["payload"].get("decision") == "approve"
        ]
        checkpoints = [event for event in events if event["stage"] == "kubernetes_plan"]
        if not checkpoints or not approvals or approvals[-1]["seq"] <= checkpoints[-1]["seq"]:
            raise ProviderError("An owner must approve the exact persisted Kubernetes checkpoint")
        change = checkpoints[-1]["payload"]["change"]
        if self.settings.github_gitops_enabled:
            if repository_plan is None or repository_plan["change"] != change:
                raise ProviderError(
                    "Direct Kubernetes execution is disabled in repository-backed mode"
                )
            if incident["status"] != "awaiting_ci":
                raise ProviderError(
                    "Merged manifest and successful CI are required before deployment"
                )
        if change["action"] != incident["plan"]["action"]:
            raise ProviderError("Approved checkpoint does not match the persisted action")
        self.store.transition(
            incident_id,
            {"awaiting_ci"} if repository_plan else {"approved"},
            "executing",
            "execute",
            "Applying the owner-approved bounded Kubernetes patch; no arbitrary commands.",
            payload={"adapter": "kubernetes-api-v1", "change": change},
        )
        applied = await self.kubernetes.change(scenario, change)
        self.store.transition(
            incident_id,
            {"executing"},
            "verifying",
            "verify",
            "Patch accepted. Watching actual Deployment rollout and pod readiness.",
            payload={"generation": applied["metadata"]["generation"]},
        )

        def record(stage: str, message: str, payload: dict):
            self.store.event(incident_id, stage, message, payload)

        recovered = await self.kubernetes.verify(scenario, applied, record)
        if recovered:
            outcome, stage = "closed", "close"
            message = "Real Kubernetes rollout passed three consecutive readiness checks."
            payload = {"adapter": "kubernetes-api-v1", "healthy": True, "consecutive_passes": 3}
        else:
            record(
                "progress",
                "Recovery timed out; restoring only the approved field checkpoint.",
                {"phase": "rollback", "healthy": False},
            )
            restored = await self.kubernetes.change(
                scenario,
                change,
                applied_hash=template_hash(applied["spec"]["template"]),
            )
            if template_hash(restored["spec"]["template"]) != change["baseline"]["template_hash"]:
                raise ProviderError("Kubernetes rollback checkpoint comparison failed")
            outcome, stage = "rolled_back", "rollback"
            message = (
                "Recovery timed out. Configuration checkpoint restored; "
                "pod health remains unresolved and requires owner investigation."
            )
            payload = {
                "adapter": "kubernetes-api-v1",
                "healthy": False,
                "configuration_restored": True,
                "recovery_verified": False,
                "repository_revert_required": bool(repository_plan),
            }
        self.store.transition(
            incident_id,
            {"verifying"},
            outcome,
            stage,
            message,
            payload=payload,
        )
        OUTCOMES.labels(outcome).inc()
        self.store.learn(incident_id, outcome)
