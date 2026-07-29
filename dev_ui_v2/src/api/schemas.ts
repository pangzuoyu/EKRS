/**
 * Phase 11 T11-2 — Zod schemas mirroring Pydantic models.
 *
 * Manual mirror, NOT codegen (parent plan Q#7). Pydantic models in
 * `shared/ekrs_shared/models.py` and `rag/ekrs_rag/api/routes/*` are the
 * authoritative source; these schemas serve as the React-side validation
 * boundary + `z.infer` TS types.
 *
 * Convention:
 *   - REQUEST schemas use `.default(...)` for fields the Pydantic side has a
 *     default for, so the form builder never sends `undefined`. Required
 *     fields the backend rejects-as-422 if missing stay strict.
 *   - RESPONSE schemas use `.optional()` only for fields the backend may
 *     legitimately omit (e.g. `error` on a success status, `version` on a
 *     failure that never started). Required fields stay strict.
 */
import { z } from "zod";

// --- POST /v1/ingestion/notify (REQUEST) -----------------------------------

export const IngestionNotificationSchema = z.object({
  doc_hash: z.string().min(1),
  version: z.number().int().min(1),
  output_path: z.string().min(1),
  // Pydantic: callback_url: str = "" → UI default to empty string.
  callback_url: z.string().default(""),
  // Pydantic: metadata: Optional[dict] = None → UI sends only if set.
  metadata: z.record(z.unknown()).optional(),
});

// --- GET /v1/ingestion/status/{doc_hash} (RESPONSE) -----------------------

export const IngestionStatusSchema = z.object({
  status: z.enum(["processing", "success", "failed"]),
  // Pydantic: chunks_indexed: int = 0 — backend always emits; we mirror
  // the default so the UI can read `.chunks_indexed` without `?.` guards.
  chunks_indexed: z.number().int().min(0).default(0),
  version: z.number().int().min(0).default(0),
  error: z.string().optional(),
});

// --- POST /v1/constraints (REQUEST) ----------------------------------------

export const ConstraintQuerySchema = z.object({
  query: z.string().min(1),
  // Pydantic: context: dict = {} → always send an object.
  context: z.record(z.unknown()).default({}),
  // Pydantic: strict: bool = False → default false; UI only sends true
  // when the operator toggles R6 mode.
  strict: z.boolean().default(false),
  // Pydantic: replay: bool = False → optional in schema (advanced).
  replay: z.boolean().optional(),
  replay_trace_id: z.string().optional(),
  trace_id: z.string().optional(),
  // Pydantic: top_k: int = 40 → match the bounded range dev_ui uses.
  top_k: z.number().int().min(1).max(200).default(40),
});

// --- POST /v1/constraints (RESPONSE) --------------------------------------

export const ConstraintQueryResponseSchema = z.object({
  branches: z.record(z.unknown()),
  primary_branch: z.string().nullable(),
  conflicts: z.array(z.record(z.unknown())),
  trace: z.array(z.record(z.unknown())),
  mode: z.enum(["single", "multi_branch"]),
  // Pydantic: deterministic_match: Optional[bool] = None → only set on
  // replay responses.
  deterministic_match: z.boolean().optional(),
});

// --- POST /v1/admin/embedding-cache/flush (RESPONSE) ----------------------

export const EmbeddingCacheFlushResponseSchema = z.object({
  status: z.literal("ok"),
  cleared: z.number().int().min(0),
  model_version: z.string(),
  cache_size_after: z.number().int().min(0),
});

// --- GET /healthz (RESPONSE) ----------------------------------------------

export const HealthResponseSchema = z.object({
  status: z.string(),
  components: z.record(z.string()).optional(),
});

// --- Inferred TS types -----------------------------------------------------

export type IngestionNotification = z.infer<typeof IngestionNotificationSchema>;
export type IngestionStatus = z.infer<typeof IngestionStatusSchema>;
export type ConstraintQuery = z.infer<typeof ConstraintQuerySchema>;
export type ConstraintQueryResponse = z.infer<typeof ConstraintQueryResponseSchema>;
export type EmbeddingCacheFlushResponse = z.infer<typeof EmbeddingCacheFlushResponseSchema>;
export type HealthResponse = z.infer<typeof HealthResponseSchema>;
