import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      // dev: proxy /v1/* to the local RAG backend so the SPA can call it
      // cross-origin without CORS pre-flight while developing.
      "/v1": {
        target: process.env.VITE_DEV_RAG_BASE ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    // chunking strategy: keep vendor libs in their own chunk so the
    // application shell ships fast. Rollup's default heuristic is good
    // for this size of SPA.
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query"],
        },
      },
    },
  },
});
