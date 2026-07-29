/**
 * Phase 11 T11-1 — React root entry point.
 *
 * - React 18 createRoot (NOT `ReactDOM.render`, deprecated since 18).
 * - QueryClientProvider for TanStack Query (data fetching later in T11-2).
 * - BrowserRouter for React Router 6 (routing later in T11-3).
 * - StrictMode in dev surfaces double-render bugs early.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { App } from "./App";

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

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("Root element #root missing in index.html");
}

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
