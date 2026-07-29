/**
 * Phase 11 T11-3 — Playwright E2E for the Overlays view.
 *
 * Smoke: navigate to /overlays, verify the placeholder note renders.
 * The view is read-only + admin-keyed; no fetches happen.
 */
import { test, expect } from "./setup";

test("Overlays view shows the placeholder note", async ({ page }) => {
  await page.goto("/overlays");
  await expect(page.getByTestId("overlays-view")).toBeVisible();
  await expect(page.getByText(/placeholder|provision overrides/i)).toBeVisible();
});

test("Overlays view shows the admin-key prompt when no key is set", async ({ page }) => {
  await page.goto("/overlays");
  await expect(page.getByText(/set the x-admin-key/i)).toBeVisible();
});
