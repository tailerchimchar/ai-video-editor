# Architecture

The web app is a deliberately thin React layer over the `api/` HTTP
surface. This doc covers the *why*, the *stack choices*, and the
*data flow* — so you can extend it without re-deriving everything.

## The three-repo system

```
ai-video-editor/
├── api/    FastAPI + ffmpeg — the brain. Owns spec.json, runs the
│           render pipeline, manages the SQLite store. Editing logic
│           lives here.
├── mcp/    FastMCP — adapter that exposes API endpoints as MCP tools
│           for conversational drive from Claude.
└── web/    React + TypeScript — visual workspace. Calls the same
            HTTP endpoints MCP does. NO editing logic here.
```

Each repo is independent. `web/` requires `api/` running on
`localhost:8000` (default) — when shipped to production, that's a
deployment concern, not a runtime coupling beyond HTTP.

## The load-bearing rule: frontend is presentation only

This single rule shapes every decision below:

> **`web/` does not reimplement editing logic.** Every mutation is an
> existing `POST /api/v1/edit/compile/...` endpoint. The frontend
> reads, displays, and triggers — it never computes spec changes
> locally.

Why this matters:
- **One source of truth.** `spec.json` on disk. MCP and the webapp
  see the same state and the same history.
- **Cheap to add a feature.** New mutator in `api/` → MCP tool + web
  button BOTH consume the same endpoint. No duplicated logic to
  maintain in two places.
- **CLAUDE.md policy.** Every editing endpoint must have BOTH an MCP
  tool AND (when relevant) a web button. Logic stays in `api/`.

What this gives up:
- No offline edits, no optimistic local mutation chains. Every change
  is a server round-trip. The trade-off is acceptable for local-first
  single-user use; the API is on `localhost`.

## Tech stack

| Choice | Version | Why |
|---|---|---|
| Bun | latest | Faster than npm/yarn for installs; user's preference |
| Vite | ^5 | Standard React build, sub-100ms HMR, plays well with Bun |
| React | ^19 | Latest stable |
| TypeScript | ^5.5 strict | `noUncheckedIndexedAccess` catches the worst surface bugs |
| Tailwind CSS | ^3.4 | shadcn-compatible without v4 friction |
| React Router | ^7 | Two routes; mature; great TS |
| TanStack Query | ^5 | Server-state cache, retry, mutation invalidation |
| Motion (`motion/react`) | ^11 | Entry transitions per the [Anthropic frontend-design skill](https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md) recommendation |
| Radix UI Slider | latest | Accessible dual-handle range primitive |
| Prettier | ^3 | Single formatter, no ESLint/Biome for this size |

**Notable non-choices:**
- No state-management library (Zustand/Redux). TanStack Query owns
  server state; component-local `useState` covers the rest.
- No shadcn CLI. We hand-write the ~6 UI primitives we need
  (`Button`, `Badge`, `PageHeader`, `PageShell`, etc.).
- No CORS middleware on the backend. Vite proxy handles dev.
- No fancy Webpack/Rollup config beyond what Vite ships.

## Data flow

```
       USER                                 SERVER
        │                                     │
   ┌────▼────┐    GET /api/v1/edit/compile  ┌─▼──┐
   │ React   │────────────────────────────►│ API│
   │ + TS    │◄────── JSON ─────────────────│    │
   └────┬────┘                              └─┬──┘
        │                                     │
        │      POST /.../extend (mutation)    │
        ├────────────────────────────────────►│ ◄── spec.json
        │◄──── EditSummary (clips refreshed) ─┤      (source of truth)
        │                                     │
        │      GET /workspace/.../          ┌─▼──┐
        ├────────────────────────────────►│Static │
        │◄──── compilation.mp4 (Range) ────┤Files │
                                            └────┘
```

TanStack Query handles:
- Caching `GET` responses (30s `staleTime`)
- Invalidating after mutations (every mutation calls
  `queryClient.invalidateQueries({ queryKey: ['compilation', id] })`)
- Retry on transient failures (default `retry: 1`)
- Refetch on focus disabled (would spam the API every tab switch)

## Backend dependencies (additions made for `web/`)

The `api/` repo gained two web-specific additions:

1. **`StaticFiles` mount at `/workspace`** —
   `app.mount("/workspace", StaticFiles(directory=settings.workspace_dir, check_dir=False))`.
   Serves `compilation.mp4`, per-clip thumbnails, and intro mp4s.
   Range requests work out of the box for `<video>` seeking.
   Source recordings (`OUTPLAYED_MEDIA_DIR`) are NOT mounted — only
   outputs the system itself produces.

