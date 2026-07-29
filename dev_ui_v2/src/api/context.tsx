/**
 * Phase 11 T11-2 — React context for the API client.
 *
 * The pure client is supplied via context so views can `useContext(ApiClientContext)`
 * and tests can inject a mock client via `ApiClientProvider value={mockClient}`.
 *
 * Default value is a production-ready client pointing at the same origin
 * (relies on the Vite dev proxy in dev mode; nginx reverse proxy in prod).
 */
import { createContext, type ReactNode } from "react";
import { createApiClient, type ApiClient } from "./client";

export const ApiClientContext = createContext<ApiClient>(createApiClient({ baseUrl: "" }));

export function ApiClientProvider(props: { client: ApiClient; children: ReactNode }): JSX.Element {
  return (
    <ApiClientContext.Provider value={props.client}>{props.children}</ApiClientContext.Provider>
  );
}
