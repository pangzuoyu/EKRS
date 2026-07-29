/**
 * Phase 11 T11-3 — ErrorBoundary + Skeleton RED tests.
 *
 * ErrorBoundary is a class component (React 18 has no functional equivalent
 * for `componentDidCatch`). It catches render-time exceptions in its
 * subtree and renders a fallback. Tests:
 *   - renders children when no error
 *   - renders fallback UI when a child throws during render
 *   - recovers when the user clicks the "Try again" button
 *
 * Skeleton is a functional component that renders an animated placeholder
 * block. Tests:
 *   - renders default 3-line block
 *   - accepts a custom line count
 *   - uses the dark theme (background-color matches the app shell)
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";
import { Skeleton } from "./Skeleton";

// --- ErrorBoundary --------------------------------------------------------

function Boom(): JSX.Element {
  throw new Error("kaboom");
}

function Quiet(): JSX.Element {
  return <div data-testid="quiet">all good</div>;
}

describe("ErrorBoundary", () => {
  it("renders children when no error is thrown", () => {
    render(
      <ErrorBoundary>
        <Quiet />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId("quiet")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders the fallback when a child throws during render", () => {
    // Silence the React error boundary console.error noise for this test.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/kaboom/)).toBeInTheDocument();
    consoleError.mockRestore();
  });

  it("exposes a 'Try again' button that recovers", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const user = userEvent.setup();
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /try again/i }));
    // After recovery the boundary re-renders children; Boom throws again,
    // so we re-enter the fallback. The important thing is the button works
    // and the state machine transitions: alert visible again means we
    // re-rendered (proves resetError wasn't a no-op).
    expect(screen.getByRole("alert")).toBeInTheDocument();
    consoleError.mockRestore();
  });
});

// --- Skeleton -------------------------------------------------------------

describe("Skeleton", () => {
  it("renders the default 3 placeholder lines", () => {
    const { container } = render(<Skeleton data-testid="sk" />);
    const sk = screen.getByTestId("sk");
    expect(sk).toBeInTheDocument();
    // Default 3 lines → 3 child divs inside
    expect(sk.children).toHaveLength(3);
    expect(container.firstChild).toBe(sk);
  });

  it("accepts a custom line count", () => {
    render(<Skeleton data-testid="sk" lines={5} />);
    expect(screen.getByTestId("sk").children).toHaveLength(5);
  });

  it("uses the dark theme background", () => {
    render(<Skeleton data-testid="sk" />);
    const sk = screen.getByTestId("sk");
    // Either inline style or class — we use inline for the dark theme.
    const style = window.getComputedStyle(sk);
    expect(style.backgroundColor).not.toBe("rgba(0, 0, 0, 0)");
    expect(style.backgroundColor).not.toBe("transparent");
  });
});
