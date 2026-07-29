/**
 * Phase 11 T11-2 — typed fetch client.
 *
 * Pure (no React). Returns 5 methods that wrap `fetch`, attach headers
 * (X-Admin-Key only for `/v1/admin/*`), and validate the response with a
 * Zod schema before returning.
 *
 * Vite dev proxy: in dev mode, the host (`http://127.0.0.1:5173`) proxies
 * `/v1/*` to the RAG service (see `vite.config.ts → server.proxy`). So
 * `createApiClient({ baseUrl: "" })` works in dev AND when the SPA is
 * served by the production nginx container alongside the RAG service.
 *
 * TanStack Query hooks live in `hooks.ts` (separate file). This split lets
 * the pure client be tested with `fetch` mocks without React.
 */
import type { z } from "zod";
import {
  ConstraintQueryResponseSchema,
  ConstraintQuerySchema,
  EmbeddingCacheFlushResponseSchema,
  HealthResponseSchema,
  IngestionNotificationSchema,
  IngestionStatusSchema,
} from "./schemas";

// --- Error type -----------------------------------------------------------

export class ApiError extends Error {
  readonly statusCode: number;
  readonly body: unknown;
  constructor(statusCode: number, body: unknown, message?: string) {
    super(message ?? `API error ${statusCode}`);
    this.name = "ApiError";
    this.statusCode = statusCode;
    this.body = body;
  }
}

// --- Client factory -------------------------------------------------------

export interface ApiClientOptions {
  baseUrl: string;
  getAdminKey?: () => string | null;
}

// Use `z.input` for caller-facing arguments (lets the caller omit fields
// that have defaults). Use `z.output` (== z.infer) for return types.
export interface ApiClient {
  notifyIngest(
    input: z.input<typeof IngestionNotificationSchema>,
  ): Promise<z.output<typeof IngestionStatusSchema>>;
  getIngestionStatus(docHash: string): Promise<z.output<typeof IngestionStatusSchema>>;
  queryConstraints(
    input: z.input<typeof ConstraintQuerySchema>,
  ): Promise<z.output<typeof ConstraintQueryResponseSchema>>;
  flushEmbeddingCache(): Promise<z.output<typeof EmbeddingCacheFlushResponseSchema>>;
  getHealth(): Promise<z.output<typeof HealthResponseSchema>>;
}

export function createApiClient(opts: ApiClientOptions): ApiClient {
  const { baseUrl, getAdminKey } = opts;

  async function request<S extends z.ZodTypeAny>(args: {
    path: string;
    method: "GET" | "POST";
    body?: unknown;
    schema: S;
  }): Promise<z.output<S>> {
    const url = `${baseUrl}${args.path}`;
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (getAdminKey && args.path.startsWith("/v1/admin/")) {
      const key = getAdminKey();
      if (key !== null) {
        headers["X-Admin-Key"] = key;
      }
    }
    const res = await fetch(url, {
      method: args.method,
      headers,
      body: args.body !== undefined ? JSON.stringify(args.body) : undefined,
    });

    // Parse body once; reuse for both success and error branches.
    let parsed: unknown;
    const text = await res.text();
    if (text.length > 0) {
      try {
        parsed = JSON.parse(text);
      } catch (err) {
        if (res.ok) {
          throw new Error(
            `expected JSON from ${args.path} but got ${text.slice(0, 80)}: ${(err as Error).message}`,
          );
        }
        parsed = { detail: text };
      }
    } else {
      parsed = null;
    }

    if (!res.ok) {
      throw new ApiError(res.status, parsed);
    }

    const result = args.schema.safeParse(parsed);
    if (!result.success) {
      throw new Error(
        `response from ${args.path} failed schema validation: ${result.error.message}`,
      );
    }
    return result.data as z.output<S>;
  }

  return {
    notifyIngest(input) {
      return request({
        path: "/v1/ingestion/notify",
        method: "POST",
        body: IngestionNotificationSchema.parse(input),
        schema: IngestionStatusSchema,
      });
    },
    getIngestionStatus(docHash) {
      const encoded = encodeURIComponent(docHash);
      return request({
        path: `/v1/ingestion/status/${encoded}`,
        method: "GET",
        schema: IngestionStatusSchema,
      });
    },
    queryConstraints(input) {
      return request({
        path: "/v1/constraints",
        method: "POST",
        body: ConstraintQuerySchema.parse(input),
        schema: ConstraintQueryResponseSchema,
      });
    },
    flushEmbeddingCache() {
      return request({
        path: "/v1/admin/embedding-cache/flush",
        method: "POST",
        schema: EmbeddingCacheFlushResponseSchema,
      });
    },
    getHealth() {
      return request({
        path: "/healthz",
        method: "GET",
        schema: HealthResponseSchema,
      });
    },
  };
}
