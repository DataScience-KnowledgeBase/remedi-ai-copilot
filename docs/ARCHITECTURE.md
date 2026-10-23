# Architecture and release scope

**Current orchestration:** Google ADK `Runner`/`LlmAgent` and native read-only
function tools, with a custom `BaseLlm` adapter to the isolated Copilot SDK.
Dynamic proposals receive a second fresh ADK reviewer invocation. See
[dynamic cases, boundaries and evaluation](ADK_DYNAMIC.md).

## Independent variant

Only source/configuration templates were copied from the original application.
The new application has its own project name, virtual environment, dependency
locks, generated tokens, databases, Copilot home, frontend build and Docker volumes.
The GPU solution has its own independent ADK implementation. No GPU dependencies or model download
are needed for the Copilot version.

The reference diagrams' governed paths are unchanged:

```text
incident -> evidence-based triage -> deterministic decision
  known + low risk + reversible + verifiable:
    checkpoint -> allowlisted simulation -> independent verification -> close/learn
  high impact or low confidence:
    exact recommendation -> owner approval -> simulation -> verify/rollback -> learn
  unsupported or invalid evidence:
    escalate, never write
```

Notebook design principles retained: reproducible versioned data/prompt/runbooks,
ReAct tools/state/HITL, request/output schemas, async backend/SSE, idempotency,
evaluation gates, privacy/least privilege, cost/latency/usage observability,
container isolation and explicit recovery/deployment limits. Three exact runbooks do
not justify vector retrieval or fine-tuning. No classifier is "trained" on eight fixtures.

## Copilot inference boundary

`backend/app/copilot_provider.py` uses the official Python SDK, not private HTTP
endpoints. `CopilotClient(mode="empty", base_directory=..., working_directory=...)`
starts the managed JSON-RPC runtime. Readiness checks supported authentication and
whether the **configured** model is available; it never silently chooses a different model.

Each controller step opens a fresh session:

- Empty available tools and tool registrations; no filesystem/shell actions.
- No custom instruction/config discovery, skills, file hooks, host Git operations,
  session-store discovery, MCP servers or custom agents.
- A deny-all permission handler is installed as defense in depth.
- The isolated working directory contains no repository source or secrets.
- The system prompt contains the task's control protocol, not VS Code agent/session
  instructions. Only generated synthetic evidence is submitted.
- A phase-specific JSON schema is passed through `send_and_wait(response_schema=...)`.
  Returned JSON is validated again with Pydantic and the controller phase allowlist.
- Usage is collected from actual `AssistantUsageData` events, not estimated from text.
- Sessions are explicitly aborted/disconnected; the client is stopped on shutdown
  or after evaluation/probe. Calls are serialized and time-bounded.
- No automatic retry of subscription inference: a timeout may already be chargeable.
  The shared circuit breaker bounds repeated failures. There is no provider fallback.

SDK version 1.0.25 exposes some transport exceptions only via `copilot._jsonrpc`.
This narrow version-pinned import catches its concrete `JsonRpcError` and
`ProcessExitedError` types without a blanket exception swallow. Re-test on SDK upgrades.
The supported auth wrapper uses `GetAuthStatusResponse.isAuthenticated` (camelCase),
which is covered using the real SDK response type in tests.

## UI/backend behavior without provider auth

Application-token login is distinct from Copilot sign-in. The React connection card
shows `BLOCKED` and actionable local sign-in guidance. `/healthz` still reports liveness,
while `/readyz` and authenticated provider status return `503`. New remediation
requests are rejected before queue admission. Demo mode must be explicitly selected.

Native mode serves the React production build directly through FastAPI, bound to
loopback on 8081. Docker uses unprivileged nginx on 8082 and a private backend.
There are no host Docker socket mounts, Windows credential mounts, or published runtime ports.

## Production gates

This implementation is a single-identity simulation/pilot. Before production:

1. Resolve account entitlement, selected model permission, organization policy and
   allowed product use with GitHub. A subscription does not establish SaaS resale rights.
2. Add supported per-user OAuth/session credentials, short-lived rotation, OIDC/SSO/MFA,
   named accountable owners and tenant isolation. Never share a personal identity
   across unrelated users or confuse app roles with Copilot account attribution.
3. Move secrets to an approved manager, TLS/private ingress, egress allowlisting,
   unauthenticated brute-force protection and per-user budget/concurrency controls.
4. Connect signed monitoring ingestion and trusted/fresh evidence separate from
   untrusted logs. Require ownership, target and maintenance-window validation.
5. Build privilege-separated ITSM/executor adapters with durable idempotency/checkpoints,
   independent real health verification and tested rollback. The model must never
   receive arbitrary shell or credentials.
6. Use realistic privacy-reviewed held-out incidents, adversarial cases, load and chaos
   tests. Five synthetic cases cannot certify general safety or MTTR gains.
7. Move audit to off-host append-only/WORM storage; implement encrypted backups and
   restore drills with defined RPO/RTO. SQLite triggers do not prevent privileged tampering.
8. For scaling, migrate SQLite to PostgreSQL and leased queue/outbox before adding
   replicas; quotas/leases/idempotency must be distributed. The current worker is single-instance.
9. Pin/sign/scan base images and SDK runtime artifacts, retain SBOMs and model/license
   approvals, run dependency/security reviews and a staged shadow/canary rollout.
10. Only after an authenticated live benchmark and all real integration gates pass
    should owner-reviewed production changes be considered.
