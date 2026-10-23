# Validation and live-call status

**ADK update:** 42 backend tests, both explicit-fixture browser workflows and the
actual signed-out browser guard now pass. Google ADK owns real tool dispatch and
dynamic cases add a separate reviewer invocation. Live Copilot remains blocked.
See [current ADK details](ADK_DYNAMIC.md). Earlier counts below are historical.

Date: **2026-10-22**. This report describes the **new Copilot variant**, not the
original GPU benchmark.

## Verified

| Check | Result |
|---|---|
| Isolated Python environment | Installed under `backend/.venv`; no global SDK/CLI install |
| Official SDK | `github-copilot-sdk==1.0.25` |
| Official runtime / project-local CLI | `1.0.102` downloaded/installed |
| Runtime dependency lock | 24 runtime packages, hashed; development tools excluded |
| Python regression/SDK contract tests | **31 passed** |
| Ruff | **Passed** |
| React/TypeScript production build | **Passed** |
| Frontend dependency audit | **0 known vulnerabilities** at validation |
| Local CLI dependency audit | **0 known vulnerabilities** at installation |
| Python dependency audit | **0 known vulnerabilities**; local application itself is not published on PyPI and cannot be audited as a package |
| Independent Docker image build | **Passed**, including non-root runtime download |
| Non-root container SDK auth probe | Executed successfully; `isAuthenticated=False` |
| Explicit Docker demo browser workflow | **Passed**: recovery, owner gate, owner approval, audit download, telemetry navigation, mobile width, no browser errors |
| Real Copilot-mode signed-out browser behavior | **Passed**: blocked guidance, subscription label, `503` on admission, no queued job |
| Real Copilot model inference | **Blocked: user has not signed in to this runtime** |

The signed-out validation test passed while the actual-inference workflow test was
intentionally skipped. The demo workflow test was run separately with `PROVIDER=demo`
and was **not** counted as a successful Copilot model call.

Screenshots:
- [Actual Copilot auth gate](../reports/copilot-auth-required.png)
- [Explicit fixture workflow](../reports/dashboard.png)

## Live probe

The official SDK runtime started successfully on Windows and in the Linux container.
The public SDK authentication check returned **false**. The `--live` probe therefore
failed closed before any model inference; its [report](../reports/copilot-live-probe.json)
records `status: blocked`. No successful Copilot API inference, latency, token
usage, subscription charge, model quality or quota headroom is claimed.

No credentials or auth artifacts were copied from the original application or VS Code.
The account owner must complete the supported user-initiated flow:

```powershell
.\scripts\copilot-login.ps1
.\scripts\probe.ps1 -Live
.\scripts\start.ps1
```

For your interactive Windows subscription, this native path is recommended.
Docker requires its own supported server credential and does not inherit Windows
keychain sign-in. The model ID must be explicitly permitted by the account; use
the probe's available-model listing rather than assuming entitlement.

## Evaluation evidence

[Provider comparison JSON](../reports/comparison.json):

| Provider | Cases | Workflow gate | Interpretation |
|---|---:|---|---|
| Demo | 5 | Passed | Deterministic simulator/controller check, not an LLM |
| Copilot | 0 | Skipped | Supported SDK auth reports signed out |

The evaluator closes its client even on a skipped provider, uses a temporary
database, and distinguishes unknown subscription USD cost from zero. Real
subscription evaluation must be rerun after authentication; do not rank Copilot
against the original local GPU model using fixture numbers.

## Deployment at handoff

- Original GPU application remains unchanged on **http://127.0.0.1:8080**.
- New Docker UI is on **http://127.0.0.1:8082**, configured to **Copilot**, not demo.
- UI/liveness is reachable; Copilot readiness correctly remains **503** while signed out.
- Native server on **8081** was started to verify its static UI and signed-out
  provider gate, then stopped after the check. Run the native start script yourself
  after sign-in. It does not depend on the Docker UI.
- Demo test incidents use their own database; the actual Copilot-mode queue has
  no admitted jobs from signed-out requests.

## Limits

The SDK contract tests mock inference and exercise real SDK event/response types;
they are not evidence of hosted-model quality. Docker/browser smoke tests are not
a penetration test, load/chaos test, per-tenant identity audit, subscription licensing
approval or real infrastructure certification. The source retains explicit simulation
labels and production gates. Real monitoring/ITSM/executors, SSO/MFA, per-user Copilot
identity, secret management, immutable audit and held-out evaluation are still
required before unattended production remediation.
