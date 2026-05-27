# History rail

Compact undo log on the right column, under the caption editor.

## What it shows

Every spec change since the initial compile, newest at the top.
Each row: version number, descriptive action, relative timestamp,
and a `REVERT` button (except the current version).

Examples of the descriptive text (driven by the backend's
`display` field in `summarise_journal`):

```
v10  Added caption to clip #01 · 'hello'
v09  Removed caption from clip #01 (segment #0)
v08  Clip #01 → tiktok captions
v07  Clip #07 → tiktok captions
v04  Reordered clips by hype
v03  Inserted intro 'noodlz-text-v1' after clip #01
v02  Extended clip #02 (+5.3s after)
v01  Initial compile (hook order, limit 10)
```

Compare with the OLD raw `action` strings (`caption_mode:tiktok`,
`reorder:hype`, etc.) — the descriptive version is much faster to
scan. The raw `action` is preserved as a tooltip on hover so power
users can still see the underlying key.

## Revert behavior

Click `REVERT` on any version → backend restores that version's
spec snapshot, re-renders, and appends a new journal entry
(`Reverted to vNN`). Reverts are themselves revertable.

The current version (newest journal entry) doesn't show a REVERT
button — there's nothing to revert to. It's also highlighted with a
slight `bg-bg-overlay/40` tint.

## Implementation

- Component: `src/components/HistoryRail.tsx`
- Backend source: `summarise_journal` in
  `api/src/ai_video_editor/compile_journal.py` — computes the
  `display` field per entry from the raw `action` + `details`
- Endpoint: `GET /edit/compile/:id/history`
- Refresh: TanStack Query invalidates after every mutation, so the
  rail re-fetches on its own. No manual refresh.

## Why backend computes the display text

Two reasons:

1. **MCP also benefits.** The `list_compilation_history` MCP tool
   returns the same `display` field; the LLM sees pretty strings
   when summarizing edit history.
2. **Single source of truth.** As new mutators ship, their pretty
   phrasing lives in ONE place (`format_action_display` in
   `compile_journal.py`). The web doesn't need to keep a parallel
   action-name → English mapping in sync.

## Adding a new mutator's display text

When you add a new mutator + endpoint + journal action key:

1. Add a `if action == "your_action":` branch in
   `format_action_display` in `compile_journal.py`.
2. Use the `_format_clip_ref(details.get("clip_ref"))` helper for
   `#NN` styling of clip references.
3. Return a sentence-case string. Match the existing tone:
   "Extended clip #03 (+2.5s after)", "Removed clip #05".

## Future improvements

- **Group consecutive same-action entries** (e.g., 4 `extend_clip`
  edits become "Extended clip #03 4 times (+8.0s)" with expand-arrow
  to see individual ops).
- **Filter** by action type (show only "remove", only "extend", etc.).
- **Jump to revision diff** — show a side-by-side of what changed
  vs the prior version.
- **Per-edit thumbnail** — store a thumbnail of the affected clip at
  the time of the edit. Diff-time scrubbing.
