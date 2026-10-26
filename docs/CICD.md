# GitHub Actions and local Docker deployment

The `CI and local Docker deployment` workflow runs on pushes to `main`, pull
requests targeting `main`, and manual dispatches. CI runs on GitHub-hosted Linux:
hashed runtime restore, backend lint/tests, dependency audits, frontend type/build
checks, Docker builds, and Playwright governance tests against explicit fixtures.
Fixture results are not live Copilot inference. CI never receives local credentials.

## Approved ADK compatibility build

The upstream ADK cap excluded the published OpenTelemetry security fix. With
explicit owner approval, this variant uses a private Apache-2.0 source mirror:
`https://github.com/DataScience-KnowledgeBase/google-adk-remedi`, pinned in
`backend/vendor/provenance.json`. Only version metadata and OpenTelemetry bounds
are changed; no runtime logic is modified. The wheel is vendored so hosted CI
does not need credentials for another private repository.

Runtime install commands must pass `--find-links backend/vendor` (or `vendor`
inside the Docker backend build). All runtime dependencies, including the private
wheel, have hashes in the lock. `scripts/verify_vendor.py` checks artifact identity,
license presence and the patched dependency floor. `pip check` enforces ordinary
dependency consistency. No resolver bypass or advisory exclusion is used.

Because PyPI vulnerability services do not recognize local build versions,
`pip-audit --local` cannot audit the private ADK version itself. A separate audit
of `backend/vendor/upstream-audit.txt` checks the unchanged upstream runtime base,
while the local audit checks the actual installed OpenTelemetry and all other
public dependencies. This is not a complete source-code security audit.
Regression tests also verify bounded malformed-tracestate parsing and SDK span
export/context propagation. The owner maintains the fork and must update its
source, wheel, provenance and lock together, returning to upstream when compatible.

Only a successful `main` CI run can deploy. Deployment uses the Windows runner
labels `self-hosted`, `Windows`, `X64`, `local-docker`, `remedi-ai-copilot` and
Windows PowerShell 5.1. Pull requests never execute on the local runner. Only
trusted maintainers should have workflow write or dispatch access.

## Machine prerequisites

- Keep the registered runner, Windows session and Docker Desktop Linux engine running.
- Set repository Actions variable `REMEDI_LOCAL_ENV_FILE` to the absolute path of
  this machine's private `.env`. This is a path, not a credential.
- Provision that file locally with distinct operator/owner tokens,
  `PROVIDER=copilot`, `COPILOT_ALLOW_EGRESS=true`, an authorized `COPILOT_MODEL`,
  and a supported Copilot credential. Never commit/upload the file.
- The runner account must be able to read that file and use Docker.
- Keep localhost port 8082 available for this deployment.

Compose accepts `REMEDI_ENV_FILE` to read the private configuration in place,
outside the runner checkout, and `REMEDI_IMAGE_TAG` for commit-specific images.
The default values preserve manual local Compose usage.

## Execution and verification

Push to `main` for CI followed by deployment. Alternatively open Actions, select
the workflow and **Run workflow** on `main`. Select `live_smoke` only when you
authorize subscription usage: it creates one synthetic low-risk cache incident,
which can require multiple bounded model calls. It does not approve changes.

Deployment builds the tested commit into SHA-tagged images, preserves previous
image IDs under rollback tags, updates only this Compose project, waits for
container health, verifies HTTP readiness and image revision labels, and checks
authenticated configuration for live Copilot plus Google ADK and disabled model
tools. The optional live check additionally requires a closed incident, ADK audit
events, consistent live inference metadata and three recovery samples.

No secret or raw model output is printed. Logs show only deployment status and
safe verification summaries. Existing incident volumes are preserved.

If deployment or smoke verification fails, both prior images are restored when
a complete previous deployment exists. The workflow still fails. Rollback
preserves the database; it is not a database backup, migration rollback or
reversal of audit entries/live model usage. Back up data before incompatible
schema changes. Failed builds leave the running deployment untouched.

The local private configuration is intentionally not versioned. Its changes do
not trigger Actions. An Actions checkout is separate from your development
folder; deploys never copy credentials into that checkout or image.

Retain rollback tags until an operator no longer needs them; there is no automatic
image pruning or volume deletion. Runner registration does not provide Copilot
authentication, and the workflow never substitutes demo or another paid provider
for a failed live deployment.
