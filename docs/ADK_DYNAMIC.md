# Google ADK, dynamic diagnosis and Copilot inference

## Real ADK orchestration

`backend/app/adk_runtime.py` creates an ADK `LlmAgent`, `Runner`, scoped transient
session and real native `inspect_metrics()` / `read_runbook(action)` tools.
A custom `BaseLlm` projects typed provider decisions into ADK function calls;
ADK dispatches tools and owns continuation. The previous hand-written triage loop
is replaced, not merely renamed. The application persists ADK invocation/event IDs
and read-only observations in its durable SQLite audit.

ADK does not imply Gemini inference: no Google model key is required. The provider
still uses the official isolated Copilot SDK, or an explicitly selected alternate
provider. Copilot's built-in tools remain disabled. A subscription is not a generic
OpenAI API key, and VS Code sign-in is not harvested or reused.

## Dynamic cases and gates

| Scenario | Model's causal task | Human involvement |
|---|---|---|
| `dynamic_cache` | Explain stale cached schema despite utilization below the old threshold; choose a remedy | Low-risk recovery only with valid citations, adequate confidence and separate reviewer agreement |
| `dynamic_worker` | Distinguish a deadlocked worker from incidental high but healthy cache utilization | Owner approval before restart |
| `dynamic_capacity` | Distinguish demand saturation from a deadlock; recommend scale-out rather than restarting healthy workers | Owner approval of cost/impact; reversible worker-count checkpoint |

Expected-action labels are never included in provider context and do not select
dynamic policy decisions. They score offline evaluations and drive the simulator's
causal recovery behavior. Wrong remedies fail probes and restore their checkpoint.
The three examples are synthetic, curated scenarios, not a production ingestion API.

Dynamic triage requires evidence inspection and a real reviewed runbook lookup,
then a second ADK reviewer invocation with a fresh session. The reviewer judges its
own action/confidence/citations; it cannot approve a write or assert recovery.
Disagreement/low confidence routes to owner approval. Reviewer failure fails closed.
Risk/severity gates always require owners for worker/capacity changes, even when
both models agree. Owner rejection means no write. Execution rechecks persisted
review, citations and approval; three independent simulated probes determine closure.

The reviewer currently uses the same configured model. This is not independent
expert validation or calibrated safety: the GPU variant's early candidate reviewer
agreed with an incorrect capacity remedy. The final smoke gate therefore requires
correct actions AND correct outcomes, not status alone. Expand with held-out,
adversarial and operational cases before promoting any model.

## Validation and honest provider comparison

Backend contract/regression tests cover real ADK dispatch, separate reviewer
invocations, missing review, uncertainty/disagreement/failure, owner gates, replay,
causal recovery and checkpoint restoration. Fixture inference is labeled `demo`
and is not live LLM quality.

The original comparison baseline was blocked by account authentication and is not
live inference evidence. Current environment readiness and live inference must be
verified separately through the CI/CD checks described in `CICD.md`; no
quality/latency/cost benchmark is inferred from a smoke test. Native login does
not authenticate the separate Linux container. For native operation use:

```powershell
.\scripts\copilot-login.ps1
.\scripts\probe.ps1 -Live
```

Then benchmark the authenticated deployment with `python -m app.evaluate --providers
copilot --repeats 2 --output reports/adk-comparison.json` from the project root.
Do not send real incident data without an approved data-handling/egress review.
Unavailable providers are reported as skipped, never silently replaced by fixtures.

For context, the local-GPU sibling's final ADK benchmark passed 16/16 synthetic
action/workflow cases, with six successful dynamic reviewer decisions and zero
unauthorized executions. Its p50/p95 were 4,986/7,186 ms with 39,018 input and
3,222 output tokens. These are **local model results**, not Copilot results.

Reviewer calls are included in reason-event telemetry, token counts and configured
cost estimates. Optional SDK usage remains unknown when absent; Copilot credits
are not converted into fictitious USD. Successful-call trace completeness does
not establish billing completeness for failed requests.

## Dependencies, CI/CD and production limits

The approved private ADK `2.12.0+remedi.1` build changes only its version and
OpenTelemetry dependency bounds to admit patched API/SDK `>=1.45.1,<1.46`.
Upstream ADK 2.12.0 caps OpenTelemetry below the security fix. This is not an
official Google release; source provenance and wheel hashes are retained in
`backend/vendor`. Restore the hashed lock with `--find-links backend/vendor`,
then install the editable project separately;
do not combine an editable install with a hashed constraints file.

The platform repository includes matrix CI, dependency audits, explicit fixture
quality gates, React and Docker/browser tests, trusted GHCR publishing and a
human-requested digest-pinned deployment bundle. No paid personal model identity
is exposed to PR workflows. Required environment reviewers depend on GitHub plan
and repository settings; no unspecified production target is automatically changed.

ADK sessions are scoped in-memory and cleaned up after every invocation; SQLite
retains durable workflow/audit state. This is not a distributed ADK session service.
Read [architecture and production gates](ARCHITECTURE.md) before real deployment.
