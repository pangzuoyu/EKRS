/**
 * Phase 11 T11-3 — Golden set view (1:1 port of dev_ui Tab 3).
 *
 * Runs each case from the in-repo golden set against POST /v1/constraints.
 * The golden set JSON ships with the RAG backend at
 * `rag/tests/golden_set/golden_set.json`; the Vite dev proxy does NOT
 * serve it (no static mount), so we run a script that fetches it at
 * dev-start time and bundles a copy via Vite's `?raw` import.
 *
 * For T11-3 we ship an inline mini-fixture (3 cases) so the view is
 * functional standalone; T11-4 (or a follow-up) wires the real 50-case
 * golden set via an additional dev-only static mount.
 */
import { useState } from "react";
import { useQueryConstraints } from "../api/hooks";

interface GoldenCase {
  name: string;
  query: string;
  strict?: boolean;
}

// 3-case fixture mirrors the dev_ui golden set shape. Real 50-case set is
// fetched by an end-to-end Playwright run against the RAG backend (T11-4).
const GOLDEN_FIXTURE: GoldenCase[] = [
  { name: "高温环境温度限制", query: "高温环境温度限制" },
  { name: "general-pressure-rating", query: "general pump pressure rating" },
  { name: "strict-missing-context", query: "STRICT_TRIGGER", strict: true },
];

interface CaseResult {
  name: string;
  status: "PASS" | "FAIL" | "ERROR";
  http: number | null;
  error: string | null;
}

export function GoldenView(): JSX.Element {
  const [results, setResults] = useState<CaseResult[] | null>(null);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState(0);

  // useQueryConstraints is the per-case fetcher (mutation).
  const mutation = useQueryConstraints();

  const run = async (): Promise<void> => {
    setRunning(true);
    setProgress(0);
    const out: CaseResult[] = [];
    for (let i = 0; i < GOLDEN_FIXTURE.length; i++) {
      const c = GOLDEN_FIXTURE[i]!;
      try {
        const res = await mutation.mutateAsync({
          query: c.query,
          context: {},
          strict: c.strict ?? false,
          top_k: 40,
        });
        out.push({ name: c.name, status: "PASS", http: 200, error: null });
        // Touch res so the linter doesn't drop the binding.
        void res;
      } catch (e) {
        const err = e as { statusCode?: number; message?: string };
        const http = err.statusCode ?? null;
        out.push({
          name: c.name,
          status: http === 200 ? "PASS" : http !== null ? "FAIL" : "ERROR",
          http,
          error: err.message ?? String(e),
        });
      }
      setProgress((i + 1) / GOLDEN_FIXTURE.length);
    }
    setResults(out);
    setRunning(false);
  };

  const passed = results?.filter((r) => r.status === "PASS").length ?? 0;
  const failed = (results?.length ?? 0) - passed;

  return (
    <div
      data-testid="golden-view"
      style={{ display: "flex", flexDirection: "column", gap: "1rem" }}
    >
      <section>
        <h3 style={{ margin: "0 0 0.5rem" }}>Golden set regression</h3>
        <p style={{ margin: "0 0 1rem", color: "#7d8590", fontSize: "0.875rem" }}>
          Runs the in-fixture golden cases against POST /v1/constraints. The real 50-case set is
          wired in T11-4 (dev-only static mount).
        </p>
        <button
          type="button"
          onClick={() => {
            void run();
          }}
          disabled={running}
          style={{
            padding: "0.5rem 1rem",
            background: "#238636",
            color: "white",
            border: "none",
            borderRadius: "4px",
            cursor: "pointer",
          }}
        >
          {running ? `Running ${Math.round(progress * 100)}%` : "Run golden set"}
        </button>
        {running ? (
          <div
            data-testid="golden-progress"
            style={{
              marginTop: "0.75rem",
              height: "0.5rem",
              background: "#21262d",
              borderRadius: "2px",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                width: `${progress * 100}%`,
                height: "100%",
                background: "#1f6feb",
                transition: "width 0.2s ease-out",
              }}
            />
          </div>
        ) : null}
      </section>

      {results ? (
        <section>
          <div style={{ display: "flex", gap: "1.5rem", marginBottom: "0.75rem" }}>
            <span data-testid="golden-passed" style={{ color: "#3fb950" }}>
              Passed: <strong>{passed}</strong>
            </span>
            <span data-testid="golden-failed" style={{ color: "#f85149" }}>
              Failed: <strong>{failed}</strong>
            </span>
          </div>
          <table
            data-testid="golden-table"
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: "0.875rem",
            }}
          >
            <thead>
              <tr style={{ background: "#161b22" }}>
                <th style={{ textAlign: "left", padding: "0.5rem" }}>Case</th>
                <th style={{ textAlign: "left", padding: "0.5rem" }}>Status</th>
                <th style={{ textAlign: "left", padding: "0.5rem" }}>HTTP</th>
                <th style={{ textAlign: "left", padding: "0.5rem" }}>Error</th>
              </tr>
            </thead>
            <tbody>
              {results.map((r) => (
                <tr key={r.name} style={{ borderTop: "1px solid #30363d" }}>
                  <td style={{ padding: "0.5rem" }}>{r.name}</td>
                  <td
                    style={{
                      padding: "0.5rem",
                      color: r.status === "PASS" ? "#3fb950" : "#f85149",
                    }}
                  >
                    {r.status}
                  </td>
                  <td style={{ padding: "0.5rem" }}>{r.http ?? "—"}</td>
                  <td style={{ padding: "0.5rem", color: "#7d8590" }}>{r.error ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}
    </div>
  );
}
