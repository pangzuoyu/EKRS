#!/usr/bin/env node
/**
 * Phase 11 T11-1 — bundle size CI gate.
 *
 * Hard cap: 500 KB gzipped (parent scope decision Q#1).
 *
 * Walks `dist/assets/*.js` produced by `vite build`, computes each asset's
 * gzipped size (Node `zlib.gzipSync`), and fails (exit 1) if the largest
 * single chunk OR the sum of all chunks exceeds the cap. Helps catch
 * accidental heavyweight imports before they ship (chart libs, markdown
 * editors, etc.).
 *
 * Usage:
 *   npm run build
 *   npm run check:bundle
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

const CAP_BYTES = 500 * 1024; // 500 KB gzipped, parent plan Q#1
const DIST = "dist/assets";

let totalGzip = 0;
let largestName = "";
let largestBytes = 0;

try {
  const files = readdirSync(DIST).filter((f) => f.endsWith(".js"));
  if (files.length === 0) {
    console.error(`[check:bundle] no .js assets in ${DIST} — did you run \`npm run build\`?`);
    process.exit(1);
  }
  for (const file of files) {
    const path = join(DIST, file);
    const raw = readFileSync(path);
    const gz = gzipSync(raw).length;
    totalGzip += gz;
    if (gz > largestBytes) {
      largestBytes = gz;
      largestName = file;
    }
    console.log(`  ${file.padEnd(36)} ${(gz / 1024).toFixed(1).padStart(7)} KB gz`);
  }
} catch (err) {
  console.error(`[check:bundle] could not read ${DIST}: ${err.message}`);
  process.exit(1);
}

console.log("");
console.log(`  total: ${(totalGzip / 1024).toFixed(1)} KB gz   cap: ${(CAP_BYTES / 1024).toFixed(0)} KB gz`);
console.log(`  largest chunk: ${largestName} (${(largestBytes / 1024).toFixed(1)} KB gz)`);

if (totalGzip > CAP_BYTES) {
  console.error(`[check:bundle] FAIL — total exceeds cap. Drop a heavy dep or split chunks.`);
  process.exit(1);
}
console.log("[check:bundle] PASS");
