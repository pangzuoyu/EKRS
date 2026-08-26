/**
 * Phase 11 T11-2 — typed fetch client RED tests.
 *
 * `createApiClient({ baseUrl, getAdminKey?, getParserToken? })` returns:
 *   { notifyIngest, getIngestionStatus, queryConstraints,
 *     flushEmbeddingCache, getHealth }
 *
 * Each method:
 *   - uses Zod schema to parse the response,
 *   - throws `ApiError(statusCode, body)` on 4xx/5xx,
 *   - attaches X-Admin-Key when `getAdminKey` returns a value AND the path
 *     is `/v1/admin/*` (NOT for `/v1/constraints`, `/v1/ingestion/*`),
 *   - attaches X-Parser-Token when `getParserToken` returns a value AND
 *     the path is `/v1/constraints`, `/v1/ingestion/*`, or `/v1/blocks/*`
 *     (NOT for `/v1/admin/*`).
 *
 * TanStack Query hooks live in `src/api/hooks.ts` (separate file) so the
 * pure client stays testable without React. This file tests the client only;
 * hook tests are co-located with the hook file.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, createApiClient } from "./client";

const BASE = "http://api.test";

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}

function jsonError(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("createApiClient — happy paths", () => {
  it("getHealth parses a 200 with status field", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({ status: "ok" }));
    const client = createApiClient({ baseUrl: BASE });
    const out = await client.getHealth();
    expect(out.status).toBe("ok");
  });

  it("notifyIngest sends POST with JSON body + parses IngestionStatus-shaped response", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ status: "success", chunks_indexed: 17, version: 2 }));
    const client = createApiClient({ baseUrl: BASE });
    const out = await client.notifyIngest({
      doc_hash: "demo_doc_001",
      version: 1,
      output_path: "/shared/demo/output",
    });
    expect(out.status).toBe("success");
    expect(out.chunks_indexed).toBe(17);
    expect(fetchSpy).toHaveBeenCalledWith(
      `${BASE}/v1/ingestion/notify`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          doc_hash: "demo_doc_001",
          version: 1,
          output_path: "/shared/demo/output",
          callback_url: "",
        }),
      }),
    );
  });

  it("getIngestionStatus uses path param", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ status: "success", chunks_indexed: 99, version: 3 }));
    const client = createApiClient({ baseUrl: BASE });
    const out = await client.getIngestionStatus("doc_abc");
    expect(out.chunks_indexed).toBe(99);
    expect(fetchSpy.mock.calls[0]?.[0]).toBe(`${BASE}/v1/ingestion/status/doc_abc`);
  });

  it("queryConstraints parses multi_branch response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        branches: { general: {}, 高温环境: {} },
        primary_branch: "高温环境",
        conflicts: [],
        trace: [],
        mode: "multi_branch",
      }),
    );
    const client = createApiClient({ baseUrl: BASE });
    const out = await client.queryConstraints({ query: "高温环境温度限制" });
    expect(out.mode).toBe("multi_branch");
    expect(out.primary_branch).toBe("高温环境");
  });

  it("flushEmbeddingCache parses admin response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        status: "ok",
        cleared: 42,
        model_version: "bge-m3-v1",
        cache_size_after: 0,
      }),
    );
    const client = createApiClient({ baseUrl: BASE });
    const out = await client.flushEmbeddingCache();
    expect(out.cleared).toBe(42);
    expect(out.model_version).toBe("bge-m3-v1");
  });
});

describe("createApiClient — error handling", () => {
  it("throws ApiError with statusCode + parsed body on 4xx", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonError(404, { detail: "Insufficient recall" }),
    );
    const client = createApiClient({ baseUrl: BASE });
    await expect(client.queryConstraints({ query: "x" })).rejects.toMatchObject({
      statusCode: 404,
      body: { detail: "Insufficient recall" },
    });
  });

  it("throws ApiError on 5xx", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonError(503, { detail: "retriever not initialized" }),
    );
    const client = createApiClient({ baseUrl: BASE });
    await expect(client.getHealth()).rejects.toBeInstanceOf(ApiError);
  });

  it("throws on network failure", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(new Error("ECONNREFUSED"));
    const client = createApiClient({ baseUrl: BASE });
    await expect(client.getHealth()).rejects.toThrow("ECONNREFUSED");
  });

  it("throws when response body is not valid JSON", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response("not json", { status: 200 }));
    const client = createApiClient({ baseUrl: BASE });
    await expect(client.getHealth()).rejects.toThrow();
  });

  it("throws when response shape fails Zod parse", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({ wrong: "shape" }));
    const client = createApiClient({ baseUrl: BASE });
    await expect(client.getHealth()).rejects.toThrow();
  });
});

describe("createApiClient — X-Admin-Key header", () => {
  it("attaches X-Admin-Key on /v1/admin/* when getAdminKey returns a value", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ status: "ok", cleared: 0, model_version: "x", cache_size_after: 0 }),
      );
    const client = createApiClient({ baseUrl: BASE, getAdminKey: () => "secret" });
    await client.flushEmbeddingCache();
    expect(fetchSpy.mock.calls[0]?.[1]?.headers).toMatchObject({
      "X-Admin-Key": "secret",
    });
  });

  it("does NOT attach X-Admin-Key on /v1/constraints even when getAdminKey returns a value", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        branches: {},
        primary_branch: null,
        conflicts: [],
        trace: [],
        mode: "single",
      }),
    );
    const client = createApiClient({ baseUrl: BASE, getAdminKey: () => "secret" });
    await client.queryConstraints({ query: "x" });
    const headers = fetchSpy.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers["X-Admin-Key"]).toBeUndefined();
  });

  it("omits X-Admin-Key when getAdminKey returns null", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ status: "ok", cleared: 0, model_version: "x", cache_size_after: 0 }),
      );
    const client = createApiClient({ baseUrl: BASE, getAdminKey: () => null });
    await client.flushEmbeddingCache();
    const headers = fetchSpy.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers["X-Admin-Key"]).toBeUndefined();
  });
});

describe("ApiError", () => {
  it("is a real Error subclass", () => {
    const e = new ApiError(400, { detail: "bad" });
    expect(e).toBeInstanceOf(Error);
    expect(e.name).toBe("ApiError");
    expect(e.statusCode).toBe(400);
    expect(e.body).toEqual({ detail: "bad" });
  });
});

/**
 * Phase 13c post-closure patch — X-Parser-Token header attachment.
 *
 * The RAG backend gates `/v1/constraints`, `/v1/ingestion/notify`,
 * `/v1/ingestion/status/{hash}`, and `/v1/blocks/{id}` behind the
 * `X-Parser-Token` header (see `rag/ekrs_rag/security.py:require_parser_token`).
 * Without it, those endpoints return 403. Operators paste the local
 * PARSER_TOKEN into ConstraintsView Settings; the value lives in localStorage
 * (`ekrs.parser_token`) and the typed client attaches it only to parser-gated
 * paths.
 *
 * Negative tests:
 *   - getParserToken not supplied → header never attached
 *   - getParserToken returns null → header not attached
 *   - path is /v1/admin/* → parser token NOT attached (admin uses X-Admin-Key)
 *   - path is /healthz → parser token NOT attached (unauthenticated)
 *
 * Note: getBlock was added on the backend in Phase 10 Td.2 (HTTP route
 * `GET /v1/blocks/{block_id}`) but a typed client method was not added.
 * The path-prefix match is `/v1/blocks/` so a future getBlock will work
 * without code changes here.
 */
