# Videos hub (`/videos`)

Every video you can narrate, in one place. Spec:
[`docs/specs/spec-video-hub.md`](../../../docs/specs/spec-video-hub.md) (Part 2).

## Pieces

- **API** `GET /api/v1/videos` returns a `VideoSummary` per video: `kind`,
  `key`, `title` ("Jhin 8/2/13", from the key `YYYY-MM-DD_HHMM_champ_K-D-A`),
  `game`, `date`, `duration_s` (ffprobe, cached per file version),
  `video_url`, `take_count`, `stale`, `exported`, `suggestion_status`
  (`none | pending | ready | applied`, read from the round's
  `suggestions.json`). The endpoint loops the narration target registry, so a
  new kind appears once its `NarrationTarget.list_keys()` exists.
- **Page** `/videos` (top-bar tab **Videos**): one card per video with title,
  game, date, length (m:ss), takes, **exported ✓**, a **suggestions ready**
  badge and a **narrate** link to `/narrate/<kind>/<key>`.
- **Switcher** `VideoSwitcher` in the narration page header: a dropdown of the
  same list (query key `["videos"]`); picking one opens that video's page.

Story rounds are the folders of `WORKSPACE/story/` that contain `story.mp4`
(names starting with `_` are skipped), newest first.

Files: `web/src/pages/VideosList.tsx`, `web/src/components/VideoSwitcher.tsx`,
`web/src/api/videos.ts`, `web/src/types/videos.ts`; API in
`api/src/ai_video_editor/narration/catalog.py` and `routers/videos.py`.
