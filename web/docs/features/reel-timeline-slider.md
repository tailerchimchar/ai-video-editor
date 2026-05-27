# Reel-timeline extend slider

The dual-handle range slider in the `ClipMetaPanel`. Lets you extend
or shrink the currently-selected clip's source window by dragging
either edge.

## The mental model

Every clip's slider uses the **SAME axis** — the whole reel timeline,
from 0 to (total reel seconds + 30s buffer). Each clip's "active
range" is its current reel position.

This way you can SEE the clip in context — where it sits in the
overall reel — and adjust its boundaries with direct manipulation.

```
axis: 0:00 ─────────────── 0:28 ─────────────── 0:56
          ┃               clip ┃                  ┃
       [other clips, faint]  ●═══════════●   [other clips, faint]
                            ↑             ↑
                       drag = extend  drag = extend
                       pre-roll       post-roll
```

Dragging:
- **LEFT handle LEFT** → extends pre-roll (adds source content
  BEFORE the current start). Positive `before` in the API call.
- **LEFT handle RIGHT** → trims pre-roll. Negative `before`.
- **RIGHT handle RIGHT** → extends post-roll. Positive `after`.
- **RIGHT handle LEFT** → trims post-roll. Negative `after`.

Deltas are in REEL space. They map 1:1 to source-seconds because
extending the source window by N seconds extends the clip's reel
duration by N seconds.

## Visual layers on the slider

The single track shows everything:

1. **Bottom layer** — the OTHER clips' positions as faint
   `bg-text-dim/30` segments. Lets you see the whole reel at a
   glance from any clip's editor.
2. **Active range** — THIS clip's current position as a bright
   `bg-accent/50` segment.
3. **Drag highlight** — `bg-accent` while dragging, showing the
   pending new range.
4. **Axis labels** — `0:00 · midpoint · total` above the track.

When the user drags, only THIS clip's slider responds visually.
After commit, all clips re-render via the next refetch with new
positions (downstream clips slide right when this one extends).

## Commit flow

Two paths to commit, both fire the same handler:

- **Release the handle** (`onValueCommit` from Radix Slider).
- **Click the "apply" button** — visible only when dirty.

The button shows "no change" when handles are at the original
position, "apply" when dirty, "applying…" while the mutation is
in flight.

Compute `before` + `after` deltas:
```typescript
const beforeDelta = clipReelStart - draggedStart;
const afterDelta = draggedEnd - clipReelEnd;
```

Send to `POST /edit/compile/:id/extend` with `{ clip_ref, before, after }`.

## Components

- **Track** — Radix `Slider.Track` with custom children for the
  background segments + Radix `Slider.Range` for the active drag.
- **Thumbs** — 20px circular handles with `border-2 border-accent` +
  `bg-text-primary` (white) + accent-glow ring. Hover scale 110%.
  This is the Linear / Figma look — bright on dark, easy to see.
- **Top row** — live timestamps (`mmss(draggedStart) → mmss(draggedEnd)`),
  duration, apply button.

## Code path

- Component: `src/components/ExtendSlider.tsx`
- Wrapper: `src/components/ClipMetaPanel.tsx` (provides the panel
  chrome + clip header)
- Hook: `useCompilation` exposes `extend` mutation
- Backend: `compile.extend_clip` mutator,
  `POST /edit/compile/:id/extend` endpoint

## Future improvements

- **Snapping** — snap handles to keyframes / scene boundaries
  detected during render. Needs a keyframe-extract step on the
  backend.
- **Keyboard arrows** for sub-second precision while a handle is
  focused. Radix Slider supports this natively; we'd just need to
  surface keyboard hints in the UI.
- **Live preview during drag** — re-render the video on drag with a
  low-quality proxy. Big undertaking (proxy pipeline doesn't exist).
- **Snap-to-clip-boundary** — handles can't currently cross
  neighboring clip ranges, but visually they can move freely. Would
  need a clamp at the previous clip's reel-end (left handle) and the
  next clip's reel-start (right handle).
