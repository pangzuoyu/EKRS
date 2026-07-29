/**
 * Phase 11 T11-3 — Playwright E2E for the Constraints view.
 *
 * Happy-path smoke: navigate to /constraints, run a query, verify the
 * mode badge + branches JSON tree appear. The MSW handler returns a
 * multi-branch response for CJK queries (the default `高温环境温度限制`).
 */
import { test, expect } from "./setup";

test("Constraints view runs a query and shows the multi-branch result", async ({ page }) => {
  await page.goto("/constraints");
  await expect(page.getByTestId("constraints-view")).toBeVisible();

  await page.getByRole("button", { name: /run query/i }).click();

  await expect(page.getByTestId("constraints-result")).toBeVisible();
  await expect(page.getByTestId("mode-badge")).toContainText(/multi_branch|single/);
  await expect(page.getByTestId("branches-json")).toBeVisible();
});
