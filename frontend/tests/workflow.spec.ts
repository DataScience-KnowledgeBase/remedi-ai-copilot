import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const environment = readFileSync(resolve("..", ".env"), "utf8");
function secret(name: string) {
  const value = environment.split(/\r?\n/).find(line => line.startsWith(`${name}=`))?.slice(name.length + 1);
  if (!value) throw new Error(`Missing ${name} in project .env`);
  return value;
}

test("configured provider workflow, accountable approval and audit export", async ({ page }) => {
  test.skip(process.env.REMEDI_EXPECT_SIGNED_OUT === "true", "Live inference requires sign-in.");
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Resolve with confidence." })).toBeVisible();
  await page.getByLabel("Access token").fill(secret("OPERATOR_TOKEN"));
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByText("operator session", { exact: true })).toBeVisible();
  await page.getByLabel("Incident scenario").selectOption("cache_pressure");
  await page.getByRole("button", { name: "+ Simulate incident" }).click();
  const detail = page.locator(".detail");
  await expect(detail.getByText("closed", { exact: true })).toBeVisible();
  await expect(page.getByText(
    process.env.REMEDI_EXPECTED_PROVIDER === "demo" ? "Fixture" : "Subscription",
    { exact: true },
  )).toBeVisible();
  await expect(detail.getByText("Three independent simulated probes passed; incident closed.")).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export audit JSON" }).click();
  expect((await download).suggestedFilename()).toMatch(/^remedi-audit-.+\.json$/);

  await page.getByLabel("Incident scenario").selectOption("worker_stall");
  await page.getByRole("button", { name: "+ Simulate incident" }).click();
  await expect(detail.getByRole("heading", { name: "Accountable owner approval required" })).toBeVisible();
  await expect(detail.getByRole("button", { name: "Approve change" })).toHaveCount(0);
  await page.getByLabel("Access token").fill(secret("OWNER_TOKEN"));
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByText("owner session", { exact: true })).toBeVisible();
  await page.locator(".incident").filter({ hasText: "Payments worker stalled" }).first().click();
  await page.getByLabel("Approval reason").fill("Reviewed impact, runbook and rollback checkpoint.");
  await page.getByRole("button", { name: "Approve change", exact: true }).click();
  await expect(detail.getByText("closed", { exact: true })).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: "../reports/dashboard.png", fullPage: true });
  await page.getByRole("button", { name: "03Observability" }).click();
  await expect(page.getByRole("heading", { name: "Inference & governance telemetry" })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  expect(errors).toEqual([]);
});

test("ADK dynamic diagnosis, independent review and human decisions", async ({ page }) => {
  test.skip(process.env.REMEDI_EXPECT_SIGNED_OUT === "true", "Live inference requires sign-in.");
  await page.goto("/");
  async function connect(role: "operator" | "owner") {
    if (await page.getByText(`${role} session`, { exact: true }).isVisible()) return;
    await page.getByLabel("Access token").fill(secret(`${role.toUpperCase()}_TOKEN`));
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await expect(page.getByText(`${role} session`, { exact: true })).toBeVisible();
  }
  const detail = page.locator(".detail");
  for (const scenario of ["dynamic_cache", "dynamic_capacity", "dynamic_worker"]) {
    await connect("operator");
    await expect(page.getByText("Google ADK + LLM review", { exact: true })).toBeVisible();
    await page.getByLabel("Incident scenario").selectOption(scenario);
    await page.getByRole("button", { name: "+ Simulate incident" }).click();
    if (scenario === "dynamic_cache") {
      await expect(detail.getByText("closed", { exact: true })).toBeVisible();
    } else {
      await expect(detail.getByRole("heading", { name: "Accountable owner approval required" })).toBeVisible();
      const title = await detail.locator(".detail-heading h3").textContent();
      if (!title) throw new Error("Missing incident title before owner review");
      await expect(detail.getByRole("button", { name: "Approve change", exact: true })).toHaveCount(0);
      await connect("owner");
      await page.locator(".incident").filter({ hasText: title }).first().click();
      await page.getByLabel("Approval reason").fill("Reviewed causal evidence, independent judgment and rollback.");
      const approve = scenario === "dynamic_capacity";
      await page.getByRole("button", { name: approve ? "Approve change" : "Reject", exact: true }).click();
      await expect(detail.getByText(approve ? "closed" : "rejected", { exact: true })).toBeVisible();
    }
  }
  await page.screenshot({ path: "../reports/adk-dynamic-dashboard.png", fullPage: true });
});
