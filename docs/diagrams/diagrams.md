# RemediAI Copilot - editable diagrams

Baseline: deployed commit `bec87e8`, 26 October 2026.
Scope: `remedi-ai-copilot` only. Real ADK orchestration and Copilot inference;
all infrastructure effects are simulated. These diagrams are not a claim of
production certification or live integrations with monitoring/ITSM platforms.

Open [the offline diagram viewer](index.html) for vector renderings.
The Mermaid sources below render directly on GitHub and in Mermaid-capable editors.

## 1. Solution architecture

```mermaid
flowchart LR
  subgraph LOCAL["Local machine / application trust boundary"]
    OP["Operator: submit synthetic incidents"]
    OWNER["Accountable owner: approve / reject"]
    UI["React / TypeScript console<br/>nginx - localhost:8082"]
    API["FastAPI<br/>Auth, schemas, limits, idempotency"]
    ENGINE["Single asynchronous worker"]
    DB[("SQLite persistent volume<br/>Queue, state, audit, feedback")]
    ADK["Google ADK Runner / LlmAgent<br/>Bounded evidence-action loop"]
    TOOLS["Native read-only functions<br/>inspect_metrics / read_runbook"]
    REVIEW["Dynamic cases only<br/>Fresh ADK reviewer invocation"]
    SDK["Custom BaseLlm bridge<br/>Copilot SDK / JSON-RPC runtime"]
    POLICY["Deterministic policy<br/>Risk, confidence, evidence, review"]
    SIM["Allowlisted simulator<br/>Checkpoint / action / 3 probes"]
    OP --> UI
    OWNER --> UI
    UI <-->|REST / authenticated SSE| API
    API --> DB
    DB -->|Queued job| ENGINE
    ENGINE --> ADK
    ADK <-->|Observed evidence| TOOLS
    ADK <-->|Typed inference| SDK
    ADK --> POLICY
    POLICY -->|Dynamic, not escalated| REVIEW
    REVIEW <-->|Same model, fresh session| SDK
    REVIEW --> POLICY
    POLICY -->|Owner gate when required| UI
    POLICY -->|Eligible / owner approved| SIM
    ENGINE -->|Decisions / telemetry| DB
    SIM -->|Outcome / learning candidate| DB
  end
  SERVICE["GitHub Copilot service<br/>claude-sonnet-5 baseline"]
  SDK <-->|HTTPS / synthetic evidence only| SERVICE
```

The SDK receives no model tools, shell, filesystem, host Git, skills or MCP access.
Only deterministic application policy can permit an action. Dynamic review uses
the same model in a fresh invocation, not an independent expert or separate provider.
The database has append-only audit triggers, not off-host tamper-proof storage.

## 2. Technical deployment and CI/CD

```mermaid
flowchart TB
  subgraph GH["GitHub"]
    MIRROR["Private ADK source mirror<br/>Actions disabled"]
    REPO["Private remedi-ai-copilot<br/>Vendored wheel + hashes + provenance"]
    CI["Hosted Ubuntu CI<br/>Lint / tests / audits / frontend build"]
    FIXTURE["Docker fixture stack<br/>Playwright governance tests"]
    GATE{"CI passed?<br/>main and not PR?"}
    MIRROR -.->|Maintainer builds pinned wheel| REPO
    REPO -->|Push / PR / manual| CI
    CI --> FIXTURE --> GATE
  end
  subgraph WIN["Windows local machine"]
    RUNNER["Self-hosted Windows runner<br/>PowerShell 5.1 / host Docker CLI"]
    ENV["Private .env outside checkout<br/>Repository variable stores path only"]
    BUILD["Build SHA-tagged images<br/>Preserve old images for rollback"]
    subgraph DOCKER["Docker Desktop Linux engine"]
      FRONT["Frontend container<br/>React static build + nginx:8080"]
      BACK["Backend:8000 / private network<br/>FastAPI + ADK + Copilot SDK"]
      VOL[("incident-data volume<br/>SQLite + isolated runtime state")]
      FRONT -->|/api and /readyz| BACK
      BACK --> VOL
    end
    VERIFY{"Health + SHA revision + contract<br/>Optional live incident smoke"}
    OK["Deployment successful<br/>Localhost:8082"]
    ROLLBACK["Restore previous images when available<br/>Preserve volume; fail workflow"]
    ENV -.->|Compose env_file| BACK
    RUNNER --> BUILD --> DOCKER
    DOCKER --> VERIFY
    VERIFY -->|Pass| OK
    VERIFY -->|Fail| ROLLBACK
  end
  GATE -->|Yes| RUNNER
  GATE -->|No| STOP["No local deployment"]
  BROWSER["Local browser"] -->|127.0.0.1:8082| FRONT
  BACK <-->|HTTPS| COPILOT["External GitHub Copilot service"]
```

