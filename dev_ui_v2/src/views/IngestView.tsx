/**
 * Phase 11 T11-3 — Ingest view (1:1 port of dev_ui Tab 1).
 *
 * Two sub-forms:
 *   - Submit notification: doc_hash + version + output_path (+ optional
 *     callback_url) → POST /v1/ingestion/notify via useNotifyIngest.
 *   - Check task status: doc_hash → GET /v1/ingestion/status/{doc_hash}
 *     via useIngestionStatus (polls every 2s while processing).
 */
import { useState } from "react";
import { useIngestionStatus, useNotifyIngest } from "../api/hooks";
import { Skeleton } from "../components/Skeleton";

export function IngestView(): JSX.Element {
  const [docHash, setDocHash] = useState("demo_doc_001");
  const [version, setVersion] = useState(1);
  const [outputPath, setOutputPath] = useState("/shared/demo/output");
  const [callbackUrl, setCallbackUrl] = useState("");
  const [checkHash, setCheckHash] = useState("demo_doc_001");

  const notify = useNotifyIngest();
  // The notify response carries no doc_hash; the form's checkHash field is
  // the single source of truth for "which document do I want to poll?"
  const status = useIngestionStatus(checkHash);

  return (
    <div
      data-testid="ingest-view"
      style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}
    >
      <section>
        <h3 style={{ margin: "0 0 0.5rem" }}>Trigger parser notification</h3>
        <p style={{ margin: "0 0 1rem", color: "#7d8590", fontSize: "0.875rem" }}>
          POST /v1/ingestion/notify — the production callback the parser would send.
        </p>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: "0.75rem",
            maxWidth: "640px",
          }}
        >
          <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <span style={{ fontSize: "0.75rem", color: "#7d8590" }}>doc_hash</span>
            <input value={docHash} onChange={(e) => setDocHash(e.target.value)} />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <span style={{ fontSize: "0.75rem", color: "#7d8590" }}>version</span>
            <input
              type="number"
              min={1}
              value={version}
              onChange={(e) => setVersion(Number(e.target.value))}
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <span style={{ fontSize: "0.75rem", color: "#7d8590" }}>output_path</span>
            <input value={outputPath} onChange={(e) => setOutputPath(e.target.value)} />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <span style={{ fontSize: "0.75rem", color: "#7d8590" }}>callback_url (optional)</span>
            <input value={callbackUrl} onChange={(e) => setCallbackUrl(e.target.value)} />
          </label>
        </div>
        <button
          type="button"
          onClick={() =>
            notify.mutate({
              doc_hash: docHash,
              version,
              output_path: outputPath,
              callback_url: callbackUrl || "",
            })
          }
          disabled={notify.isPending}
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
          {notify.isPending ? "Submitting…" : "Submit notification"}
        </button>
        {notify.data ? (
          <pre
            data-testid="notify-response"
            style={{
              marginTop: "1rem",
              padding: "0.75rem",
              background: "#161b22",
              border: "1px solid #30363d",
              borderRadius: "6px",
              overflow: "auto",
              fontSize: "0.875rem",
            }}
          >
            {JSON.stringify(notify.data, null, 2)}
          </pre>
        ) : null}
        {notify.error ? (
          <p role="alert" style={{ color: "#f85149", marginTop: "0.5rem" }}>
            {notify.error.message}
          </p>
        ) : null}
      </section>

      <section>
        <h3 style={{ margin: "0 0 0.5rem" }}>Check task status</h3>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "flex-end" }}>
          <label
            style={{
              display: "flex",
              flexDirection: "column",
              gap: "0.25rem",
              flex: 1,
              maxWidth: "320px",
            }}
          >
            <span style={{ fontSize: "0.75rem", color: "#7d8590" }}>doc_hash</span>
            <input value={checkHash} onChange={(e) => setCheckHash(e.target.value)} />
          </label>
        </div>
        <div data-testid="status-result" style={{ marginTop: "1rem" }}>
          {status.isLoading ? (
            <Skeleton lines={2} />
          ) : status.data ? (
            <pre
              style={{
                padding: "0.75rem",
                background: "#161b22",
                border: "1px solid #30363d",
                borderRadius: "6px",
                overflow: "auto",
                fontSize: "0.875rem",
              }}
            >
              {JSON.stringify(status.data, null, 2)}
            </pre>
          ) : status.error ? (
            <p role="alert" style={{ color: "#f85149" }}>
              {status.error.message}
            </p>
          ) : null}
        </div>
      </section>
    </div>
  );
}
