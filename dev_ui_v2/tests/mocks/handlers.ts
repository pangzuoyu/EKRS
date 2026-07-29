/**
 * Phase 11 T11-2 — MSW handlers (mock backend).
 *
 * Per parent plan Q#6: the mocks ARE the wire-format contract specification.
 * Handlers mirror the FastAPI + Pydantic responses for the 4 endpoints
 * the React UI calls (plus /healthz). When a real Pydantic schema changes,
 * the matching handler must change too — tests fail loudly.
 *
 * MSW v2 setup:
 *   - node setup (for vitest): `import { setupServer } from 'msw/node'`
 *   - browser setup (for Playwright E2E in T11-3): `import { setupWorker }
 *     from 'msw/browser'` + `worker.start()`
 */
import { http, HttpResponse } from "msw";

// Deterministic test admin key. Matches what E2E tests will inject.
export const TEST_ADMIN_KEY = "test-admin-key";

// Static fixture map: doc_hash → ingestion status. Read-only — the notify
// handler returns `processing` directly without mutating this map so status
// queries against known fixtures stay deterministic across test ordering.
const FIXTURE_INGESTION_STATUS: Record<string, Record<string, unknown>> = {
  demo_doc_001: { status: "success", chunks_indexed: 42, version: 1 },
  processing_doc: { status: "processing", chunks_indexed: 0, version: 1 },
  failed_doc: { status: "failed", chunks_indexed: 0, version: 1, error: "boom" },
};

function isAdmin(req: Request): boolean {
  return req.headers.get("x-admin-key") === TEST_ADMIN_KEY;
}

export const handlers = [
  // --- GET /healthz --------------------------------------------------------
  http.get("http://test.local/healthz", () => {
    return HttpResponse.json({ status: "ok" });
  }),

  // --- POST /v1/ingestion/notify -------------------------------------------
  http.post("http://test.local/v1/ingestion/notify", async ({ request }) => {
    const body = (await request.json()) as {
      doc_hash?: string;
      version?: number;
      output_path?: string;
    };
    if (!body.doc_hash || typeof body.version !== "number" || !body.output_path) {
      return HttpResponse.json(
        { detail: "missing required field" },
        { status: 422 }
      );
    }
    return HttpResponse.json({
      status: "success",
      chunks_indexed: 17,
      version: body.version,
    });
  }),

  // --- GET /v1/ingestion/status/{doc_hash} --------------------------------
  http.get("http://test.local/v1/ingestion/status/:docHash", ({ params }) => {
    const docHash = params["docHash"] as string;
    const entry = FIXTURE_INGESTION_STATUS[docHash];
    if (!entry) {
      return HttpResponse.json({ detail: "not found" }, { status: 404 });
    }
    return HttpResponse.json(entry);
  }),

  // --- POST /v1/constraints ------------------------------------------------
  http.post("http://test.local/v1/constraints", async ({ request }) => {
    const body = (await request.json()) as {
      query: string;
      strict?: boolean;
    };
    const query = body.query ?? "";

    // Strict + missing_context → 400 (R6)
    if (body.strict && query === "STRICT_TRIGGER") {
      return HttpResponse.json(
        { detail: "missing_context: inferred constraint not allowed in strict mode" },
        { status: 400 }
      );
    }

    // Insufficient recall → 404
    if (query === "NO_RECALL") {
      return HttpResponse.json(
        { detail: "Insufficient recall" },
        { status: 404 }
      );
    }

    // Conflict → 409
    if (query === "CONFLICT_TRIGGER") {
      return HttpResponse.json(
        {
          detail: {
            conflicts: [
              {
                parameter: "temperature",
                reason: "upper bound exceeds lower bound",
              },
            ],
          },
        },
        { status: 409 }
      );
    }

    // Multi-branch when query contains branch keyword
    if (query.includes("高温")) {
      return HttpResponse.json({
        branches: {
          general: { constraints: [{ parameter: "temperature", unit: "C" }] },
          高温环境: { constraints: [{ parameter: "temperature", unit: "C", max: 425 }] },
        },
        primary_branch: "高温环境",
        conflicts: [],
        trace: [{ step: "retrieval", matched: 5 }],
        mode: "multi_branch",
      });
    }

    // Default single-mode response
    return HttpResponse.json({
      branches: {
        general: { constraints: [{ parameter: "pressure", unit: "MPa" }] },
      },
      primary_branch: "general",
      conflicts: [],
      trace: [{ step: "retrieval", matched: 3 }],
      mode: "single",
    });
  }),

  // --- POST /v1/admin/embedding-cache/flush --------------------------------
  http.post("http://test.local/v1/admin/embedding-cache/flush", ({ request }) => {
    if (!isAdmin(request)) {
      return HttpResponse.json(
        { detail: "admin key required" },
        { status: 401 }
      );
    }
    return HttpResponse.json({
      status: "ok",
      cleared: 7,
      model_version: "bge-m3-v1",
      cache_size_after: 0,
    });
  }),
];