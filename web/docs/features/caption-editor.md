# Caption editor

The right-column primary editing surface. Where you correct
Whisper's mistakes, add missing lines, flip styles.

## The unified caption model

`caption_mode` (the old `"segment" | "tiktok"` flag) is deprecated.
Each caption segment now carries an optional `style` field. The
renderer reads per-segment style and emits one drawtext filter per
segment.

```typescript
interface CaptionSegment {
  start_seconds: number;
  end_seconds: number;
  text: string;
  words?: CaptionWord[];          // Whisper word timings (optional)
  style?: {
    preset?: "default" | "tiktok";
    fontsize?: number;            // override preset
    color?: string;
    y_position?: string;          // ffmpeg expr e.g. "h-th-60"
    border_width?: number;
    border_color?: string;
  };
  sentiment_score?: number;
}
```

**"TikTok mode" is now data, not code.** A clip in TikTok style is a
clip whose `caption_segments` are many short word-segments each
tagged with `style.preset = "tiktok"`. There's no separate code path
in the renderer.

This is extensible: adding a "Netflix" or "title-card" preset = one
new entry in the backend's `STYLE_PRESETS` table. Per-segment
overrides let users tweak individual lines without forking a preset.

## Operations

| Action | What happens | Backend |
|---|---|---|
| **Edit text inline** | Type in a textarea, click `save edits` | `POST /clip_captions` (full replacement; drops `words` on edited segments) |
| **+ add caption** | Opens a new-segment row with start/end inputs + text. Click `add to clip` | `POST /clip_captions/add` |
| **× delete** | Click the `×` on a segment row | `POST /clip_captions/remove` |
| **TikTok-ify clip** | Explodes every segment into per-word segments + applies `tiktok` preset | `POST /clip_captions/tiktokify` |

All mutations refetch via TanStack Query's `invalidateQueries` on
success — the editor re-syncs from server state.

## Dirty tracking

When the user edits a segment's text, that row gets the `border-accent/40`
state + an `EDITED` badge. On save:

- For each segment whose text changed: send the new text + drop
  `words` (the old Whisper boundaries don't match the new text;
  renderer will even-split).
- For unchanged segments: send the original segment unchanged
  (including `words`) so per-word timing is preserved in TikTok mode.

`reset` clears all edits without saving.

## New-segment row UX

Clicking `+ add caption` opens an inline form with:
- `start` / `end` number inputs (default: right after the last existing
  segment ends, ~2s slot, clamped to the clip's source range)
- Multi-line textarea for the caption text
- `add to clip` button — only enabled when text is non-empty and `end > start`

Pressing the button fires `add_caption_segment` with the entered
values. The new segment is sorted into the existing list by start
time.

## Style-tagged rows

If a segment has `style.preset = "tiktok"` (typically after a
tiktokify), the row shows a `TIKTOK` accent badge. Useful when a
clip has mixed-style segments.

## Code path

- Component: `src/components/CaptionEditor.tsx`
- Wrapper: `src/components/ClipActionsPanel.tsx` (provides the panel
  chrome + future-action stubs)
- Hook: `useCompilation` exposes `editCaptions`, `addCaption`,
  `removeCaption`, `tiktokify` mutations
- Backend mutators: `compile.set_clip_captions`, `add_caption_segment`,
  `remove_caption_segment`, `tiktokify_clip` (in
  `api/src/ai_video_editor/compile.py`)
- Backend renderer: `caption_filters` + `resolve_caption_style` in
  `api/src/ai_video_editor/edits.py`
- Legacy migration: `_build_clip_filterchain` in `compile.py` detects
  `caption_mode == "tiktok"` on un-styled segments and explodes
  them at render-time (no spec mutation)

## What it does NOT do (yet)

- **Per-WORD editing in TikTok mode** — currently the editor shows
  one row per segment. After tiktokify, each segment is one word — so
  per-word edit works by editing each tiny segment. UX could be
  better with a word-grid view. See roadmap.
- **Speaker tagging** — no UI yet. The `style` field is forward-
  compat for per-speaker colors in Phase 4.
- **Live preview during edit** — text changes only burn into the
  video on save. A live drawtext-style preview overlay would be cool;
  out of scope for v1.

## Backend touchpoints

The caption editor exercises five backend endpoints. See `api/`
docs for the full surface; this is the subset web hits:

| Endpoint | Mutator | When |
|---|---|---|
| `POST /clip_captions` | `set_clip_captions` | Full replacement (save edits) |
| `POST /clip_captions/add` | `add_caption_segment` | Add one segment |
| `POST /clip_captions/remove` | `remove_caption_segment` | Delete by index |
| `POST /clip_captions/tiktokify` | `tiktokify_clip` | Explode + restyle |
| `POST /caption_mode` | `set_caption_mode` (legacy) | Routes mode=tiktok to `tiktokify_clip`; mode=segment clears the legacy field |
