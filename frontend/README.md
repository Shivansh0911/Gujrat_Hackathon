# SETU console

The operator interface. React 18 + TypeScript + Vite + Tailwind, with TanStack Query
for server state, MapLibre GL for mapping and hls.js for live playback.

Designed for a police control room: dark, dense, high contrast, readable at three
metres, no decorative animation.

## Running

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api to 127.0.0.1:8090
```

The API must be running (`make api` from the project root). The dev server proxies
`/api`, `/media` and `/ws` to it, so the console runs same-origin in development —
a cross-origin dev setup hides CORS bugs until deployment.

```bash
npm run build      # production bundle into dist/
npm run gen:api    # regenerate src/lib/api-types.ts from the live OpenAPI schema
```

## Screens

| Route | Purpose |
|---|---|
| `/map` | Camera registry on a Gujarat basemap. Status colours, coordinate provenance, pin-drop editor, camera detail with live HLS. |
| `/journey` | Route reconstruction. **The screen the live test case is scored on.** |
| `/alerts` | Live watchlist matches over WebSocket, with acknowledge and disposition. |
| `/health` | Per-camera diagnostics, declared vs measured fps, §2.5 fault report. |

## Conventions worth knowing before editing

**Types are generated, never hand-written.** `src/lib/api-types.ts` comes from the
backend's OpenAPI schema via `npm run gen:api`. A hand-maintained client is how a UI
silently drifts from its API, and the drift is only discovered in a demo. If an
endpoint changes, regenerate rather than editing the file.

**The JWT lives in a module variable, not localStorage.** localStorage is readable by
any script on the origin, so one XSS becomes a session that outlives the tab. In
memory it dies with the page.

**Uncertainty is rendered, not hidden.** A camera we can only place to a district is
drawn as a circle at its confidence radius, not as a precise pin. A plate that needed
character corrections is badged as corrected, with the substitutions listed. Coverage
gaps are drawn as dashed segments. This is a deliberate product position: a system
that quantifies its own uncertainty is the only kind admissible as evidence.

**No mocked components.** A screen without a real backing endpoint does not ship.
This is a scoring rule for the competition, not a style preference.

## Screenshots

`scripts/capture_screenshots.mjs` drives a real browser against real API data and
writes `docs/screenshots/`. It fails the run if any screen renders empty — an empty
screenshot in a submission is worse than none, because it looks like the feature does
not work.

```bash
node scripts/capture_screenshots.mjs
```
