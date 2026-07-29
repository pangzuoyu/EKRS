/**
 * Phase 11 T11-3 — Playwright E2E for the sidebar nav.
 *
 * Smoke: navigate to the home page, click the Ingest nav link, verify
 * the Ingest view renders. Tests the Sidebar's NavLink wiring + the
 * BrowserRouter route table.
 */
import { test, expect } from "./setup";

test("Sidebar nav links route to the 4 views", async ({ page }) => {
  await page.goto("/");

  // Sidebar links exist for all 4 routes.
  await expect(page.getByRole("link", { name: /ingest/i }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /constraints/i }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /golden/i }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /overlays/i }).first()).toBeVisible();

  // Click Ingest → /ingest view renders.
  await page.getByRole("link", { name: /ingest/i }).first().click();
  await expect(page.getByTestId("ingest-view")).toBeVisible();
  expect(page.url()).toMatch(/\/ingest$/);
});
