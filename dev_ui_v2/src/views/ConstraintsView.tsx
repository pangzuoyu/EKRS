/**
 * Phase 11 T11-3 — Constraints view (1:1 port of dev_ui Tab 2).
 *
 * Form: query (textarea) + strict checkbox + top_k + trace_id
 * Submit → POST /v1/constraints via useQueryConstraints
 *
 * Output sections:
 *   - Mode badge (single / multi_branch)
 *   - Primary branch (highlighted)
 *   - Conflicts (if any; warning styled)
 *   - Branches JSON tree
 *   - Trace expander (collapsible debug)
 */
import { useState } from "react";
import { useQueryConstraints } from "../api/hooks";

export function ConstraintsView(): JSX.Element {
  const [query, setQuery] = useState("高温环境温度限制");
  const [strict, setStrict] = useState(false);
  const [topK, setTopK] = useState(40);
  const [traceId, setTraceId] = useState("");
  const [showTrace, setShowTrace] = useState(false);

  const mutation = useQueryConstraints();

  return (
    <div
      data-testid="constraints-view"
      style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}
    >
      <section>
        <h3 style={{ margin: "0 0 0.5rem" }}>POST /v1/constraints</h3>
        <p style={{ margin: "0 0 1rem", color: "#7d8590", fontSize: "0.875rem" }}>
          Three-gate pipeline: recall → extract → solve.
        </p>
        <label
          style={{ display: "flex", flexDirection: "column", gap: "0.25rem", maxWidth: "640px" }}
        >
          <span style={{ fontSize: "0.75rem", color: "#7d8590" }}>Query</span>
          <textarea
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            rows={3}
            style={{ resize: "vertical" }}
          />
        </label>
        <div style={{ display: "flex", gap: "1.5rem", marginTop: "0.75rem", flexWrap: "wrap" }}>
          <label style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
            <input
              type="checkbox"
              checked={strict}
              onChange={(e) => setStrict(e.target.checked)}
              style={{ width: "auto" }}
            />
            <span style={{ fontSize: "0.875rem" }}>strict (R6)</span>
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <span style={{ fontSize: "0.75rem", color: "#7d8590" }}>top_k</span>
            <input
              type="number"
              min={1}
              max={200}
              value={topK}
              onChange={(e) => setTopK(Number(e.target.value))}
              style={{ width: "100px" }}
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <span style={{ fontSize: "0.75rem", color: "#7d8590" }}>trace_id (optional)</span>
            <input
              value={traceId}
              onChange={(e) => setTraceId(e.target.value)}
              style={{ width: "180px" }}
            />
          </label>
        </div>
        <button
          type="button"
          onClick={() =>
            mutation.mutate({
              query,
              context: {},
              strict,
              top_k: topK,
              ...(traceId ? { trace_id: traceId } : {}),
            })
          }
          disabled={mutation.isPending}
          style={{
            marginTop: "1rem",
            padding: "0.5rem 1rem",
            background: "#238636",
            color: "white",
            border: "none",
            borderRadius: "4px",
            cursor: "pointer",
          }}
        >
          {mutation.isPending ? "Running…" : "Run query"}
        </button>
        {mutation.error ? (
          <p role="alert" style={{ color: "#f85149", marginTop: "0.5rem" }}>
            {mutation.error.message}
          </p>
        ) : null}
      </section>

      {mutation.data ? (
        <section
          data-testid="constraints-result"
          style={{ display: "flex", flexDirection: "column", gap: "1rem" }}
        >
          <div>
            <span
              data-testid="mode-badge"
              style={{
                padding: "0.25rem 0.6rem",
                borderRadius: "12px",
                background: mutation.data.mode === "multi_branch" ? "#1f6feb" : "#6e7681",
                color: "white",
                fontSize: "0.75rem",
                fontWeight: 600,
              }}
            >
              {mutation.data.mode}
            </span>
            <span style={{ marginLeft: "0.75rem", fontSize: "0.875rem", color: "#7d8590" }}>
              Primary branch:{" "}
              <code style={{ color: "#e6edf3" }}>{mutation.data.primary_branch ?? "—"}</code>
            </span>
          </div>

          {mutation.data.conflicts.length > 0 ? (
            <div
              role="alert"
              style={{
                padding: "0.75rem",
                background: "#3a1e1e",
                border: "1px solid #f85149",
                borderRadius: "6px",
                color: "#ff7b72",
              }}
            >
              Conflicts detected: {mutation.data.conflicts.length}
              <pre style={{ margin: "0.5rem 0 0", fontSize: "0.875rem", color: "#e6edf3" }}>
                {JSON.stringify(mutation.data.conflicts, null, 2)}
              </pre>
            </div>
          ) : null}

          <div>
            <h4 style={{ margin: "0 0 0.5rem" }}>Branches</h4>
            <pre
              data-testid="branches-json"
              style={{
                padding: "0.75rem",
                background: "#161b22",
                border: "1px solid #30363d",
                borderRadius: "6px",
                overflow: "auto",
                fontSize: "0.875rem",
              }}
            >
              {JSON.stringify(mutation.data.branches, null, 2)}
            </pre>
          </div>

          <div>
            <button
              type="button"
              onClick={() => setShowTrace((v) => !v)}
              style={{
                padding: "0.4rem 0.8rem",
                background: "#21262d",
                color: "#e6edf3",
                border: "1px solid #30363d",
                borderRadius: "4px",
                cursor: "pointer",
                fontSize: "0.875rem",
              }}
            >
              {showTrace ? "Hide trace" : "Show trace (debug)"}
            </button>
            {showTrace ? (
              <pre
                style={{
                  marginTop: "0.5rem",
                  padding: "0.75rem",
                  background: "#161b22",
                  border: "1px solid #30363d",
                  borderRadius: "6px",
                  overflow: "auto",
                  fontSize: "0.875rem",
                }}
              >
                {JSON.stringify(mutation.data.trace, null, 2)}
              </pre>
            ) : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}
