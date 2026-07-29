/**
 * Phase 11 T11-1 — App shell skeleton.
 *
 * Minimal landing page that proves the stack works end-to-end:
 * - Renders without crashing (browser smoke test).
 * - Wires dark theme (study terminal style — Tier 2 polish).
 *
 * T11-3 will replace this with React Router routes for the 4 views:
 * Ingest / Constraints / Golden / Overlays. Keep this surface area
 * intentionally tiny so the scaffold acceptance test (build + preview)
 * stays focused on stack wiring, not feature surface.
 */
export function App(): JSX.Element {
  return (
    <main
      style={{
        minHeight: "100vh",
        margin: 0,
        padding: "2rem 3rem",
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif',
        background: "#0e1116",
        color: "#e6edf3",
        colorScheme: "dark",
      }}
    >
      <header style={{ marginBottom: "2rem" }}>
        <h1 style={{ margin: 0, fontSize: "1.5rem", fontWeight: 600 }}>🛠️ EKRS Dev UI v2</h1>
        <p
          style={{
            margin: "0.5rem 0 0",
            color: "#7d8590",
            fontSize: "0.875rem",
          }}
        >
          Phase 11 scaffold — T11-1 stack + bundle budget CI gate.
        </p>
      </header>
      <section
        style={{
          padding: "1rem 1.5rem",
          background: "#161b22",
          border: "1px solid #30363d",
          borderRadius: "6px",
        }}
      >
        <p style={{ margin: 0 }}>Stack ready. T11-3 will add the 4 views.</p>
      </section>
    </main>
  );
}