Baseline runtime: Python 3.12, React 19, Node 22.20 build, Copilot SDK 1.0.25,
private ADK 2.12.0+remedi.1 and OpenTelemetry API/SDK 1.45.1.
Normal dependency resolution and hashed runtime restores apply. The local-version
ADK artifact is verified and its upstream base is separately audited.

CI uses disposable fixture credentials; it receives no personal Copilot secret.
Only trusted main deployments use the local runner. The optional live smoke
consumes subscription allowance and creates a synthetic low-risk incident;
it does not approve high-impact changes. Image rollback is not database rollback.

## 3. Incident workflow

```mermaid
flowchart TD
  A["Operator submits validated synthetic incident"] --> B{"Copilot ready?<br/>Admission checks pass?"}
  B -->|No| BLOCK["Reject / no queue admission<br/>Provider unavailable: 503"]
  B -->|Yes| Q["Durable queue / single worker"]
  Q --> T["ADK bounded triage<br/>Inspect metrics / read runbook / propose"]
  T --> V["Pydantic + full original phase JSON Schema<br/>Validate bounds, enums and citations"]
  V --> P{"Evidence/action policy"}
  P -->|Unsupported| ESC["Escalate / no execution"]
  P -->|Supported| D{"Dynamic scenario?"}
  D -->|Yes| REVIEW["Fresh ADK reviewer invocation"]
  D -->|No| RISK
  REVIEW --> RISK{"Low risk, sufficient confidence<br/>and valid review when required?"}
  RISK -->|Yes| EXEC
  RISK -->|No| OWNER["Await accountable owner approval"]
  OWNER -->|Approve exact plan| EXEC["Recheck execution policy<br/>Save checkpoint / simulate allowlisted action"]
  OWNER -->|Reject| REJECT["Rejected / no execution"]
  EXEC --> PROBE["Three independent simulated health samples"]
  PROBE --> HEALTH{"Recovered?"}
  HEALTH -->|Yes| CLOSE["Closed"]
  HEALTH -->|No| RESTORE["Restore checkpoint / verify rollback<br/>Rolled back / owner escalation"]
  CLOSE --> LEARN["Record outcome and runbook-learning candidate<br/>Changes require offline review"]
  RESTORE --> LEARN
  T -.->|Provider / schema / runtime failure| FAIL["Fail closed / operator review"]
  V -.->|Invalid output| FAIL
  REVIEW -.->|Reviewer failure| FAIL
```

High-impact worker/capacity changes remain owner-gated regardless of confidence.
Reviewer disagreement or low confidence also requires the owner. No model can
assert recovery, approve a change or grant itself additional privileges.

## Implementation references

- [Architecture and scope](../ARCHITECTURE.md)
- [ADK orchestration](../ADK_DYNAMIC.md)
- [CI/CD and maintenance](../CICD.md)
- [Compose topology](../../compose.yaml)
- [Agent engine](../../backend/app/engine.py)
- [ADK bridge](../../backend/app/adk_runtime.py)
- [Copilot adapter and schema boundary](../../backend/app/copilot_provider.py)
- [Actions pipeline](../../.github/workflows/pipeline.yml)
- [Deployment and rollback](../../scripts/deploy.ps1)

The original `AI_Remedy_1` and `AI_Remedy_2` concepts map to triage, the approved
risk boundary, owner approval, execution, verification, rollback, audit and
reviewed learning. No automatic runbook deployment or real-system self-healing
is implied by the diagrams.
