/**
 * Phase 11 T11-3 — Sidebar + views RED tests.
 *
 * Sidebar tests:
 *   - renders 4 nav links (Ingest / Constraints / Golden / Overlays)
 *   - admin key input calls setAdminKey on blur
 *   - admin key input shows stored value on mount
 *   - "Clear" button removes the stored admin key
 *
 * View tests (minimal — full flow is covered by Playwright E2E):
 *   - each view renders without crashing (smoke test that imports work)
 *   - IngestView shows a "Submit notification" button
 *   - ConstraintsView shows a "Run query" button
 *   - GoldenView shows a "Run golden set" button
 *   - OverlaysView shows a placeholder note
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Sidebar } from "./Sidebar";
import { IngestView } from "../views/IngestView";
import { ConstraintsView } from "../views/ConstraintsView";
import { GoldenView } from "../views/GoldenView";
import { OverlaysView } from "../views/OverlaysView";
import { ApiClientProvider } from "../api/context";
import { createApiClient } from "../api/client";
import { ADMIN_KEY_STORAGE_KEY, getAdminKey, setAdminKey } from "../lib/auth";

function renderWithProviders(node: JSX.Element): void {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const client = createApiClient({ baseUrl: "http://test.local" });
  render(
    <QueryClientProvider client={qc}>
      <ApiClientProvider client={client}>
        <MemoryRouter>{node}</MemoryRouter>
      </ApiClientProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  localStorage.clear();
});

// --- Sidebar --------------------------------------------------------------

describe("Sidebar", () => {
  it("renders 4 navigation links", () => {
    renderWithProviders(<Sidebar />);
    expect(screen.getByRole("link", { name: /ingest/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /constraints/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /golden/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /overlays/i })).toBeInTheDocument();
  });

  it("renders an admin key input", () => {
    renderWithProviders(<Sidebar />);
    expect(screen.getByLabelText(/admin key/i)).toBeInTheDocument();
  });

  it("shows the stored admin key on mount", () => {
    setAdminKey("preloaded-secret");
    renderWithProviders(<Sidebar />);
    const input = screen.getByLabelText(/admin key/i) as HTMLInputElement;
    expect(input.value).toBe("preloaded-secret");
  });

  it("stores the admin key on input change", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Sidebar />);
    const input = screen.getByLabelText(/admin key/i);
    await user.type(input, "my-key");
    expect(getAdminKey()).toBe("my-key");
  });

  it("Clear button removes the admin key", async () => {
    const user = userEvent.setup();
    setAdminKey("to-be-cleared");
    renderWithProviders(<Sidebar />);
    const clearBtn = screen.getByRole("button", { name: /clear/i });
    await user.click(clearBtn);
    expect(getAdminKey()).toBeNull();
    expect(localStorage.getItem(ADMIN_KEY_STORAGE_KEY)).toBeNull();
  });

  it("hides Clear button when no admin key is set", () => {
    renderWithProviders(<Sidebar />);
    expect(screen.queryByRole("button", { name: /clear/i })).not.toBeInTheDocument();
  });
});

// --- Views (smoke tests; full flow covered by Playwright E2E) ------------

describe("IngestView (smoke)", () => {
  it("renders without crashing and shows the submit button", () => {
    renderWithProviders(<IngestView />);
    expect(screen.getByRole("button", { name: /submit notification/i })).toBeInTheDocument();
  });
});

describe("ConstraintsView (smoke)", () => {
  it("renders without crashing and shows the run-query button", () => {
    renderWithProviders(<ConstraintsView />);
    expect(screen.getByRole("button", { name: /run query/i })).toBeInTheDocument();
  });
});

describe("GoldenView (smoke)", () => {
  it("renders without crashing and shows the run-golden button", () => {
    renderWithProviders(<GoldenView />);
    expect(screen.getByRole("button", { name: /run golden/i })).toBeInTheDocument();
  });
});

describe("OverlaysView (smoke)", () => {
  it("renders the placeholder note", () => {
    renderWithProviders(<OverlaysView />);
    expect(screen.getByText(/placeholder|provision overrides/i)).toBeInTheDocument();
  });
});
