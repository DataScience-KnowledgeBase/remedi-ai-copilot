import asyncio
import json
import logging
import time
from pathlib import Path

from copilot import CopilotClient
from copilot._jsonrpc import JsonRpcError, ProcessExitedError
from copilot.generated.session_events import PermissionDeniedByRules
from copilot.session import PermissionInvocation
from copilot.session_events import (
    AssistantMessageData,
    AssistantUsageData,
    PermissionRequest,
    SessionEvent,
)

from app.config import Settings
from app.domain import AgentStep
from app.providers import SYSTEM, Completion, ProviderError

log = logging.getLogger("remedi.copilot")


def deny_permission(_request: PermissionRequest, _invocation: PermissionInvocation):
    return PermissionDeniedByRules(rules=[])


class CopilotInference:
    """One tool-less, isolated SDK conversation per bounded inference step."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: CopilotClient | None = None
        self.lock = asyncio.Lock()
        self.ready_at = 0.0

    async def _connect(self) -> CopilotClient:
        if self.client is not None:
            return self.client
        home = Path(self.settings.copilot_home).resolve()
        work = Path(self.settings.copilot_work_directory).resolve()
        home.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        token = self.settings.copilot_github_token.get_secret_value()
        client = CopilotClient(
            mode="empty",
            base_directory=str(home),
            working_directory=str(work),
            github_token=token or None,
            use_logged_in_user=not bool(token),
            session_idle_timeout_seconds=120,
        )
        try:
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                await client.start()
        except (OSError, RuntimeError, TimeoutError, JsonRpcError, ProcessExitedError) as exc:
            log.error("copilot_runtime_start_failed type=%s", type(exc).__name__)
            await client.stop()
            raise ProviderError(
                "Copilot runtime could not start; check installation and logs"
            ) from exc
        self.client = client
        return client

    async def ready(self) -> bool:
        async with self.lock:
            return await self._ready()

    async def _ready(self) -> bool:
        if not self.settings.copilot_allow_egress:
            raise ProviderError("Copilot egress is disabled")
        if time.monotonic() - self.ready_at < 30:
            return True
        client = await self._connect()
        try:
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                auth = await client.get_auth_status()
                if not auth.isAuthenticated:
                    raise ProviderError(
                        "Copilot sign-in required. Run scripts\\copilot-login.ps1 locally; "
                        "never paste credentials into chat."
                    )
                models = await client.list_models()
        except (OSError, RuntimeError, TimeoutError, JsonRpcError, ProcessExitedError) as exc:
            log.error("copilot_readiness_failed type=%s", type(exc).__name__)
            raise ProviderError("Copilot authentication/model discovery failed") from exc
        if not any(model.id == self.settings.copilot_model for model in models):
            raise ProviderError(
                "Configured COPILOT_MODEL is unavailable for this account; use scripts\\probe.ps1"
            )
        self.ready_at = time.monotonic()
        return True

    async def close(self):
        async with self.lock:
            if self.client is not None:
                try:
                    await self.client.stop()
                finally:
                    self.client = None
                    self.ready_at = 0

    async def complete(self, context: dict, schema: dict, started: float) -> Completion:
        async with self.lock:
            await self._ready()
            client = self.client
            if client is None:
                raise ProviderError("Copilot client is not connected")
            usage: list[AssistantUsageData] = []

            def observe(event: SessionEvent):
                if isinstance(event.data, AssistantUsageData):
                    usage.append(event.data)

            try:
                async with asyncio.timeout(self.settings.llm_timeout_seconds):
                    session = await client.create_session(
                        model=self.settings.copilot_model,
                        available_tools=[],
                        tools=[],
                        on_permission_request=deny_permission,
                        system_message={"mode": "replace", "content": SYSTEM},
                        working_directory=str(Path(self.settings.copilot_work_directory).resolve()),
                        skip_custom_instructions=True,
                        enable_config_discovery=False,
                        enable_skills=False,
                        enable_file_hooks=False,
                        enable_host_git_operations=False,
                        enable_session_store=False,
                        enable_on_demand_instruction_discovery=False,
                        mcp_servers={},
                        custom_agents=[],
                        streaming=False,
                        on_event=observe,
                    )
                    try:
                        reply = await session.send_and_wait(
                            json.dumps(context),
                            response_schema=schema,
                            timeout=self.settings.llm_timeout_seconds,
                        )
                    finally:
                        try:
                            await session.abort()
                        finally:
                            await session.disconnect()
            except (
                OSError,
                RuntimeError,
                TimeoutError,
                ValueError,
                JsonRpcError,
                ProcessExitedError,
            ) as exc:
                log.error("copilot_inference_failed type=%s", type(exc).__name__)
                self.ready_at = 0
                raise ProviderError(
                    f"Copilot inference failed ({type(exc).__name__}); no fallback provider used"
                ) from exc
            if reply is None or not isinstance(reply.data, AssistantMessageData):
                raise ProviderError("Copilot returned no final assistant message")
            step = AgentStep.model_validate_json(reply.data.content)
            if (
                step.tool not in schema["properties"]["tool"]["enum"]
                or step.action not in schema["properties"]["action"]["enum"]
            ):
                raise ProviderError("Copilot response violates the current workflow phase")
            inputs = (
                sum(item.input_tokens for item in usage if item.input_tokens is not None)
                if usage and all(item.input_tokens is not None for item in usage)
                else None
            )
            outputs = (
                sum(item.output_tokens for item in usage if item.output_tokens is not None)
                if usage and all(item.output_tokens is not None for item in usage)
                else None
            )
            billed = [
                item.copilot_usage.total_nano_aiu
                for item in usage
                if item.copilot_usage is not None
            ]
            return Completion(
                step=step,
                latency_ms=(time.perf_counter() - started) * 1000,
                input_tokens=inputs,
                output_tokens=outputs,
                estimated_usd=None,
                attempts=1,
                usage_events=len(usage),
                copilot_nano_aiu=sum(billed) if billed else None,
            )
