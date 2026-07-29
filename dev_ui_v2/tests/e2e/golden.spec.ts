/**
 * Phase 11 T11-3 — Playwright E2E for the Golden view.
 *
 * Happy-path smoke: navigate to /golden, click "Run golden set", verify
 * the progress bar + results table render. The MSW handler returns a
 * 200/422 mix so both PASS and ERROR rows appear.
 */
import { test, expect } from "./setup";

test("Golden view runs the fixture and shows a results table", async ({ page }) => {
  await page.goto("/golden");
  await expect(page.getByTestId("golden-view")).toBeVisible();

  await page.getByRole("button", { name: /run golden/i }).click();

  // Progress bar appears while running.
  await expect(page.getByTestId("golden-progress")).toBeVisible();
  // After completion the passed/failed counters and table render.
  await expect(page.getByTestId("golden-passed")).toBeVisible();
  await expect(page.getByTestId("golden-table")).toBeVisible();
});
