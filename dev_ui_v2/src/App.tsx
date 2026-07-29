/**
 * Phase 11 T11-3 — App router shell.
 *
 * - Sidebar (left rail) with nav + admin key input + health dot
 * - React Router 6 routes for the 4 views
 * - ErrorBoundary wraps each route so a render crash is contained
 *   to its view (white-screen-free)
 *
 * `useRouteError` is not yet wired because we use top-level ErrorBoundary
 * per route — simpler than per-element boundaries + matches the dev_ui
 * "one tab, one boundary" mental model.
 */
import { Link, Route, Routes } from "react-router-dom";
import { Sidebar } from "./components/Sidebar";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { IngestView } from "./views/IngestView";
import { ConstraintsView } from "./views/ConstraintsView";
import { GoldenView } from "./views/GoldenView";
import { OverlaysView } from "./views/OverlaysView";

export function App(): JSX.Element {
  return (
    <div style={{ display: "flex", minHeight: "100vh" }}>
      <Sidebar />
      <main
        style={{
          flex: 1,
          padding: "2rem 2.5rem",
          maxWidth: "1000px",
          color: "#e6edf3",
        }}
      >
        <Routes>
          <Route
            path="/"
            element={
              <ErrorBoundary>
                <HomeRedirect />
              </ErrorBoundary>
            }
          />
          <Route
            path="/ingest"
            element={
              <ErrorBoundary>
                <IngestView />
              </ErrorBoundary>
            }
          />
          <Route
            path="/constraints"
            element={
              <ErrorBoundary>
                <ConstraintsView />
              </ErrorBoundary>
            }
          />
          <Route
            path="/golden"
            element={
              <ErrorBoundary>
                <GoldenView />
              </ErrorBoundary>
            }
          />
          <Route
            path="/overlays"
            element={
              <ErrorBoundary>
                <OverlaysView />
              </ErrorBoundary>
            }
          />
          <Route
            path="*"
            element={
              <ErrorBoundary>
                <NotFound />
              </ErrorBoundary>
            }
          />
        </Routes>
      </main>
    </div>
  );
}

function HomeRedirect(): JSX.Element {
  return (
    <div data-testid="home-view">
      <h2 style={{ margin: "0 0 1rem" }}>Welcome</h2>
      <p style={{ color: "#7d8590" }}>Pick a view from the sidebar to start.</p>
      <ul style={{ marginTop: "1rem", listStyle: "none", padding: 0 }}>
        <li style={{ margin: "0.4rem 0" }}>
          <Link to="/ingest">📥 Ingest — trigger notifications + check status</Link>
        </li>
        <li style={{ margin: "0.4rem 0" }}>
          <Link to="/constraints">🔍 Constraints — run three-gate queries</Link>
        </li>
        <li style={{ margin: "0.4rem 0" }}>
          <Link to="/golden">📊 Golden — regression against the fixture</Link>
        </li>
        <li style={{ margin: "0.4rem 0" }}>
          <Link to="/overlays">🧩 Overlays — provision overrides (placeholder)</Link>
        </li>
      </ul>
    </div>
  );
}

function NotFound(): JSX.Element {
  return (
    <div data-testid="not-found-view">
      <h2 style={{ margin: "0 0 0.5rem" }}>404 — Not found</h2>
      <p style={{ color: "#7d8590" }}>
        That route doesn&apos;t exist. Use the sidebar to pick a view.
      </p>
    </div>
  );
}
