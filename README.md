# RemediAI — Copilot subscription / LLM API edition

A separate, self-contained variant of the original RemediAI application. It retains
the React UI, Google ADK bounded ReAct workflow, deterministic policy, approvals, simulated
execution, recovery verification, rollback, audit and observability. It replaces
the GPU inference dependency with the **official GitHub Copilot Python SDK**.
The GPU and Copilot variants keep independent credentials, models and databases.
Both now use **Google ADK 2.12.0** orchestration and separate LLM review for dynamic
cases. See [ADK and dynamic remediation](docs/ADK_DYNAMIC.md).

## Important: subscription access is not a generic API key

The supported request path is:

```text
React UI -> FastAPI -> Google ADK Runner/LlmAgent -> Copilot Python SDK
         -> managed Copilot runtime (JSON-RPC) -> GitHub Copilot model service
```

It does **not** extract VS Code tokens, impersonate this chat session, call private
Copilot endpoints, or turn a subscription into an OpenAI-compatible API key.
Your account must sign in using the supported OAuth flow, be entitled to Copilot,
and allow the selected model under organization policy. Subscription/AI-credit
limits and current GitHub terms apply. It is not an unlimited/free token API.

**Current validation:** application, Docker runtime, contract tests and demo browser
workflow work. The supported live auth probe reports **signed out**. No successful
subscription-backed inference is claimed. See [measured status](docs/VALIDATION.md).

## Quick start using your own Copilot subscription (recommended local pilot)

Requirements: Python 3.12+, Node 22.20+, internet access and an eligible GitHub
Copilot account. No GPU, Ollama or paid OpenAI API key is needed.
Run from this folder in PowerShell:

```powershell
.\scripts\setup.ps1
.\scripts\copilot-login.ps1
.\scripts\probe.ps1
.\scripts\probe.ps1 -Live
.\scripts\start.ps1
```

- Setup restores hashed Python runtime dependencies and project-local frontend/CLI
  packages; it never installs the CLI globally or automatically logs you in.
- Sign-in opens GitHub's browser flow with the project-specific `COPILOT_HOME`.
  Sign in yourself. **Never paste credentials, device codes or tokens into chat.**
- Probe prints supported auth status and available model IDs, not identity secrets.
  If necessary, set `COPILOT_MODEL` in private `.env` to an ID available to your account.
  No unavailable-model substitution is performed.
- `-Live` attempts **one synthetic inference**, consuming your entitlement if
  authenticated, and saves a report. It never counts a fixture as live inference.
- Start serves the built UI and API on **http://127.0.0.1:8081**.
  If already running while you sign in, restart the application afterward.

Generated private `.env` contains **new, independent** `OPERATOR_TOKEN` and
`OWNER_TOKEN` values. Open it locally to connect to the UI: operator creates
synthetic incidents; owner approves/rejects high-impact changes. These app tokens
are not GitHub/Copilot credentials. Browser storage never persists them.

Try dynamic schema mismatch (low-risk recovery after LLM review), dynamic worker
deadlock and demand saturation (LLM recommendation plus owner approval), or the five
legacy regression scenarios. Demo results remain deterministic fixtures, not live inference.
Expanding a trace event shows the returned operational explanation, observations,
versions, latency and **reported** usage. Hidden chain-of-thought is not requested.

## What is production-oriented here?

- A model can only return typed recommendations. **No Copilot shell, filesystem,
  web, MCP, Git, skills, plugins or ambient repository instructions are enabled.**
- `mode="empty"`, explicit empty tool allowlist, disabled discovery/hooks/skills,
  denial of all permission requests, isolated empty working directory.
- One fresh SDK conversation per inference step; bounded timeout, explicit abort/
  disconnect, lifecycle cleanup, no automatic paid-call retries or fallback provider.
- Metrics first; known signatures require reviewed runbook evidence before proposals.
  Deterministic policy checks evidence, action compatibility and risk again before writes.
- Low-risk automatic path and owner-gated impact/confidence path; independent synthetic
  health checks and actual simulated checkpoint restoration on verification failure.
- SQLite durable queue, transactional state/audit, idempotency, approval replay defense,
  bounded capacity, rate limits and interrupted-state fail-closed recovery.
- Native signed-out health is explicit: UI/liveness remain accessible, readiness is
  `503`, and new remediation jobs are rejected before queue admission.
- Hashed runtime lock, npm locks, dependency audits, Docker non-root image and health probes.

