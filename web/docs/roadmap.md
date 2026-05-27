# Roadmap — UI

Living doc. What's built, what's planned, open questions.

## Built

### Phase 1 — viewer foundation
- `/` compilations list with thumbnails + metadata
- `/compilations/:id` viewer with video player + 7/5 layout
- Vite proxy for `/api` and `/workspace` — same-origin in dev
- Backend additions: StaticFiles mount, per-clip thumbnails

### Phase 1.5 — filmstrip + editing surface
- Horizontal scrollable clip filmstrip with per-clip thumbnails
- Click tile → selects clip + seeks video to its reel start
- Vertical-wheel-to-horizontal-pan
- Auto-scroll filmstrip to follow video playback (pulse dot indicator)
- Selected vs playing visual states (selected = bold accent border,
  playing = bg tint + pulse)

### Phase 1.6 — clip editor
- `ClipMetaPanel` (left col) — header + reel-timeline extend slider
- `ClipActionsPanel` (right col) — caption editor + future-action stubs
- Reel-timeline slider showing each clip's position on the shared axis

### Phase 2 — caption editor
- Inline text editing per segment
- Add caption at arbitrary timestamp + style preset
- Remove caption by index
- "TikTok-ify clip" — explode + restyle in one click
- **Unified caption model** — `caption_mode` flag deprecated, per-segment
  `style.preset` drives rendering. Single renderer code path.

### Phase 2.5 — history + design polish
- Backend `display: str` field on history entries (descriptive vs
  raw action keys)
- Cinematic dark theme — Instrument Serif / Geist / JetBrains Mono
- Refined slider styling — visible track + bigger handles
- Cache-bust on video URL (`?v=<journal-len>`) so re-renders refresh

## Next

These are roughly ordered by impact, but pick whatever the user
prompts. None block the others.

### Per-word caption editing (TikTok mode)
TikTok-style clips are many short segments. The current editor edits
text per-segment, which works for "default" mode but is clumsy when
each segment is one word. Add a per-word inline editor: hover over a
word in the rendered preview, click to edit, save fires a partial
`set_clip_captions` with just that word's text changed. Effort: ~2hr.

### "Un-tiktokify" — merge per-word back to segments
Today `tiktokify` is destructive: original segment groupings aren't
preserved. Add an `untiktokify` mutator that re-groups adjacent
tiktok-styled segments back into longer ones (e.g., merge segments
whose start/end gaps are ≤ 50ms). Effort: ~2hr.

### Mid-stream caption styling
Pick a different style preset per segment from a dropdown — title,
subtitle, tiktok, default. Add a preset table to the design system
and let the editor pick from a `<select>`. Effort: ~1hr.

### Per-clip remove (wire the stub)
The `MORE ACTIONS · COMING SOON` row has a `REMOVE CLIP` button
stubbed. Wire it to the existing `POST /edit/compile/:id/remove`
endpoint with a confirmation dialog. Effort: ~30min.

### Per-clip zoom / focus effects (wire the stubs)
Same as above — endpoints exist (`POST /edit/compile/:id/effect`).
Need a small ROI picker UI for zoom (preset name OR click-to-pick).
Effort: ~2hr.

### Asset browser page
A `/assets` route showing every indexed recording, with per-asset
action buttons: transcribe / rank / make compilation. Today the only
way to start a new compilation is via MCP or `curl`. Effort: ~3hr.

### Full-reel scrubber
A horizontal timeline UNDER the video showing the whole reel with
clip boundaries marked, draggable playhead. Bound to
`VideoPlayer.onTimeUpdate`. Effort: ~3hr — the harder bit is the
drag-precision logic; rendering is straightforward.

### Multi-speaker tagging UI (Phase 4 prep)
A speaker picker in the caption editor (per-segment dropdown:
"NoName" / "Player 2" / "Caster"). Stores speaker + colour in
`segment.style`. Renderer already reads style — no backend renderer
change. Backend needs a transcripts-table column for speaker.
Effort: ~3hr UI + ~1hr backend schema.

### Screen-shake effect (Phase 5)
New ffmpeg filter chain element for the renderer. Trigger via the
detail panel — a checkbox "shake on impact". Effort: ~2hr backend
+ ~30min UI.

### MCP-trigger buttons in detail panel
"Tell Claude to zoom this clip on the kill" → sends a prompt to
Claude with the clip context, triggers an MCP tool call, refetches.
Needs a thin pass-through endpoint on `api/` (LLM call →
mcp-tool dispatch). Big design discussion before building. Effort:
~1 day.

### Light theme
CSS-variable swap. Should "just work" since every colour is a token.
Effort: ~30min (mostly testing).

### Keyboard hotkeys
Space = play/pause, J/K/L for scrubbing, arrow keys for clip
selection, etc. Add a hotkey provider component. Effort: ~2hr.

### E2E tests
Playwright + Vitest. Smoke-test the critical flows (load, scrub,
edit caption, revert). Effort: ~1 day.

## Out of scope / explicitly NOT building

- **No client-side rendering / preview** — ffmpeg is on the server.
  Browsers can't render the spec.
- **Multi-user / collaboration** — single-user app on `localhost`.
- **Self-hosted in prod** — `web/dist` could be served via FastAPI's
  static files at some point, but right now this is local-only.
- **Mobile-first** — the editing workflow needs a real screen. Mobile
  view-only is fine; mobile editing isn't a goal.
- **Heavy state-management library** — TanStack Query + useState is
  enough for this app's complexity. No Zustand/Redux/Jotai.
- **shadcn CLI install** — hand-writing the ~6 UI primitives we use is
  less overhead than the dependency.

## Open questions

- **MCP-from-browser** — how does the LLM tool-call surface from a
  React app actually look? Plain `fetch` to an `api/llm/dispatch`
  endpoint that proxies to Anthropic? Or run a local MCP client
  inline? Needs a design pass before building.
- **Live preview during slider drag** — should the video update its
  bounds mid-drag (every keystroke), or only on release (current
  behavior)? Real video editors do the live preview but require a
  proxy/lo-res render pipeline that doesn't exist here.
- **Spec.json schema versioning** — once we add more per-segment
  fields (Phase 4+), should we add an explicit `schema_version`
  field? Currently the renderer is tolerant to missing fields; a
  version field would let us hard-fail on incompatible data.

## How to use this doc

Each "Next" item has a rough effort estimate so you can pick what
fits the session. Add new items here as they come up — don't let
ideas live only in chat. Move items between sections as state
changes. Keep "Out of scope" honest — every entry there should be
something we've actively decided NOT to build, not just unclassified
backlog.
