from collections.abc import AsyncGenerator, Callable

from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import PrivateAttr

from app.config import Settings
from app.domain import RUNBOOK_VERSION, RUNBOOKS, AgentStep, evidence
from app.providers import Completion, Provider, ProviderError


class ProviderLlm(BaseLlm):
    """Translate typed provider decisions into native ADK function calls."""

    _provider: Provider = PrivateAttr()
    _context: dict = PrivateAttr()
    _record: Callable[[Completion, int], None] = PrivateAttr()
    _calls: int = PrivateAttr(default=0)
    _budget: int = PrivateAttr()

    def __init__(
        self,
        provider: Provider,
        context: dict,
        budget: int,
        record: Callable[[Completion, int], None],
    ):
        super().__init__(model=provider.model)
        self._provider, self._context = provider, context
        self._budget, self._record = budget, record

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        if self._calls >= self._budget:
            raise ProviderError("Agent exhausted its bounded step budget without a valid proposal")
        self._calls += 1
        result = await self._provider.complete({**self._context, "step": self._calls})
        self._record(result, self._calls)
        step = result.step
        if step.tool == "propose":
            parts = [types.Part(text=step.model_dump_json())]
        else:
            if self._context.get("review_mode"):
                raise ProviderError("Independent reviewer must return a final judgment")
            args = {"action": step.action} if step.tool == "read_runbook" else {}
            parts = [types.Part(function_call=types.FunctionCall(name=step.tool, args=args))]
        usage = None
        if result.input_tokens is not None and result.output_tokens is not None:
            usage = types.GenerateContentResponseUsageMetadata(
                prompt_token_count=result.input_tokens,
                candidates_token_count=result.output_tokens,
                total_token_count=result.input_tokens + result.output_tokens,
            )
        yield LlmResponse(content=types.Content(role="model", parts=parts), usage_metadata=usage)


async def run_agent(
    provider: Provider,
    settings: Settings,
    job: dict,
    record_completion: Callable[[Completion, int], None],
    record_event: Callable[[str, str, dict], None],
    *,
    observations: list[dict] | None = None,
    review_of: AgentStep | None = None,
) -> tuple[AgentStep, list[dict]]:
    collected = list(observations or [])
    review = review_of is not None
    context = {"synthetic": True, "service": job["service"], "observations": collected}
    if review:
        context.update({"review_mode": True, "proposed_resolution": review_of.model_dump()})

    async def inspect_metrics() -> dict:
        """Inspect trusted synthetic service metrics and diagnostic context."""
        observation = evidence(job["scenario"])
        collected.append(observation)
        record_event(
            "observe",
            "ADK invoked read-only metrics inspection.",
            {"tool": "inspect_metrics", "observation": observation},
        )
        return observation

    async def read_runbook(action: str) -> dict:
        """Read a reviewed runbook for clear_cache, restart_worker or scale_workers."""
        if action not in RUNBOOKS:
            raise ProviderError("Model requested an unavailable runbook")
        observation = {**RUNBOOKS[action], "version": RUNBOOK_VERSION}
        collected.append(observation)
        record_event(
            "observe",
            "ADK invoked read-only runbook retrieval.",
            {"tool": "read_runbook", "observation": observation},
        )
        return observation

    budget = 1 if review else settings.max_agent_steps
    model = ProviderLlm(provider, context, budget, record_completion)
    agent = LlmAgent(
        name="resolution_reviewer" if review else "incident_triage",
        model=model,
        instruction="Diagnose from tool evidence; return a cited, bounded remediation proposal.",
        tools=[] if review else [inspect_metrics, read_runbook],
    )
    sessions = InMemorySessionService()
    app_name, user_id = "remedi_adk", "service_operator"
    session = await sessions.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(app_name=app_name, agent=agent, session_service=sessions)
    plan = None
    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=types.Content(
                role="user",
                parts=[
                    types.Part(
                        text="Independently review the proposed remedy."
                        if review
                        else "Triage the synthetic incident using the approved read-only tools.",
                    )
                ],
            ),
            run_config=RunConfig(max_llm_calls=budget),
        ):
            record_event(
                "adk",
                "Google ADK orchestration event.",
                {
                    "framework": "google-adk",
                    "agent": event.author,
                    "event_id": event.id,
                    "invocation_id": event.invocation_id,
                    "function_calls": [call.name for call in event.get_function_calls()],
                    "function_responses": [
                        response.name for response in event.get_function_responses()
                    ],
                },
            )
            if event.error_code:
                raise ProviderError(
                    event.error_message or f"ADK invocation failed: {event.error_code}"
                )
            if event.is_final_response() and event.content and event.content.parts:
                text = "".join(part.text or "" for part in event.content.parts)
                if text:
                    plan = AgentStep.model_validate_json(text)
    except LlmCallsLimitExceededError as exc:
        raise ProviderError(
            "Agent exhausted its bounded step budget without a valid proposal"
        ) from exc
    finally:
        await runner.close()
        await sessions.delete_session(app_name=app_name, user_id=user_id, session_id=session.id)
    if plan is None or plan.tool != "propose":
        raise ProviderError("ADK did not return a valid final proposal")
    return plan, collected
