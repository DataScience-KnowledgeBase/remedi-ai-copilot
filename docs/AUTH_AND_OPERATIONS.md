# Authentication, billing and operations

## Opt-in Kubernetes access

[Real Kubernetes operations](KUBERNETES.md) use a separate short-lived
namespace-scoped service-account token, not the operator/owner tokens, Copilot
credential or administrator kubeconfig. Owner approval is mandatory for every
real change. Selected real pod/event/configuration evidence is sent to the model;
review those data-sharing boundaries before enabling the integration.
Token expiry, TLS/network failures and stale approvals stop explicitly.
Progress and rollout samples are durable audit events, not estimated percentages.

## Supported subscription sign-in

The official SDK supports signed-in Copilot users and documented server credentials.
The local CLI is pinned in `tools/package.json`, not installed globally. Its
`login --web-flow` command runs only when **you** invoke the login script.
`COPILOT_HOME` is set to this variant's `.runtime/copilot`; the SDK uses that same
base directory. The isolated home is ignored by Git and the Docker build context.

The SDK auth-status check is the source of truth. Neither VS Code sign-in nor
being able to use this chat proves that an unrelated SDK process is authenticated.
This implementation never reads VS Code credential stores or internal session tokens.

After sign-in, run `scripts\probe.ps1` to list available model IDs; choose the desired
account-authorized model explicitly. `gpt-4.1` is a configurable starting ID, **not**
a claim that every plan permits it. `scripts\probe.ps1 -Live` attempts one synthetic
call and records a completed or blocked report without printing credentials.

Docker does not inherit a Windows keychain. Use an approved supported server
credential/secret manager in accordance with GitHub's documentation. The pilot's
`COPILOT_GITHUB_TOKEN` setting passes a server-side secret to the SDK; it is not
exposed to React. Do not reuse arbitrary tokens, extract chat credentials, commit
tokens or paste them into chat. Organization/SaaS use needs a proper per-user
authorization model rather than a single shared token.

## Billing and metrics

| Mode | Cost/usage meaning |
|---|---|
| Copilot | Account entitlement/AI-credit/request terms apply; actual SDK usage events only; **not** configured API-token USD pricing |
| Generic API | Actual returned tokens × configured current input/output rates; separate provider bill |
| Demo | Fixture, no model inference; never a Copilot benchmark |
| Original GPU app | Separate local hardware inference; unaffected by this variant |

SDK `cost` is experimental and is deliberately **not interpreted as USD**.
Where present, `copilot_usage.total_nano_aiu` is retained as the SDK's own unit,
without converting it into money. Token counts omitted by the SDK remain `null`
in step events. Aggregates count/report calls with missing usage, and only sum
reported tokens. Unknown subscription cost is `null`, never "$0/free".

Traces record provider, requested model, prompt/runbook versions, step/tool, latency,
attempts, usage-event count, reported token counts and optional subscription usage.
No full raw SDK event dumps, identities, auth tokens or hidden reasoning are stored.
The UI includes a subscription label and reported-usage caveat.

Metrics remain at authenticated `/api/metrics`; counters reset with the process.
UI aggregates cover the most recent 100 incidents. Configure approved remote scraping,
log/trace retention and alerts for readiness, queue age, failures, escalations,
rollback, latency and account quotas. This pilot has no external telemetry exporter
or subscription-level hard billing enforcement.

## Lifecycle and failure handling

Claude structured outputs do not accept numeric bounds, string-length bounds,
array maximum lengths or array minimum lengths above one in the wire schema.
For `claude-*` models, the adapter moves
those flat AgentStep field constraints into descriptions before sending. Required
fields, types, enums and forbidden extra properties remain in the wire schema.
Every returned message is still validated against the original Pydantic model
and full original JSON Schema, including phase-specific citation counts and
observed-citation enums, before use. Invalid responses fail closed;
there is no relaxation of risk policy, automatic retry or provider substitution.

Only one backend worker is supported. Queued/approved incidents are durable;
interrupted in-flight work fails closed for owner investigation instead of replaying
potentially applied writes. Model errors never become successful simulated actions.

The SDK runtime is provisioned during setup/image build. Non-root Docker execution
has a readable pre-provisioned runtime cache and writes application/session data only
to the project volume/tmpfs. No local source workspace is available to model tools.
Check health with `docker compose ps` and `/readyz`; a signed-out provider correctly
makes the backend unhealthy even though the UI/liveness is available.

Stop only this project with `docker compose stop`. Do not run global cleanup or
`down -v` unless you intend to destroy this variant's data. Native server shutdown
uses Ctrl+C. The user-initiated login and native start scripts do not print tokens.

## Dependencies and security evidence

Runtime Python dependencies are locked with hashes in `backend/requirements.lock`.
Use `pip install --require-hashes -r ...` before installing the application with
`--no-deps`; development tools are separate. npm locks cover frontend and local CLI.
Python/npm audits are point-in-time dependency checks, not image/OS or full security
certification. SDK/model-service policy and availability can change independently.

The application has role-token authorization, queue/rate limits, validation, evidence
policy, approval replay prevention, simulated rollback and append-only audit triggers.
It does not implement SSO, per-person identity, full DLP, distributed quotas,
multi-tenancy, WORM storage or real infrastructure adapters. See the release gates.

Regex redaction is defense in depth, not a guarantee for arbitrary PII. Only
synthetic typed scenarios are accepted in this pilot. Before adding real logs or
documents, complete data classification, consent, retention/region review and DLP.
