import asyncio
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import Settings, get_settings
from app.domain import (
    PROMPT_VERSION,
    RUNBOOK_VERSION,
    RUNBOOKS,
    ApprovalInput,
    IncidentInput,
)
from app.engine import OUTCOMES, Engine
from app.providers import Provider, ProviderError
from app.store import TERMINAL, Conflict, Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
security = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(config.database_path)
        provider = Provider(config)
        engine = Engine(store, config, provider)
        app.state.store, app.state.provider, app.state.engine = store, provider, engine
        engine.start()
        try:
            yield
        finally:
            try:
                await engine.stop()
            finally:
                try:
                    await provider.close()
                finally:
                    store.close()

    app = FastAPI(
        title="RemediAI Copilot API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    rate: dict[str, deque] = defaultdict(deque)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(_request: Request, exc: Conflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def missing_handler(_request: Request, _exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": "Incident not found"})

    async def role(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ) -> str:
        if credentials is None:
            raise HTTPException(
                401, "Bearer authentication required", headers={"WWW-Authenticate": "Bearer"}
            )
        token = credentials.credentials
        if secrets.compare_digest(token, config.owner_token.get_secret_value()):
            identity = "owner"
        elif secrets.compare_digest(token, config.operator_token.get_secret_value()):
            identity = "operator"
        else:
            raise HTTPException(401, "Invalid bearer token")
        stamp = time.monotonic()
        bucket = rate[identity]
        while bucket and bucket[0] < stamp - 60:
            bucket.popleft()
        if len(bucket) >= config.requests_per_minute:
            raise HTTPException(429, "Request limit exceeded", headers={"Retry-After": "60"})
        bucket.append(stamp)
        return identity

    Role = Annotated[str, Depends(role)]

    @app.get("/healthz")
    async def health():
        return {"status": "alive"}

    @app.get("/readyz")
    async def ready():
        engine = app.state.engine
        if engine.task is None or engine.task.done():
            raise HTTPException(503, "Worker is not running")
        try:
            available = await app.state.provider.ready()
        except ProviderError as exc:
            raise HTTPException(503, "Configured provider unavailable") from exc
        if not available:
            raise HTTPException(503, "Configured model is not loaded")
        return {"status": "ready", "provider": config.provider}

    @app.get("/api/config")
    async def configuration(identity: Role):
        return {
            "role": identity,
            "provider": config.provider,
            "model": app.state.provider.model,
            "simulation_only": True,
            "api_egress": config.copilot_allow_egress
            if config.provider == "copilot"
            else config.allow_api_egress,
            "confidence_threshold": config.confidence_threshold,
            "max_agent_steps": config.max_agent_steps,
            "orchestrator": "google-adk",
            "dynamic_reviewer_max_calls": 1,
            "billing_basis": "copilot_subscription"
            if config.provider == "copilot"
            else "configured_rates",
            "copilot_tool_access": "disabled",
            "prompt_version": PROMPT_VERSION,
            "runbook_version": RUNBOOK_VERSION,
            "pricing": {
                "input_usd_per_million": config.input_usd_per_million,
                "output_usd_per_million": config.output_usd_per_million,
                "gpu_usd_per_hour": config.local_gpu_usd_per_hour,
            },
        }

    @app.get("/api/provider-status")
    async def provider_status(_identity: Role):
        try:
            available = await app.state.provider.ready()
            return {
                "ready": available,
                "provider": config.provider,
                "message": "Configured provider ready" if available else "Model unavailable",
            }
        except ProviderError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "ready": False,
                    "provider": config.provider,
                    "message": str(exc),
                },
            )

    @app.post("/api/incidents", status_code=202)
    async def create_incident(
        body: IncidentInput,
        identity: Role,
        idempotency_key: Annotated[str, Header(min_length=8, max_length=100)],
    ):
        if identity != "operator":
            raise HTTPException(403, "Only operators ingest incidents; owners approve")
        if config.provider == "copilot":
            try:
                await app.state.provider.ready()
            except ProviderError as exc:
                raise HTTPException(503, str(exc)) from exc
        item, created = app.state.store.create(
            body.scenario,
            idempotency_key,
            config.provider,
            app.state.provider.model,
            config.max_active_incidents,
        )
        return {"incident": item, "created": created}

    @app.get("/api/incidents")
    async def incidents(_identity: Role, limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return app.state.store.list(limit)

    @app.get("/api/incidents/{incident_id}")
    async def incident(incident_id: str, _identity: Role):
        return app.state.store.get(incident_id)

    @app.get("/api/incidents/{incident_id}/events")
    async def events(incident_id: str, _identity: Role, after: int = Query(default=0, ge=0)):
        return app.state.store.events(incident_id, after)

    @app.get("/api/incidents/{incident_id}/stream")
    async def stream(
        incident_id: str,
        request: Request,
        _identity: Role,
        after: int = Query(default=0, ge=0),
    ):
        app.state.store.get(incident_id)

        async def generate():
            cursor = after
            deadline = time.monotonic() + 1200
            while time.monotonic() < deadline:
                if await request.is_disconnected():
                    return
                for event in app.state.store.events(incident_id, cursor):
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: audit\ndata: {json.dumps(event)}\n\n"
                item = app.state.store.get(incident_id)
                yield f"event: status\ndata: {json.dumps(item)}\n\n"
                if item["status"] in TERMINAL:
                    return
                await asyncio.sleep(1)

        return StreamingResponse(
            generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.post("/api/incidents/{incident_id}/approval")
    async def approve(incident_id: str, body: ApprovalInput, identity: Role):
        if identity != "owner":
            raise HTTPException(403, "Accountable owner role required")
        status = "approved" if body.decision == "approve" else "rejected"
        app.state.store.transition(
            incident_id,
            {"awaiting_approval"},
            status,
            "approval",
            body.reason,
            actor="owner",
            payload={"decision": body.decision, "scope": "exact persisted proposal"},
        )
        if status == "rejected":
            OUTCOMES.labels(status).inc()
        return app.state.store.get(incident_id)

    @app.get("/api/runbooks")
    async def runbooks(_identity: Role):
        return {"version": RUNBOOK_VERSION, "items": list(RUNBOOKS.values())}

    @app.get("/api/feedback")
    async def feedback(_identity: Role):
        return app.state.store.feedback()

    @app.get("/api/telemetry")
    async def telemetry(_identity: Role):
        rows = app.state.store.list(100)
        calls = [
            event["payload"]
            for item in rows
            for event in app.state.store.events(item["id"])
            if event["stage"] == "reason"
        ]
        latencies = sorted(call["latency_ms"] for call in calls)
        return {
            "scope": "most_recent_100_incidents",
            "incident_count": len(rows),
            "llm_calls": len(calls),
            "input_tokens": sum(call["input_tokens"] or 0 for call in calls),
            "output_tokens": sum(call["output_tokens"] or 0 for call in calls),
            "estimated_usd": sum(call["estimated_usd"] or 0 for call in calls)
            if config.provider != "copilot"
            else None,
            "calls_with_missing_usage": sum(
                call["input_tokens"] is None or call["output_tokens"] is None for call in calls
            ),
            "copilot_nano_aiu": sum(call.get("copilot_nano_aiu") or 0 for call in calls)
            if any(call.get("copilot_nano_aiu") is not None for call in calls)
            else None,
            "p95_llm_ms": latencies[max(0, int(len(latencies) * 0.95 + 0.999) - 1)]
            if latencies
            else None,
            "outcomes": {
                status: sum(item["status"] == status for item in rows)
                for status in sorted(TERMINAL | {"awaiting_approval", "queued"})
            },
        }

    @app.get("/api/metrics")
    async def metrics(_identity: Role):
        return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    static = Path(config.frontend_dist)
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
    return app
