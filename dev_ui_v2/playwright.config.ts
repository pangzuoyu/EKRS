/**
 * Phase 11 T11-3 — Playwright config.
 *
 * E2E specs live under `tests/e2e/*.spec.ts`. The MSW browser worker
 * (see `tests/e2e/setup.ts`) intercepts `/v1/*` and `/healthz` from the
 * browser; the built `dist/` is served by `npm run preview` at :4173.
 *
 * TDD note: each spec is a happy-path smoke for one view. The MSW
 * handlers from `tests/mocks/handlers.ts` (T11-2) double as the wire-format
 * contract for the browser tests.
 */
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false, // serial keeps MSW worker state predictable
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    headless: true,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], channel: undefined },
    },
  ],
  webServer: {
    // Dev server (NOT preview) so the MSW worker guard
    // `import.meta.env.DEV` is true and the mock backend starts.
    command: "npm run dev",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
