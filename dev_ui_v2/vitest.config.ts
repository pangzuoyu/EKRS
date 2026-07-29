/// <reference types="vitest" />
/**
 * Phase 11 T11-2 — Vitest config.
 *
 * Unit tests live co-located next to source as `*.test.ts` / `*.test.tsx`.
 * Playwright E2E (T11-3) lives under `tests/e2e/*.spec.ts` — excluded here so vitest
 * doesn't try to run them and Playwright doesn't try to run vitest.
 *
 * Note: `__tests__` directories are not used because vite's esbuild config bundler
 * treats `__tests__` (leading double underscore) as an undefined global identifier,
 * crashing config load. Co-located `*.test.ts` is also the Vite/Vitest idiom.
 */
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.ts", "src/**/*.test.tsx", "tests/**/*.test.ts"],
    exclude: ["node_modules", "dist", "tests/e2e/**", "playwright.config.ts"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/test-setup.ts", "src/main.tsx", "src/vite-env.d.ts"],
    },
  },
});
