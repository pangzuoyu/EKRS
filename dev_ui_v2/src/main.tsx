/**
 * Phase 11 T11-1 — React root entry point.
 *
 * - React 18 createRoot (NOT `ReactDOM.render`, deprecated since 18).
 * - QueryClientProvider for TanStack Query (data fetching in T11-2+).
 * - ApiClientProvider exposes the typed fetch client (T11-2) via context.
 * - BrowserRouter for React Router 6 (routing in T11-3).
 * - StrictMode in dev surfaces double-render bugs early.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { App } from "./App";
import { ApiClientProvider } from "./api/context";
import { createApiClient } from "./api/client";
import { getAdminKey, getParserToken } from "./lib/auth";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The RAG backend serves real-time data — never reuse a stale response
      // for a freshly submitted query.
      staleTime: 0,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

// Default client points at the same origin — Vite dev proxy (vite.config.ts)
// forwards /v1/* to the RAG service in dev; nginx reverse proxy in production.
// `getParserToken` (Phase 13c post-closure patch) attaches X-Parser-Token to
// /v1/constraints, /v1/ingestion/*, /v1/blocks/* paths. Without it, those
// endpoints return 403 — the operator pastes the local PARSER_TOKEN into the
// ConstraintsView Settings input.
const apiClient = createApiClient({
  baseUrl: "",
  getAdminKey,
  getParserToken,
});

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("Root element #root missing in index.html");
}

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ApiClientProvider client={apiClient}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </ApiClientProvider>
    </QueryClientProvider>
  </StrictMode>,
);

// Optional MSW worker for dev / E2E. The dev-only guard means production
// builds statically replace `import.meta.env.DEV` with `false`, eliminating
// the dynamic import. The `mocks/browser` chunk remains on disk but is never
// fetched — it is excluded from the bundle-size CI gate by name pattern.
if (import.meta.env.DEV) {
  void import("../tests/mocks/browser").then(({ startWorker }) => startWorker());
}
