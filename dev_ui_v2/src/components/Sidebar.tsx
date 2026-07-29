/**
 * Phase 11 T11-3 — sidebar navigation + admin key input.
 *
 * Layout:
 *   - 4 NavLinks for the 4 views (Ingest / Constraints / Golden / Overlays)
 *   - Admin key TextField → calls `setAdminKey` on input
 *   - Clear button → calls `clearAdminKey` (visible only when a key is set)
 *   - Health indicator: small dot showing `useHealth()` status
 *
 * The component is a presentational shell; data fetching lives in the
 * views so they can colocate their loading skeletons.
 */
import { NavLink } from "react-router-dom";
import { useHealth } from "../api/hooks";
import { clearAdminKey, setAdminKey, useAdminKey } from "../lib/auth";

const linkStyle: React.CSSProperties = {
  display: "block",
  padding: "0.5rem 0.75rem",
  borderRadius: "4px",
  textDecoration: "none",
  color: "#e6edf3",
  fontSize: "0.9rem",
};
const activeStyle: React.CSSProperties = {
  ...linkStyle,
  background: "#21262d",
  color: "#58a6ff",
};

export function Sidebar(): JSX.Element {
  const adminKey = useAdminKey();
  const health = useHealth();

  return (
    <aside
      data-testid="sidebar"
      style={{
        width: "220px",
        minHeight: "100vh",
        padding: "1.25rem 1rem",
        background: "#0d1117",
        borderRight: "1px solid #30363d",
        boxSizing: "border-box",
      }}
    >
      <h2 style={{ margin: "0 0 1rem", fontSize: "1rem", color: "#7d8590" }}>EKRS Dev UI v2</h2>

      <nav style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
        <NavLink to="/ingest" style={({ isActive }) => (isActive ? activeStyle : linkStyle)}>
          📥 Ingest
        </NavLink>
        <NavLink to="/constraints" style={({ isActive }) => (isActive ? activeStyle : linkStyle)}>
          🔍 Constraints
        </NavLink>
        <NavLink to="/golden" style={({ isActive }) => (isActive ? activeStyle : linkStyle)}>
          📊 Golden
        </NavLink>
        <NavLink to="/overlays" style={({ isActive }) => (isActive ? activeStyle : linkStyle)}>
          🧩 Overlays
        </NavLink>
      </nav>

      <div
        style={{
          marginTop: "2rem",
          paddingTop: "1rem",
          borderTop: "1px solid #30363d",
        }}
      >
        <div
          style={{ display: "flex", alignItems: "center", gap: "0.4rem", marginBottom: "0.5rem" }}
        >
          <span
            data-testid="health-dot"
            style={{
              width: "0.5rem",
              height: "0.5rem",
              borderRadius: "50%",
              background: health.data?.status === "ok" ? "#3fb950" : "#f85149",
            }}
          />
          <span style={{ fontSize: "0.75rem", color: "#7d8590" }}>
            {health.data ? health.data.status : "loading…"}
          </span>
        </div>

        <label
          htmlFor="admin-key"
          style={{
            display: "block",
            fontSize: "0.75rem",
            color: "#7d8590",
            marginBottom: "0.25rem",
          }}
        >
          X-Admin-Key
        </label>
        <input
          id="admin-key"
          type="password"
          aria-label="Admin key"
          defaultValue={adminKey ?? ""}
          onChange={(e) => setAdminKey(e.target.value)}
          style={{ width: "100%", boxSizing: "border-box" }}
        />
        {adminKey !== null ? (
          <button
            type="button"
            onClick={() => clearAdminKey()}
            style={{
              marginTop: "0.5rem",
              padding: "0.3rem 0.6rem",
              fontSize: "0.75rem",
              background: "#21262d",
              color: "#e6edf3",
              border: "1px solid #30363d",
              borderRadius: "4px",
              cursor: "pointer",
            }}
          >
            Clear
          </button>
        ) : null}
      </div>
    </aside>
  );
}