describe("createApiClient — X-Parser-Token header", () => {
  it("attaches X-Parser-Token on /v1/constraints when getParserToken returns a value", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        branches: {},
        primary_branch: null,
        conflicts: [],
        trace: [],
        mode: "single",
      }),
    );
    const client = createApiClient({
      baseUrl: BASE,
      getParserToken: () => "dev-local-token-32chars-aaaaaaaaaa",
    });
    await client.queryConstraints({ query: "x" });
    expect(fetchSpy.mock.calls[0]?.[1]?.headers).toMatchObject({
      "X-Parser-Token": "dev-local-token-32chars-aaaaaaaaaa",
    });
  });

  it("attaches X-Parser-Token on /v1/ingestion/notify when getParserToken returns a value", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ status: "success", chunks_indexed: 1, version: 1 }));
    const client = createApiClient({
      baseUrl: BASE,
      getParserToken: () => "dev-local-token-32chars-aaaaaaaaaa",
    });
    await client.notifyIngest({
      doc_hash: "demo_doc_001",
      version: 1,
      output_path: "/shared/demo/output",
    });
    expect(fetchSpy.mock.calls[0]?.[1]?.headers).toMatchObject({
      "X-Parser-Token": "dev-local-token-32chars-aaaaaaaaaa",
    });
  });

  it("attaches X-Parser-Token on /v1/ingestion/status/{hash} when getParserToken returns a value", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ status: "success", chunks_indexed: 1, version: 1 }));
    const client = createApiClient({
      baseUrl: BASE,
      getParserToken: () => "dev-local-token-32chars-aaaaaaaaaa",
    });
    await client.getIngestionStatus("doc_abc");
    expect(fetchSpy.mock.calls[0]?.[1]?.headers).toMatchObject({
      "X-Parser-Token": "dev-local-token-32chars-aaaaaaaaaa",
    });
  });

  it("does NOT attach X-Parser-Token when getParserToken returns null", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        branches: {},
        primary_branch: null,
        conflicts: [],
        trace: [],
        mode: "single",
      }),
    );
    const client = createApiClient({ baseUrl: BASE, getParserToken: () => null });
    await client.queryConstraints({ query: "x" });
    const headers = fetchSpy.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers["X-Parser-Token"]).toBeUndefined();
  });

  it("does NOT attach X-Parser-Token when getParserToken is not supplied", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        branches: {},
        primary_branch: null,
        conflicts: [],
        trace: [],
        mode: "single",
      }),
    );
    const client = createApiClient({ baseUrl: BASE });
    await client.queryConstraints({ query: "x" });
    const headers = fetchSpy.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers["X-Parser-Token"]).toBeUndefined();
  });

  it("does NOT attach X-Parser-Token on /v1/admin/* even when getParserToken returns a value", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ status: "ok", cleared: 0, model_version: "x", cache_size_after: 0 }),
      );
    const client = createApiClient({
      baseUrl: BASE,
      getAdminKey: () => "admin-secret",
      getParserToken: () => "parser-token",
    });
    await client.flushEmbeddingCache();
    const headers = fetchSpy.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers["X-Parser-Token"]).toBeUndefined();
    expect(headers["X-Admin-Key"]).toBe("admin-secret");
  });

  it("does NOT attach X-Parser-Token on /healthz", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ status: "ok" }));
    const client = createApiClient({
      baseUrl: BASE,
      getParserToken: () => "parser-token",
    });
    await client.getHealth();
    const headers = fetchSpy.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers["X-Parser-Token"]).toBeUndefined();
  });

  it("attaches X-Admin-Key + X-Parser-Token on independent paths when both keys are supplied", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ status: "ok", cleared: 0, model_version: "x", cache_size_after: 0 }),
      );
    const client = createApiClient({
      baseUrl: BASE,
      getAdminKey: () => "admin-key",
      getParserToken: () => "parser-token",
    });
    await client.flushEmbeddingCache();
    const headers = fetchSpy.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers["X-Admin-Key"]).toBe("admin-key");
    expect(headers["X-Parser-Token"]).toBeUndefined();

    fetchSpy.mockResolvedValueOnce(
      jsonResponse({
        branches: {},
        primary_branch: null,
        conflicts: [],
        trace: [],
        mode: "single",
      }),
    );
    await client.queryConstraints({ query: "x" });
    const headers2 = fetchSpy.mock.calls[1]?.[1]?.headers as Record<string, string>;
    expect(headers2["X-Parser-Token"]).toBe("parser-token");
    expect(headers2["X-Admin-Key"]).toBeUndefined();
  });
});
