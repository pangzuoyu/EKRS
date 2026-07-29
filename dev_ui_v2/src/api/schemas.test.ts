/**
 * Phase 11 T11-2 — Zod schema RED tests.
 *
 * Each schema must:
 *   - parse a minimal valid payload
 *   - reject a missing required field with a usable error path
 *   - reject an invalid type
 *   - expose its TS type via `z.infer<typeof X>` (compile-time only)
 *
 * Schemas manually mirror Pydantic models in:
 *   shared/ekrs_shared/models.py          → IngestionNotification, IngestionStatus
 *   rag/ekrs_rag/api/routes/constraints.py → ConstraintQuery, ConstraintQueryResponse
 *   rag/ekrs_rag/api/routes/admin_embedding_cache.py → EmbeddingCacheFlushResponse
 *   rag/ekrs_rag/main.py /healthz         → HealthResponse
 */
import { describe, expect, it } from "vitest";
import {
  ConstraintQuerySchema,
  ConstraintQueryResponseSchema,
  EmbeddingCacheFlushResponseSchema,
  HealthResponseSchema,
  IngestionNotificationSchema,
  IngestionStatusSchema,
} from "./schemas";

describe("IngestionNotificationSchema", () => {
  it("parses a minimal notification", () => {
    const out = IngestionNotificationSchema.parse({
      doc_hash: "demo_doc_001",
      version: 1,
      output_path: "/shared/demo/output",
    });
    expect(out.doc_hash).toBe("demo_doc_001");
    expect(out.version).toBe(1);
    expect(out.output_path).toBe("/shared/demo/output");
    // Optional defaults — Pydantic IngestionNotification sets callback_url=""
    // and metadata=None; mirror exactly so callers don't have to set them.
    expect(out.callback_url).toBe("");
    expect(out.metadata).toBeUndefined();
  });

  it("rejects missing doc_hash", () => {
    const result = IngestionNotificationSchema.safeParse({
      version: 1,
      output_path: "/shared/demo/output",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues[0]?.path).toEqual(["doc_hash"]);
    }
  });

  it("rejects version < 1", () => {
    const result = IngestionNotificationSchema.safeParse({
      doc_hash: "x",
      version: 0,
      output_path: "/x",
    });
    expect(result.success).toBe(false);
  });

  it("accepts callback_url when provided", () => {
    const out = IngestionNotificationSchema.parse({
      doc_hash: "x",
      version: 1,
      output_path: "/x",
      callback_url: "http://parser/cb",
    });
    expect(out.callback_url).toBe("http://parser/cb");
  });
});

describe("IngestionStatusSchema", () => {
  it("parses a success status with defaults", () => {
    const out = IngestionStatusSchema.parse({ status: "success" });
    expect(out.status).toBe("success");
    expect(out.chunks_indexed).toBe(0);
    expect(out.version).toBe(0);
    expect(out.error).toBeUndefined();
  });

  it("rejects unknown status", () => {
    const result = IngestionStatusSchema.safeParse({ status: "WAT" });
    expect(result.success).toBe(false);
  });

  it("parses failed with error message", () => {
    const out = IngestionStatusSchema.parse({
      status: "failed",
      error: "boom",
      chunks_indexed: 0,
      version: 2,
    });
    expect(out.error).toBe("boom");
    expect(out.version).toBe(2);
  });
});

describe("ConstraintQuerySchema", () => {
  it("parses with defaults applied", () => {
    const out = ConstraintQuerySchema.parse({ query: "高温环境温度限制" });
    expect(out.query).toBe("高温环境温度限制");
    expect(out.context).toEqual({});
    expect(out.strict).toBe(false);
    expect(out.top_k).toBe(40);
  });

  it("rejects empty query", () => {
    const result = ConstraintQuerySchema.safeParse({ query: "" });
    expect(result.success).toBe(false);
  });

  it("rejects top_k > 200", () => {
    const result = ConstraintQuerySchema.safeParse({ query: "x", top_k: 999 });
    expect(result.success).toBe(false);
  });

  it("accepts strict + replay + trace_id + scope_path in context", () => {
    const out = ConstraintQuerySchema.parse({
      query: "x",
      strict: true,
      replay: true,
      replay_trace_id: "abc",
      trace_id: "def",
      context: { scope_path: ["高温环境"] },
      top_k: 10,
    });
    expect(out.strict).toBe(true);
    expect(out.replay).toBe(true);
    expect(out.context.scope_path).toEqual(["高温环境"]);
    expect(out.top_k).toBe(10);
  });
});

describe("ConstraintQueryResponseSchema", () => {
  it("parses multi_branch success response", () => {
    const out = ConstraintQueryResponseSchema.parse({
      branches: {
        general: { constraints: [] },
        高温环境: { constraints: [] },
      },
      primary_branch: "高温环境",
      conflicts: [],
      trace: [],
      mode: "multi_branch",
    });
    expect(out.mode).toBe("multi_branch");
    expect(out.primary_branch).toBe("高温环境");
    expect(out.deterministic_match).toBeUndefined();
  });

  it("parses single-mode response with null primary_branch", () => {
    const out = ConstraintQueryResponseSchema.parse({
      branches: { general: {} },
      primary_branch: null,
      conflicts: [],
      trace: [],
      mode: "single",
    });
    expect(out.mode).toBe("single");
    expect(out.primary_branch).toBeNull();
  });

  it("rejects invalid mode", () => {
    const result = ConstraintQueryResponseSchema.safeParse({
      branches: {},
      primary_branch: null,
      conflicts: [],
      trace: [],
      mode: "double_branch",
    });
    expect(result.success).toBe(false);
  });

  it("accepts deterministic_match for replay responses", () => {
    const out = ConstraintQueryResponseSchema.parse({
      branches: {},
      primary_branch: null,
      conflicts: [],
      trace: [],
      mode: "single",
      deterministic_match: true,
    });
    expect(out.deterministic_match).toBe(true);
  });
});

describe("EmbeddingCacheFlushResponseSchema", () => {
  it("parses a flush response", () => {
    const out = EmbeddingCacheFlushResponseSchema.parse({
      status: "ok",
      cleared: 42,
      model_version: "bge-m3-v1",
      cache_size_after: 0,
    });
    expect(out.status).toBe("ok");
    expect(out.cleared).toBe(42);
    expect(out.model_version).toBe("bge-m3-v1");
    expect(out.cache_size_after).toBe(0);
  });

  it("rejects missing cleared", () => {
    const result = EmbeddingCacheFlushResponseSchema.safeParse({
      status: "ok",
      model_version: "x",
      cache_size_after: 0,
    });
    expect(result.success).toBe(false);
  });
});

describe("HealthResponseSchema", () => {
  it("parses a minimal ok response", () => {
    const out = HealthResponseSchema.parse({ status: "ok" });
    expect(out.status).toBe("ok");
  });

  it("parses a degraded response with components", () => {
    const out = HealthResponseSchema.parse({
      status: "degraded",
      components: { qdrant: "down", redis: "ok" },
    });
    expect(out.status).toBe("degraded");
    expect(out.components?.qdrant).toBe("down");
  });
});
