# Compilation viewer layout

`/compilations/:id` — the hero screen. Where editing happens.

## The 7/5 split

```
┌──────────────────────────────────────┬────────────────────────┐
│ LEFT (lg:col-span-7)                  │ RIGHT (lg:col-span-5)  │
│ ┌──────────────────────────────────┐  │ ┌────────────────────┐ │
│ │       VIDEO PLAYER               │  │ │                    │ │
│ │       (16:9, native controls)    │  │ │  CAPTION EDITOR    │ │
│ └──────────────────────────────────┘  │ │  (most prominent)  │ │
│ ┌──────────────────────────────────┐  │ │                    │ │
│ │ FILMSTRIP · horizontal scroll    │  │ │  (add / edit /     │ │
│ │ [#01] [#02] [#03] [#04] [#05] …  │  │ │   remove / tiktok) │ │
│ └──────────────────────────────────┘  │ └────────────────────┘ │
│ ┌──────────────────────────────────┐  │ ┌────────────────────┐ │
│ │ CLIP META PANEL                  │  │ │ MORE ACTIONS       │ │
│ │ • header + badges                │  │ │ (stubs for now)    │ │
│ │ • extend-or-shrink slider        │  │ └────────────────────┘ │
│ └──────────────────────────────────┘  │ ┌────────────────────┐ │
│                                       │ │ HISTORY            │ │
│                                       │ │ (compact, revert)  │ │
│                                       │ └────────────────────┘ │
└──────────────────────────────────────┴────────────────────────┘
```

Asymmetric on purpose — per the frontend-design skill, predictable
50/50 layouts are forbidden. The video gets visual weight (left) while
the work surface (captions) gets prominent right placement.

## What lives where + why

**LEFT — review surface.** Video player, filmstrip, and clip metadata
are about UNDERSTANDING what you have. Read-mostly.

- VideoPlayer (`components/VideoPlayer.tsx`) — controlled `<video>`
  with `onTimeUpdate` exposed for filmstrip sync, `version` prop for
  cache-busting after re-renders.
- ClipFilmstrip (`components/ClipFilmstrip.tsx`) — horizontal strip
  with per-clip thumbnails. Auto-scrolls to the currently playing
  clip; selected tile has bold accent border.
- ClipMetaPanel (`components/ClipMetaPanel.tsx`) — selected clip's
  identity (index, reel time, source time, badges) plus the reel
  timeline extend slider.

**RIGHT — editing surface.** Captions, action stubs, history. Write-
mostly.

- ClipActionsPanel (`components/ClipActionsPanel.tsx`) — wraps the
  CaptionEditor and the future-action stubs.
- HistoryRail (`components/HistoryRail.tsx`) — compact undo log.

## Why everything stays mounted

Earlier iterations had two conditional JSX branches (no-selection vs
selected). When the no-selection branch swapped to selected, the
`<VideoPlayer>` element remounted, losing `currentTime` and dropping
the `onTimeUpdate` handler mid-scrub. This broke the filmstrip's
follow-the-playhead behavior.

The fix: ONE JSX layout. VideoPlayer, ClipFilmstrip, and HistoryRail
always render. ClipMetaPanel and ClipActionsPanel are conditional
(rendered only when a clip is selected). The video element never
remounts.

```tsx
<div className="grid lg:grid-cols-12">
  <div className="lg:col-span-7">
    <VideoPlayer ... />                                  // always
    <ClipFilmstrip ... />                                 // always
    {selected && <ClipMetaPanel clip={sel} ... />}        // conditional
  </div>
  <aside className="lg:col-span-5">
    {selected && <ClipActionsPanel clip={sel} ... />}     // conditional
    <HistoryRail ... />                                   // always
  </aside>
</div>
```

## URL state — selected clip

The selected clip's UUID lives in `window.location.hash` as `#clip=<uuid>`.

- Refresh restores the focus.
- Back/forward navigates between selected clips.
- Default behavior: select the first clip when data lands.
- If a revert removes the currently selected clip, fall back to the
  first clip so the panels don't show stale data.

## Code path

- Page: `src/pages/CompilationViewer.tsx`
- Hook: `src/hooks/useCompilation.ts` — composite query bundling
  metadata + clips + history + every mutation
- URL helpers: `buildWorkspaceUrl`, `buildWorkspaceFolderUrl`,
  `folderStem` at the bottom of the page file
