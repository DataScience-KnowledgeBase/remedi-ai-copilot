import asyncio
import logging
from contextlib import suppress

from prometheus_client import Counter, Histogram

from app.config import Settings
from app.domain import (
    RUNBOOK_VERSION,
    RUNBOOKS,
    AgentStep,
    evidence,
    policy,
)
from app.providers import Provider, ProviderError, completion_metadata
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
        while not self.stopping:
            job = self.store.next_job()
            if job:
                await self.process(job)
            else:
                await asyncio.sleep(0.25)

    async def process(self, job: dict):
        incident_id = job["id"]
        try:
            if job["status"] == "approved":
                await self.execute(incident_id)
                return
            self.store.transition(
                incident_id, {"queued"}, "triaging", "triage", "Bounded ReAct triage started."
            )
            observations: list[dict] = []
            observed: set[str] = set()
            for index in range(self.settings.max_agent_steps):
                result = await self.provider.complete(
                    {
                        "synthetic": True,
                        "service": job["service"],
                        "observations": observations,
                        "step": index + 1,
                    }
                )
                meta = completion_metadata(result, self.provider)
                LLM_LATENCY.labels(self.settings.provider).observe(result.latency_ms / 1000)
                if result.input_tokens is not None:
                    LLM_TOKENS.labels(self.settings.provider, "input").inc(result.input_tokens)
                if result.output_tokens is not None:
                    LLM_TOKENS.labels(self.settings.provider, "output").inc(result.output_tokens)
                if result.estimated_usd is not None:
                    LLM_COST.labels(self.settings.provider).inc(result.estimated_usd)
                step = result.step
                self.store.event(
                    incident_id,
                    "reason",
                    step.summary,
                    {**meta, "step": index + 1, "tool": step.tool},
                )
                if step.tool == "propose":
                    status, reason = policy(job["scenario"], step, observed)
                    if status == "ready" and step.confidence < self.settings.confidence_threshold:
                        status, reason = (
                            "awaiting_approval",
                            "Confidence below automatic threshold.",
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
                    return
                if step.tool == "inspect_metrics":
                    observation = evidence(job["scenario"])
                else:
                    if step.action not in RUNBOOKS:
                        raise ProviderError("Model requested an unavailable runbook")
                    observation = {**RUNBOOKS[step.action], "version": RUNBOOK_VERSION}
                observations.append(observation)
                observed.add(str(observation["id"]))
                self.store.event(
                    incident_id,
                    "observe",
                    "Read-only tool observation.",
                    {"tool": step.tool, "observation": observation},
                )
            raise ProviderError("Agent exhausted its bounded step budget without a valid proposal")
        except (ProviderError, ValueError, KeyError) as exc:
            FAILURES.labels(type(exc).__name__).inc()
            log.exception("workflow_failed incident_id=%s", incident_id)
            current = self.store.get(incident_id)
            self.store.transition(
                incident_id,
                {current["status"]},
                "failed",
                "error",
                "Workflow failed closed; operator review required.",
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
