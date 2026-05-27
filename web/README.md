# ai-video-editor-web

The visual workspace for the AI Video Editor — a thin React frontend
over the `api/` HTTP surface.

```
┌─────────────────────────────────────────────────┐
│ Compilation viewer at /compilations/:id         │
│                                                 │
│ ┌─────────────────────┬───────────────────────┐ │
│ │ video player        │  caption editor       │ │
│ │ filmstrip           │  future actions       │ │
│ │ selected-clip meta  │  history (compact)    │ │
│ └─────────────────────┴───────────────────────┘ │
└─────────────────────────────────────────────────┘
```

## Why this repo exists

Editing a compilation through MCP / Claude conversation works for
one-shot moves ("zoom clip 3"). It breaks down for the bulk-review
workflow that real video editors do:

- Looking at every clip, finding the bad ones, retrimming each.
- Fixing transcription mistakes word-by-word.
- Adding captions at exact timestamps.
- Watching the reel + seeking + spotting where something feels off.

The frontend exists to do those things visually — see what you're
editing, drag-to-trim with a slider, click to fix a caption, jump the
video to a specific clip with one click.

Architectural rule: **the frontend doesn't reimplement editing
logic**. Every mutation routes through an existing API endpoint. Same
contract MCP uses. The frontend is presentation + UX. See
[docs/architecture.md](docs/architecture.md) for the full design.

## Quick start

The backend (`api/` sibling repo) must be running first:

```sh
# In another terminal:
cd ../api
uv sync
uv run dev          # starts FastAPI at :8000
```

Then this repo:

```sh
cd web
bun install
bun run dev         # Vite dev server at :5173
```

Open `http://localhost:5173`. The dev server proxies `/api` and
`/workspace` to the backend transparently — same-origin in dev, no
CORS to think about.

## Scripts

| Command | Does |
|---|---|
| `bun run dev` | Vite dev server with HMR |
| `bun run build` | `tsc -b && vite build` — type-check + production bundle |
| `bun run preview` | Serve the production build locally |
| `bun run check` | `tsc --noEmit` — TypeScript-only check |
| `bun run format` | Prettier write |
| `bun run format:check` | Prettier check (CI-friendly) |

## Documentation

- **[docs/architecture.md](docs/architecture.md)** — tech stack, data
  flow, design language, the load-bearing "frontend is presentation
  only" principle.
- **[docs/roadmap.md](docs/roadmap.md)** — what's built, what's next,
  open questions.
- **[docs/features/](docs/features/)** — per-feature deep-dives:
  - [compilations-list.md](docs/features/compilations-list.md) —
    landing page
  - [viewer-layout.md](docs/features/viewer-layout.md) — the
    7/5 split + which component owns what
  - [filmstrip.md](docs/features/filmstrip.md) — horizontal
    clip strip with thumbnails, selected vs playing states,
    auto-scroll, vertical-wheel-pan
  - [caption-editor.md](docs/features/caption-editor.md) —
    add / edit / remove / tiktokify, the unified style model
  - [reel-timeline-slider.md](docs/features/reel-timeline-slider.md)
    — drag-to-extend/shrink on a shared reel axis
  - [history-rail.md](docs/features/history-rail.md) —
    descriptive journal + per-version revert
  - [design-system.md](docs/features/design-system.md) — fonts,
    colour tokens, motion, the "refined cinematic minimalism" brief

## Layout

```
web/
├── docs/                     ← architecture, roadmap, feature pages
├── public/                   (none yet — Vite serves from index.html)
├── src/
│   ├── api/                  typed fetch wrappers (one file per router)
│   ├── components/           shared UI (VideoPlayer, ClipFilmstrip, ...)
│   ├── hooks/                useCompilation, useJob (forward-compat)
│   ├── lib/                  cn, time, title, env — pure utilities
│   ├── pages/                CompilationsList, CompilationViewer
│   ├── styles/               tokens.css, fonts.css, globals.css
│   └── types/                hand-mirrored Pydantic shapes
├── index.html
├── package.json
├── tailwind.config.ts
├── tsconfig.json
└── vite.config.ts
```
