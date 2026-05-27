# Compilations list — `/`

The landing page. Shows every compilation in the workspace, newest
first.

## Behavior

- Fetches `GET /api/v1/edit/compile?limit=50` on mount via TanStack
  Query (`queryKey: ["compilations", "list"]`).
- Each compilation renders as a horizontal row with: 2-digit ordinal,
  parsed game/date title, mono short-id, relative timestamp.
- Hover row → subtle highlight + "open ↗" affordance fade-in.
- Click row → `react-router-dom` `<Link>` to `/compilations/<id>`.
- Entry animation: 60ms stagger per row (Motion's framer-style API),
  respecting `prefers-reduced-motion`.

## Code path

- Page: `src/pages/CompilationsList.tsx`
- Row component: `src/components/CompilationRow.tsx`
- API call: `listCompilations(limit)` in `src/api/compilations.ts`
- Title parsing: `parseCompilationTitle` in `src/lib/title.ts` — pulls
  game + game-date + render-time out of Outplayed filenames

## What it does NOT do

- No filtering, no sorting beyond "newest first" (the API does that).
- No deletion. Compilations are append-only; remove via filesystem if
  needed.
- No "make new compilation" button. New comps are made via MCP. A
  trigger button would belong on the future asset-browser page.

## Future improvements

- Per-row thumbnail (the compilation's hero thumbnail.jpg) — would
  require fetching one extra static file per row.
- Search/filter ("by asset", "by game", "by date").
- Bulk operations (delete N, re-render N).
