/**
 * Phase 11 T11-3 — React error boundary.
 *
 * React 18 has no functional equivalent for `componentDidCatch`, so this
 * is a class component (the only place we use one in the project). It
 * wraps each top-level view route so a render-time crash in one view
 * doesn't white-screen the entire app.
 *
 * Behaviour:
 *   - When a child throws during render → render a fallback `<div role="alert">`
 *     with the error message + a "Try again" button that clears the error
 *     state and re-mounts the subtree.
 *   - When no error → render children unchanged.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Console-only logging; in production we'd plumb this to the audit
    // log via a global handler, but T11-3 ships the minimal UI contract.
    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  reset = (): void => {
    this.setState({ error: null });
  };

  override render(): ReactNode {
    if (this.state.error) {
      return (
        <div
          role="alert"
          style={{
            padding: "1.5rem",
            margin: "1.5rem",
            background: "#3a1e1e",
            border: "1px solid #f85149",
            borderRadius: "6px",
            color: "#ff7b72",
            fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
          }}
        >
          <h2 style={{ margin: "0 0 0.5rem", fontSize: "1.1rem" }}>Something went wrong</h2>
          <p style={{ margin: "0 0 1rem", color: "#e6edf3" }}>{this.state.error.message}</p>
          <button
            type="button"
            onClick={this.reset}
            style={{
              padding: "0.5rem 1rem",
              background: "#21262d",
              border: "1px solid #30363d",
              borderRadius: "4px",
              color: "#e6edf3",
              cursor: "pointer",
              fontFamily: "inherit",
            }}
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