2. **Per-clip thumbnails** (`thumbnail.py`) — extracts a midpoint
   frame from each clip's source on every render. Stored at
   `<compilation>/_thumbnails/<clip_id>.jpg`. The filmstrip uses
   these. `POST /edit/compile/:id/thumbnails/regenerate` backfills
   for compilations made before this feature.

Beyond these two, **no editing logic moved client-side**.

## Design language

Per the Anthropic frontend-design skill: commit to a bold aesthetic
direction, avoid AI-generic templates.

**Direction: refined cinematic minimalism.** Single dominant surface
(near-black `#0a0a0b`), single sharp accent (electric blue `#3b82f6`).
Hairline borders, no drop shadows on cards, 1px inset top-highlight
for elevated surfaces. Distinctive typography (Instrument Serif +
Geist + JetBrains Mono — never Inter/Arial/Roboto).

Full breakdown in [features/design-system.md](features/design-system.md).

## Folder structure

```
src/
├── api/                 typed fetch wrappers, one file per backend router
│   ├── client.ts        single typed `request<T>` generic
│   ├── compilations.ts  list / getOne / getClips / getHistory
│   ├── edits.ts         extend / revert / setClipCaptions / add / remove / tiktokify
│   └── jobs.ts          (scaffold for Phase 2+ async ops)
│
├── components/          shared UI
│   ├── VideoPlayer.tsx        controlled <video> with cache-bust + onTimeUpdate
│   ├── ClipFilmstrip.tsx      horizontal strip + auto-scroll-to-playing
│   ├── ClipMetaPanel.tsx      header + extend slider (LEFT col)
│   ├── ClipActionsPanel.tsx   captions + future stubs (RIGHT col)
│   ├── CaptionEditor.tsx      add / edit / remove / tiktokify
│   ├── ExtendSlider.tsx       reel-timeline dual-handle slider
│   ├── HistoryRail.tsx        journal + revert
│   ├── CompilationRow.tsx     list-page row
│   └── ui/                    primitives (Button, Badge, PageHeader, PageShell)
│
├── hooks/
│   ├── useCompilation.ts      composite hook — every read + mutation
│   └── useJob.ts              poll scaffold (Phase 2+)
│
├── lib/
│   ├── cn.ts            clsx + tailwind-merge
│   ├── time.ts          mmss(), parseClipRef(), relativeTime() — match backend semantics
│   ├── title.ts         parse Outplayed filename → human title
│   └── env.ts           VITE_API_URL with default
│
├── pages/
│   ├── CompilationsList.tsx   /
│   └── CompilationViewer.tsx  /compilations/:id
│
├── styles/
│   ├── tokens.css       CSS variables — single source of truth for theme
│   ├── fonts.css        @fontsource imports
│   └── globals.css      Tailwind directives + grain texture + base resets
│
└── types/
    ├── clip.ts          Clip, CaptionSegment, CaptionStyle, Effect
    └── compilation.ts   CompilationSummary, HistoryEntry, EditSummary
```

## Extension points designed in

Cheap insurance for future phases — these cost nothing today:

- **`Clip` type carries optional `speaker?`, `style?`, `effects: Effect[]`**
  (discriminated union by `kind`). Phase 4 multi-speaker captions and
  Phase 5 screen-shake plug in without breaking the renderer.
- **`VideoPlayer` is controlled** — `onTimeUpdate` already exposes
  playback time. Phase 2's full-reel scrubber binds here.
- **`useJob(jobId)` hook scaffolded** — Phase 1 doesn't mutate via
  background jobs, but the polling abstraction is ready for the
  Phase 2+ POST endpoints that return `job_id`.
- **`api/client.ts` typed `request<T>`** — adding a new endpoint =
  one new function with a generic, no plumbing changes.

## Verification

```
bun run check     # tsc --noEmit
bun run format    # Prettier
```

End-to-end smoke test:
1. Start `api/` (`uv run dev`)
2. Start `web/` (`bun run dev`)
3. Open `http://localhost:5173/compilations/<existing-id>` — should
   load video + filmstrip + caption editor
4. Drag the extend slider and release — clip should re-render, new
   timestamp appears, history rail gains an `Extended clip …` entry
5. Click `+ add caption`, type text, save — new segment burns in,
   history shows `Added caption to clip …`