**Scope:** production-oriented **single-identity pilot with simulated infrastructure**,
not a certified production remediation service or multi-tenant SaaS. Copilot's
subscription identity is separate from the demo's operator/owner application roles.
For shared production, implement per-user supported OAuth identity/session credentials,
SSO/MFA, organization billing/policy, named approvers, external audit and real least-
privilege executors. Do not share one personal subscription across unrelated users.
See [architecture and release gates](docs/ARCHITECTURE.md).

## Docker

Docker has a **separate authentication context** from Windows keychain/CLI sign-in.
No host credential directories are copied or mounted into the image.

```powershell
# First provision an approved supported credential through a secret manager.
# For this local pilot only, .env documents COPILOT_GITHUB_TOKEN as the server-side
# injection point. It must have Copilot access; an arbitrary GitHub PAT is not enough.
docker compose up -d --build
```

Docker UI: **http://127.0.0.1:8082**. With no server credential it deliberately shows
the sign-in gate and fails readiness. For your interactive subscription, use the
native sign-in/start commands above rather than copying Windows auth stores.
Real production should replace `.env` secret injection with a supported secret manager
and per-request identity model, subject to GitHub's current authentication guidance.

### Explicit fixture-only Docker demonstration

```powershell
docker compose -f compose.yaml -f compose.demo.yaml up -d --build
```

This sets `PROVIDER=demo`, disables Copilot egress and uses a separate demo database.
The UI prominently says `demo` / `Fixture`. It is **not** a successful live LLM call.
Restore the real provider with `docker compose up -d --no-deps backend`.

The original application remains on **8080**, native variant uses **8081**, and
the independent Docker variant uses **8082**. Named volumes and Compose project names
are separate. Never use global Docker cleanup or copy the original application's `.env`.

## Generic paid LLM API alternative

The existing strict-schema OpenAI-compatible adapter is retained:

```dotenv
# PROVIDER=api
# ALLOW_API_EGRESS=true
# API_BASE_URL=https://api.openai.com/v1
# API_MODEL=<supported-model-id>
# API_KEY=<server-side-secret>
# INPUT_USD_PER_MILLION=<verified-current-price>
# OUTPUT_USD_PER_MILLION=<verified-current-price>
```

This alternative is billed by that provider, **not your Copilot subscription**.
No automatic Copilot-to-paid-API fallback is permitted.

## Validation and evaluation

Install optional development tools into the isolated environment:

```powershell
.\backend\.venv\Scripts\python.exe -m pip install --require-hashes -r backend\requirements.lock
.\backend\.venv\Scripts\python.exe -m pip install -e ".\backend[dev]"
.\scripts\validate.ps1
.\backend\.venv\Scripts\python.exe -m app.evaluate --providers demo copilot --repeats 2 --output reports\comparison.json
```

Evaluation uses a temporary database. It marks unsigned-in Copilot as **skipped**,
never as a passed fixture. Once authenticated, rerun it for actual action/workflow
accuracy, citations, unauthorized actions, latency, usage and telemetry completeness.
The synthetic smoke set is not a generalization/safety certification.

Browser validation against the explicit Docker demo:

```powershell
Set-Location frontend
npx playwright install chromium
$env:REMEDI_URL="http://127.0.0.1:8082"
$env:REMEDI_EXPECTED_PROVIDER="demo"
npm test
```

For signed-out UI validation, set `REMEDI_EXPECT_SIGNED_OUT=true`. To test actual
authenticated native inference, clear that variable, set `REMEDI_URL` to 8081 and
`REMEDI_EXPECTED_PROVIDER=copilot`. Do not label demo browser results as Copilot quality.

## Documentation and evidence

- [Architecture, isolation and production release gates](docs/ARCHITECTURE.md)
- [Authentication, billing, security and observability](docs/AUTH_AND_OPERATIONS.md)
- [Measured validation and current live-call blocker](docs/VALIDATION.md)
- [Actual live-probe report](reports/copilot-live-probe.json)
- [Provider evaluation report](reports/comparison.json)

Official references:
[SDK](https://github.com/github/copilot-sdk),
[authentication](https://github.com/github/copilot-sdk/blob/main/docs/auth/authenticate.md),
[shared-server isolation](https://github.com/github/copilot-sdk/blob/main/docs/setup/multi-tenancy.md).
Implementation is pinned to Python SDK **1.0.25** and runtime/CLI **1.0.102**.
Review current support status and licensing before an enterprise rollout.
