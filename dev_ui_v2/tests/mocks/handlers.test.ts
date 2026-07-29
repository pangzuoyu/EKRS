/**
 * Phase 11 T11-2 — MSW handler RED tests.
 *
 * These handlers are the **wire-format contract** between the React client
 * and the FastAPI backend. They mirror the Pydantic response shapes so
 * tests catch contract drift when the backend changes.
 *
 * Handlers tested:
 *   - GET  /healthz
 *   - POST /v1/ingestion/notify
 *   - GET  /v1/ingestion/status/{doc_hash}
 *   - POST /v1/constraints  (happy + 4 error paths: 400 strict, 404 recall, 409 conflict)
 *   - POST /v1/admin/embedding-cache/flush (X-Admin-Key enforcement)
 */
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { handlers } from "./handlers";

const server = setupServer(...handlers);

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
});
afterEach(() => {
  server.resetHandlers();
});
afterAll(() => {
  server.close();
});

describe("MSW handlers — healthz", () => {
  it("GET /healthz returns ok", async () => {
    const res = await fetch("http://test.local/healthz");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ status: "ok" });
  });
});

describe("MSW handlers — ingestion", () => {
  it("POST /v1/ingestion/notify echoes success", async () => {
    const res = await fetch("http://test.local/v1/ingestion/notify", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        doc_hash: "demo_doc_001",
        version: 1,
        output_path: "/shared/demo/output",
      }),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body).toMatchObject({ status: "success", version: 1 });
  });

  it("POST /v1/ingestion/notify 422s on missing doc_hash", async () => {
    const res = await fetch("http://test.local/v1/ingestion/notify", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ version: 1, output_path: "/x" }),
    });
    expect(res.status).toBe(422);
  });

  it("GET /v1/ingestion/status/{doc_hash} returns success shape", async () => {
    const res = await fetch("http://test.local/v1/ingestion/status/demo_doc_001");
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body).toMatchObject({ status: "success", chunks_indexed: expect.any(Number) });
  });

  it("GET /v1/ingestion/status/{doc_hash} returns 404 for unknown doc", async () => {
    const res = await fetch("http://test.local/v1/ingestion/status/unknown_doc");
    expect(res.status).toBe(404);
  });
});

describe("MSW handlers — constraints", () => {
  it("POST /v1/constraints returns multi_branch for query matching fixtures", async () => {
    const res = await fetch("http://test.local/v1/constraints", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: "高温环境温度限制" }),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body).toMatchObject({ mode: "multi_branch" });
    expect(body["primary_branch"]).toBeTruthy();
  });

  it("POST /v1/constraints returns single mode when query has no branch match", async () => {
    const res = await fetch("http://test.local/v1/constraints", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: "general pump pressure rating" }),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body).toMatchObject({ mode: "single", primary_branch: "general" });
  });

  it("POST /v1/constraints returns 400 on strict=true + missing_context", async () => {
    const res = await fetch("http://test.local/v1/constraints", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: "STRICT_TRIGGER", strict: true }),
    });
    expect(res.status).toBe(400);
  });

  it("POST /v1/constraints returns 404 on insufficient_recall", async () => {
    const res = await fetch("http://test.local/v1/constraints", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: "NO_RECALL" }),
    });
    expect(res.status).toBe(404);
  });

  it("POST /v1/constraints returns 409 on conflict", async () => {
    const res = await fetch("http://test.local/v1/constraints", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: "CONFLICT_TRIGGER" }),
    });
    expect(res.status).toBe(409);
  });
});

describe("MSW handlers — admin embedding cache flush", () => {
  it("POST /v1/admin/embedding-cache/flush without X-Admin-Key returns 401/403", async () => {
    const res = await fetch("http://test.local/v1/admin/embedding-cache/flush", {
      method: "POST",
    });
    expect([401, 403]).toContain(res.status);
  });

  it("POST /v1/admin/embedding-cache/flush with X-Admin-Key returns ok", async () => {
    const res = await fetch("http://test.local/v1/admin/embedding-cache/flush", {
      method: "POST",
      headers: { "x-admin-key": "test-admin-key" },
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body).toMatchObject({ status: "ok", cleared: expect.any(Number) });
  });
});

describe("MSW handlers — request inspection", () => {
  it("calls receive() to inspect the request body for constraints", async () => {
    let observedQuery: string | null = null;
    server.use(
      http.post("http://test.local/v1/constraints", async ({ request }) => {
        const body = (await request.json()) as { query: string };
        observedQuery = body.query;
        return HttpResponse.json({
          branches: {},
          primary_branch: null,
          conflicts: [],
          trace: [],
          mode: "single",
        });
      })
    );
    await fetch("http://test.local/v1/constraints", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: "INSPECTION" }),
    });
    expect(observedQuery).toBe("INSPECTION");
  });
});