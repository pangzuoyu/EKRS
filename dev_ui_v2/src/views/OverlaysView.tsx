/**
 * Phase 11 T11-3 — Overlays view (placeholder for provision overrides).
 *
 * 1:1 port of dev_ui Tab 4 — read-only viewer for DocumentRepo
 * `provision_overrides`. The full admin endpoint ships in a later
 * phase; for now we show a banner explaining the placeholder + an
 * "Admin key" indicator (calls useAdminKey).
 */
import { useAdminKey } from "../lib/auth";

export function OverlaysView(): JSX.Element {
  const adminKey = useAdminKey();
  return (
    <div
      data-testid="overlays-view"
      style={{ display: "flex", flexDirection: "column", gap: "1rem" }}
    >
      <section>
        <h3 style={{ margin: "0 0 0.5rem" }}>Provision overrides</h3>
        <p style={{ margin: "0 0 1rem", color: "#7d8590", fontSize: "0.875rem" }}>
          Read-only view of the DocumentRepo `provision_overrides` table.
        </p>
      </section>

      {adminKey === null ? (
        <div
          style={{
            padding: "0.75rem",
            background: "#161b22",
            border: "1px solid #30363d",
            borderRadius: "6px",
            color: "#7d8590",
          }}
        >
          Set the X-Admin-Key in the sidebar to load overrides.
        </div>
      ) : (
        <div
          role="alert"
          style={{
            padding: "0.75rem",
            background: "#3a2e1e",
            border: "1px solid #d29922",
            borderRadius: "6px",
            color: "#d29922",
          }}
        >
          Placeholder — there is no /v1/admin/overrides endpoint yet. Use `sqlite3` or DocumentRepo
          directly until a later phase ships the admin endpoint.
        </div>
      )}
    </div>
  );
}
