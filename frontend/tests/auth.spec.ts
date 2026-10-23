import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

test("signed-out Copilot shows a clear gate and never admits a remediation job", async ({ page }) => {
  test.skip(process.env.REMEDI_EXPECT_SIGNED_OUT !== "true", "Explicit signed-out validation only.");
  const environment = readFileSync(resolve("..", ".env"), "utf8");
  const token = environment.split(/\r?\n/).find(line => line.startsWith("OPERATOR_TOKEN="))
    ?.slice("OPERATOR_TOKEN=".length);
  if (!token) throw new Error("Missing application operator token");
  await page.goto("/");
  await page.getByLabel("Access token").fill(token);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByText("operator session", { exact: true })).toBeVisible();
  await expect(page.getByText("Subscription", { exact: true })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("BLOCKED: Copilot sign-in required.");
  await page.getByRole("button", { name: "+ Simulate incident" }).click();
  await expect(page.getByRole("alert")).toContainText("503");
  await expect(page.getByRole("alert")).toContainText("Copilot sign-in required");
  await expect(page.locator(".incident")).toHaveCount(0);
  await page.getByRole("button", { name: "Dismiss", exact: true }).click();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: "../reports/copilot-auth-required.png", fullPage: true });
});
