/**
 * Phase 11 T11-2 — Vitest global setup.
 *
 * Extends `expect` with @testing-library/jest-dom matchers (toBeInTheDocument,
 * toHaveTextContent, etc.) for component tests in T11-3. T11-2 hooks tests
 * use these too.
 */
import "@testing-library/jest-dom/vitest";
