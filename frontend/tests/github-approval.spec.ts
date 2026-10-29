import { test, expect } from "@playwright/test";

for (const state of ["proposal", "legacy", "awaiting_merge", "awaiting_ci", "api_error"]) {
  test(`repository approval action: ${state} (mock APIs, no real PRs)`, async ({ page }) => {
    let status = ["awaiting_merge", "awaiting_ci"].includes(state) ? state : "awaiting_approval";
    const incident = () => ({
      id: "gitops-fixture", title: "Kubernetes: OOMKilled", service: "remedi-oom",
      scenario: "k8s_oom", severity: "high", status, created_at: new Date().toISOString(),
      provider: "copilot", model: "fixture", error: null,
      plan: { action: "increase_pod_memory", confidence: 0.95,
        evidence_ids: ["EV-METRICS-01", "RB-K8S-MEMORY-01"], summary: "OOM evidence and repo memory limit agree." },
    });
    const events = () => [
      ...(state === "legacy" ? [] : [{ stage: "repository_plan", payload: {
        plan: { diff: "- memory: 32Mi\n+ memory: 128Mi" },
      } }]),
      ...(["awaiting_merge", "awaiting_ci"].includes(status) ? [{ stage: "github_pr", payload: {
        number: 42, url: "https://github.com/DataScience-KnowledgeBase/remedi-ai-copilot/pull/42",
      } }] : []),
    ].map((event, index) => ({
      ...event, seq: index + 1, actor: "agent", message: event.stage, at: new Date().toISOString(),
    }));
    let writes = 0;
    await page.route("**/api/**", async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path.endsWith("/config")) return route.fulfill({ json: {
        role: "owner", provider: "copilot", model: "fixture", billing_basis: "copilot_subscription",
        pricing: {}, kubernetes: { enabled: true, namespace: "remedi-demo" },
        github_gitops: { enabled: true, repository: "DataScience-KnowledgeBase/remedi-ai-copilot",
          approval_action: "create_pull_request", manual_merge_required: true },
      } });
      if (path.endsWith("/provider-status") || path.endsWith("/repository-status")) {
        return route.fulfill({ json: { ready: true, message: "Mock ready" } });
      }
      if (path.endsWith("/runbooks")) return route.fulfill({ json: { items: [] } });
      if (path.endsWith("/feedback")) return route.fulfill({ json: [] });
      if (path.endsWith("/telemetry")) return route.fulfill({ json: { outcomes: {} } });
      if (path.endsWith("/approval")) {
        writes++;
        expect(request.method()).toBe("POST");
        expect(request.postDataJSON()).toEqual({
          decision: "approve", reason: "Reviewed exact repository diff and rollback.",
        });
        if (state === "api_error") {
          return route.fulfill({ status: 409, json: { detail: "Repository changed; reanalyse." } });
        }
        status = "awaiting_merge";
        return route.fulfill({ json: incident() });
      }
      if (path.endsWith("/stream")) return route.fulfill({
        contentType: "text/event-stream",
        body: events().map(event => `event: audit\ndata: ${JSON.stringify(event)}\n\n`).join("")
          + `event: status\ndata: ${JSON.stringify(incident())}\n\n`,
      });
      if (path.endsWith("/incidents")) return route.fulfill({ json: [incident()] });
      throw new Error(`Unexpected API request: ${path}`);
    });
    await page.goto("/");
    await page.getByLabel("Access token").fill("owner-" + "x".repeat(40));
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await expect(page.getByText("owner session", { exact: true })).toBeVisible();
    await page.locator(".incident").first().click();
    const approval = page.getByRole("button", { name: "Approve & create PR", exact: true });
    if (state === "legacy") {
      await expect(page.getByRole("alert")).toContainText("legacy proposal has no repository diff");
      await page.getByLabel("Approval reason").fill("Reviewed exact repository diff and rollback.");
      await expect(approval).toBeDisabled();
    } else if (state === "proposal" || state === "api_error") {
      await expect(page.getByRole("heading", { name: "Repository manifest diff for owner approval" })).toBeVisible();
      await expect(approval).toBeDisabled();
      await page.getByLabel("Approval reason").fill("Reviewed exact repository diff and rollback.");
      await approval.click();
      if (state === "api_error") {
        await expect(page.getByRole("alert")).toContainText("Repository changed; reanalyse.");
      } else {
        await expect(page.getByRole("link", { name: "Open pull request for manual review and merge" })).toBeVisible();
        await expect(page.getByRole("region", { name: "Remediation progress" }))
          .toContainText("Waiting for a human to merge");
        await expect(approval).toHaveCount(0);
      }
    } else {
      await expect(page.getByRole("link", { name: "Open pull request for manual review and merge" })).toBeVisible();
      await expect(page.getByRole("region", { name: "Remediation progress" }))
        .toContainText(state === "awaiting_ci" ? "Waiting for successful CI" : "Waiting for a human");
    }
    expect(writes).toBe(state === "proposal" || state === "api_error" ? 1 : 0);
    await expect(page.getByRole("button", { name: "Approve change", exact: true })).toHaveCount(0);
  });
}
