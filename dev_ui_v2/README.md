# EKRS Dev UI v2

> Phase 11 T11-1 scaffold. Replaces the Streamlit `dev_ui/` debug tool with a
> production-grade React SPA. 1:1 port of 4 existing tabs (Ingest /
> Constraints / Golden Set / Overlays) with production polish (typed API
> client, X-Admin-Key localStorage, ErrorBoundary, skeleton loaders).

**Stack**: React 18.3 + TypeScript 5 strict + Vite 5 + TanStack Query 5 +
React Router 6 + Zod 3 + Playwright + ESLint + Prettier.

**Phase plan**: [`docs/superpowers/plans/2026-07-29-phase11-react-ui.md`](../../docs/superpowers/plans/2026-07-29-phase11-react-ui.md).

## Quick commands

```bash
npm install           # install (uses .nvmrc; Node 20.20+)
npm run dev           # Vite dev server + /v1/* proxy to localhost:8000
npm run build         # type-check + production build
npm run preview       # serve dist/ at http://127.0.0.1:4173
npm run typecheck     # tsc -b
npm run lint          # ESLint, max-warnings 0
npm run test          # Vitest (unit + MSW contract)
npm run test:cov      # Vitest + v8 coverage
npm run test:watch    # Vitest watch mode
npm run format        # Prettier write
npm run format:check  # Prettier check (CI gate)
npm run check:bundle  # CI gate: dist/assets/*.js gzipped ≤ 500 KB
```

## E2E tests (Playwright)

Headless Chromium runs the 6 specs under `tests/e2e/` against the Vite dev
server. MSW's service worker (`public/mockServiceWorker.js`) intercepts
`/v1/*` and `/healthz` so no RAG backend is needed — the same wildcard-host
handlers from `tests/mocks/handlers.ts` that the Vitest suite uses.

| From repo root          | Effect                                                     |
| ----------------------- | ---------------------------------------------------------- |
| `make test-e2e`         | First-time setup (`npm ci` + Playwright Chromium install) + run |
| `make test-e2e-ci`      | CI-mode: assumes Playwright cache warm, runs specs only     |
| `make test-e2e-ready`   | Pre-flight: node ≥ 20.20.0, Playwright + Chromium + MSW worker present |

Or step inside `dev_ui_v2/`:

```bash
npm run test:e2e          # playwright test
npm run test:e2e:ui       # headed UI for local debugging
```

### Notes

- `playwright.config.ts` runs the dev server (`npm run dev`) as `webServer`,
  NOT `npm run preview`. The MSW worker only registers when
  `import.meta.env.DEV` is true, so a production build would bypass the
  mock backend entirely. The build is also faster and avoids the need to
  rebuild for every iteration.
- `fullyParallel: false` + `workers: 1` keep MSW handler state predictable
  across specs (see `playwright.config.ts` comments); removing those in
  favor of parallel runs is a deliberate trade-off, not an oversight.
- E2E does NOT yet run in CI on PRs — that wiring is Phase 12-B. Until
  then, run `make test-e2e` locally before merging UI-affecting changes.

## Layout

```
dev_ui_v2/
├── index.html             Vite entry HTML
├── package.json           deps + npm scripts
├── tsconfig.json          project-references root
├── tsconfig.app.json      src/ strict TS config
├── tsconfig.node.json     vite.config + scripts config
├── vite.config.ts         Vite + React plugin + /v1/* dev proxy
├── vitest.config.ts       Vitest + jsdom + MSW node + v8 coverage
├── scripts/
│   └── check-bundle-size.mjs   CI gate: 500 KB gzipped hard cap
├── src/
│   ├── main.tsx           React root + QueryClientProvider + ApiClientProvider + BrowserRouter
│   ├── App.tsx            App shell skeleton (T11-3 fills this in)
│   ├── vite-env.d.ts      Vite ambient types
│   ├── test-setup.ts      Vitest global setup (jest-dom matchers)
│   ├── api/
│   │   ├── schemas.ts     Zod schemas mirroring Pydantic models + z.infer types
│   │   ├── client.ts      Pure typed fetch wrapper + ApiError
│   │   ├── context.tsx    React context for the ApiClient
│   │   └── hooks.ts       TanStack Query hooks (useHealth, useNotifyIngest, …)
│   └── lib/
│       └── auth.ts        X-Admin-Key localStorage helpers + useAdminKey hook
└── tests/
    └── mocks/
        └── handlers.ts    MSW handlers (the wire-format contract spec)
```

T11-3 adds `src/views/{Ingest,Constraints,Golden,Overlays}.tsx` + Playwright
`tests/e2e/`. T11-4 Dockerizes (node:20-alpine → nginx:1.27-alpine). T11-5
deprecates `dev_ui/`.

## Bundle budget (CI gate)

Hard cap: **500 KB gzipped** for `dist/assets/*.js` total. Per parent scope
decision Q#1. The check script fails CI if exceeded; a chart library or
markdown editor inflate blows the cap within a single PR.

## Coexistence with `dev_ui/`

Streamlit `dev_ui/` is preserved as a 1-quarter fallback. Both can be
installed side-by-side. Once ops confirms the React UI is the primary
tool, T11-5 retires the Streamlit path entirely.
