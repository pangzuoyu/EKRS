/**
 * Phase 11 T11-3 — MSW browser worker setup (Playwright only).
 *
 * Lives under `tests/` so the Vite production build never bundles it.
 * Playwright's `addInitScript` injects `startWorker()` into the page before
 * the React app mounts, so the SW intercepts the first fetches.
 *
 * The wildcard-host handlers in `handlers.ts` (T11-2) match the same way
 * in the browser worker as they do in the node setupServer.
 */
import { setupWorker } from "msw/browser";
import { handlers } from "./handlers";

export const worker = setupWorker(...handlers);

export async function startWorker(): Promise<void> {
  await worker.start({
    onUnhandledRequest: "bypass",
    serviceWorker: { url: "/mockServiceWorker.js" },
  });
}
