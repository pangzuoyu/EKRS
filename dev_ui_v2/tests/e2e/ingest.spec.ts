/**
 * Phase 11 T11-3 — Playwright E2E for the Ingest view.
 *
 * Happy-path smoke: navigate to /ingest, submit a notification, verify
 * the response block renders. MSW worker (started in setup.ts) returns
 * a 200 fixture so the app receives the wire-format response.
 */
import { test, expect } from "./setup";

test("Ingest view submits notification and shows the response", async ({ page }) => {
  await page.goto("/ingest");
  await expect(page.getByTestId("ingest-view")).toBeVisible();

  // Submit the form (default values are fine; the MSW handler echoes them).
  await page.getByRole("button", { name: /submit notification/i }).click();

  // The response block appears when the mutation succeeds.
  await expect(page.getByTestId("notify-response")).toBeVisible();
  await expect(page.getByTestId("notify-response")).toContainText("chunks_indexed");
});
