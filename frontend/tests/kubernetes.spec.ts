import { test, expect } from "@playwright/test";

for (const status of ["awaiting_approval", "verifying", "rejected"]) {
  test(`Kubernetes progress and approval boundary: ${status} (mock API, no cluster writes)`, async ({ page }) => {
    const incident = {
      id: "kubernetes-ui-fixture", title: "Kubernetes: OOMKilled", service: "remedi-oom",
      scenario: "k8s_oom", severity: "high", status, created_at: new Date().toISOString(),
      provider: "copilot", model: "test-model", error: null,
      plan: {
        action: "increase_pod_memory", confidence: 0.95, summary: "Observed OOMKilled at 32Mi.",
        evidence_ids: ["EV-METRICS-01", "RB-K8S-MEMORY-01"],
      },
    };
    const stages = ["triage", "observe", "judge", "kubernetes_plan", "decision"];
    if (status === "verifying") stages.push("approval", "execute", "verify", "progress");
    if (status === "rejected") stages.push("approval");
    const events = stages.map((stage, index) => ({
      seq: index + 1, at: new Date().toISOString(), stage, actor: "agent",
      message: stage === "progress" ? "Live Kubernetes recovery checks: 2/3 consecutive passes." : stage,
      payload: stage === "kubernetes_plan" ? {
        change: { namespace: "remedi-demo", deployment: "remedi-oom", before: "32Mi", after: "128Mi" },
      } : stage === "progress" ? { phase: "verification", consecutive_passes: 2 } : {},
    }));
    let incidentPosts = 0;
    let approvalPosts = 0;
    await page.route("**/api/**", async route => {
      const path = new URL(route.request().url()).pathname;
      const role = route.request().headers().authorization?.includes("owner") ? "owner" : "operator";
      if (path.endsWith("/config")) return route.fulfill({ json: {
        role, provider: "copilot", model: "test-model", billing_basis: "copilot_subscription",
        confidence_threshold: 0.85, max_agent_steps: 4, prompt_version: "test", runbook_version: "test",
        pricing: {}, kubernetes: { enabled: true, namespace: "remedi-demo", owner_approval_required: true },
      } });
      if (path.endsWith("/provider-status")) return route.fulfill({ json: { ready: true, message: "Mock provider" } });
      if (path.endsWith("/runbooks")) return route.fulfill({ json: { items: [] } });
      if (path.endsWith("/feedback")) return route.fulfill({ json: [] });
      if (path.endsWith("/telemetry")) return route.fulfill({ json: {
        incident_count: 1, llm_calls: 4, outcomes: {}, calls_with_missing_usage: 0,
      } });
      if (path.endsWith("/approval")) {
        approvalPosts++;
        return route.fulfill({ json: incident });
      }
      if (path.endsWith("/stream")) {
        const frames = events.map(event => `event: audit\ndata: ${JSON.stringify(event)}\n\n`).join("");
        return route.fulfill({
          contentType: "text/event-stream",
          body: frames + `event: status\ndata: ${JSON.stringify(incident)}\n\n`,
        });
      }
      if (path.endsWith("/incidents") && route.request().method() === "POST") {
        incidentPosts++;
        expect(route.request().postDataJSON()).toEqual({ scenario: "k8s_oom" });
        return route.fulfill({ status: 202, json: { incident, created: true } });
      }
      if (path.endsWith("/incidents")) return route.fulfill({ json: [incident] });
      throw new Error(`Unexpected mocked API path: ${path}`);
    });
    await page.goto("/");
    await expect(page.getByText("EXECUTION SCOPE NOT LOADED", { exact: false })).toBeVisible();
    await expect(page.getByText("No production systems are modified.", { exact: true })).toHaveCount(0);
    await page.getByLabel("Access token").fill("operator-" + "x".repeat(40));
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await expect(page.getByText("operator session", { exact: true })).toBeVisible();
    await page.getByLabel("Incident scenario").selectOption("k8s_oom");
    await page.getByRole("button", { name: "Diagnose live pods", exact: true }).click();
    const progress = page.getByRole("region", { name: "Remediation progress" });
    await expect(progress).toContainText("LIVE KUBERNETES");
    await expect(page.getByRole("heading", { name: "Exact Kubernetes change for approval" })).toBeVisible();
    if (status === "awaiting_approval") {
      await expect(progress).toContainText("No remediation patch has been applied.");
      await expect(page.getByRole("button", { name: "Approve change", exact: true })).toHaveCount(0);
      await page.getByLabel("Access token").fill("owner-" + "y".repeat(40));
      await page.getByRole("button", { name: "Connect", exact: true }).click();
      await expect(page.getByText("owner session", { exact: true })).toBeVisible();
      await page.locator(".incident").first().click();
      await expect(page.getByRole("button", { name: "Approve change", exact: true })).toBeDisabled();
      await page.getByLabel("Approval reason").fill("Reviewed OOM evidence, memory bound and rollback.");
      await expect(page.getByRole("button", { name: "Approve change", exact: true })).toBeEnabled();
    } else if (status === "verifying") {
      await expect(progress).toContainText("Measured consecutive recovery checks: 2 / 3");
      await expect(progress.locator('[aria-current="step"]')).toContainText("Verify recovery");
    } else {
      await expect(progress).toContainText("no remediation was applied");
      await expect(progress.getByText("Apply change", { exact: false })).toHaveClass("pending");
    }
    expect(incidentPosts).toBe(1);
    expect(approvalPosts).toBe(0);
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  });
}
