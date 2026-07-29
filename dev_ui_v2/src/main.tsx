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
import { getAdminKey } from "./lib/auth";

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
const apiClient = createApiClient({
  baseUrl: "",
  getAdminKey,
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
