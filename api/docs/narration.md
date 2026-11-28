# Narration API

Voice (and optional camera) over a finished video, edited as takes on a
timeline, exported to `narrated.mp4`. Code: `src/ai_video_editor/narration/`
(pure take math in `edits.py`, ffmpeg argv in `mix.py`, use-cases in
`service.py`), routes in `routers/narration.py`. Web page and usage:
`web/docs/features/narration.md`. Design: `docs/specs/spec-narration-v2.md`.
All times are video seconds. `{kind}/{key}` names the video (`story/<round>`,
`joined/<stem>`).

## Data model (`narration/models.py`)

- **Take**: `id`, `start_s`, `duration_s`, `media` (file in `takes/`),
  `has_camera`, `created_at`; v2: `media_in_s` (where in the recording it
  plays from), `offset_s` (nudge, -3..3), `gain` (0..4], `muted`. Effective
  start = `start_s + offset_s`. Minimum length 0.3 s.
- **AudioMix**: `narration_gain` (1.0), `duck_threshold` (0.02), `duck_ratio`
  (8, 1..20), `duck_attack_ms` (20), `duck_release_ms` (400), `game_gain`
  (1.0, 0..2).
- **GameAudioRegion**: `id`, `start_s`, `end_s`, `level` 0..2.
- **CameraRegion**: `id`, `start_s`, `end_s`, `box` (CameraBox).
- **CameraBox**: `enabled`, `width_frac` (0.08..0.5), `x`, `y` (frame
  fractions, top-left; None = game profile default), `rounded`.
- **NarrationSession**: `target`, `game`, `video_fingerprint`, `takes`,
  `camera`, `mix`, `game_regions`, `camera_regions`, `pinned_intro_s` (the
  video's intro length when the first take was recorded or at the last relock).
- `SessionView` (every session route returns it): `session`, `video_url`,
  `game`, `stale`, `camera_px`, `default_camera_px`, `export_url`,
  `narration_url`, `suggested_shift_s`, `markers`.

Stale = the video file's fingerprint (size + mtime_ns) differs from
`session.video_fingerprint`. `suggested_shift_s` = current `intro_s` minus
`pinned_intro_s` (None when equal or unknown).

## Routes (prefix `/narration/{kind}/{key}`)

Unknown kind or key: 404; bad characters in kind/key: 422 (all routes).

| Method, path | Body / query | Returns | Errors |
|---|---|---|---|
| GET `` | | SessionView (creates an empty session on first call) | |
| GET `/frames` | | FrameStrip (sprite JPEG, cached) | 500 |
| GET `/peaks` | | Peaks (game-audio loudness, cached) | 500 |
| GET `/markers` | | Marker list (target markers + `cues.json`), by time | |
| POST `/takes` | raw `video/webm` or `audio/webm`; query `start_s`, `duration_s`, `has_camera`, `replace` (default true) | Take | 415 type, 413 over 500 MB, 422 (`start_s` < 0, `duration_s` ≤ 0 or < 0.3 s) |
| PATCH `/takes/{id}` | TakeEdit (any of start_s, duration_s, media_in_s, offset_s, gain, muted) | SessionView | 404, 422 (too short, starts before 0) |
| POST `/takes/{id}/split?at_s=` | | SessionView | 404, 422 (within 0.3 s of an end) |
| DELETE `/takes/{id}` | | SessionView | 404 |
| PUT `/camera` | CameraBox | SessionView | |
| PUT `/mix` | AudioMix | SessionView | |
| PUT `/game-regions` | list of GameAudioRegion (replaces all) | SessionView | 422 overlap, or a region with `end_s <= start_s` |
| PUT `/camera-regions` | list of CameraRegion (replaces all) | SessionView | 422 overlap |
| POST `/relock` | `{shift_s}` (optional, default 0) | SessionView | |
| GET `/hook` | | HookView: the hook track (saved edits if any, else what is rendered); each pick carries `rendered_rec_start_s` / `rendered_rec_end_s` (what the mp4 shows, null for a new piece) | 404 no hook data (not a joined video, or a map older than v2), 422 unreadable edits file |
| PUT `/hook` | `{picks, mode?, title?}` (whole list) | HookView | 404, 409 stale (laptop re-joined since), 422 bad picks or an unreadable edits file |
| DELETE `/hook` | | HookView (edits file removed) | 404 |
| GET `/storyboard` | | games and chapters (`id`, kind, game-clock window, `video_start_s`, kill/death times, `in_hook`, `has_hook_window`, `hook_len_s`) plus the hook view | 404, 422 unreadable edits file |
| POST `/export` | `{force}` (optional) | `{job_id}` (job type `narration_export`) | 422 no takes or all takes muted (muted takes are left out of the export), 409 stale unless forced |

Marker kinds: `chapter`, `game`, `cue`, `insert`.

## Files on disk (`<target.work_dir>/narration/`)

- `session.json`: the NarrationSession (written atomically).
- `takes/<id>.webm`: recordings. Split halves share one file; it is deleted
  when no take plays from it any more.
- `cache/frames-<fp>.jpg` + `.json`: frame-strip sprite and its grid;
  `cache/peaks-<fp>.json`: `{rate_hz, peaks}`. `<fp>` is the video fingerprint.
- `cues.json` (optional, hand-written): `[{"at_s": 12.5, "label": "..."}]`;
  shown as `cue` markers (label cut to 80 chars); a missing or invalid file
  is ignored.
- `narrated.mp4`: the export. Story: `story/<round>/narration/`; joined:
  `story/_joined/<key>/narration/`.

## Punch-in (record over)

A new take replaces what it covers (`replace=true`), compared on effective
time: older takes under it are trimmed at the head or tail (`start_s` and
`media_in_s` advance), dropped when fully covered, or split in two (the tail
gets a new id) when the new take sits inside one. Takes outside it are
untouched. `replace=false` just appends.

## Mix recipe (`mix.py`)

Input 0 is the video, take k is input k. Each take: `atrim=start=media_in_s:
duration=duration_s`, `volume=gain`, stereo, `adelay` to its effective start.
Takes are summed (`amix`, no normalising), times `narration_gain`, then split
into the voice and a sidechain key. Game audio: `volume=game_gain`, then per
region `volume=level` with `enable='between(t,start,end)'`. The game is
ducked by `sidechaincompress` (threshold, ratio, attack, release) keyed on
the voice (padded so the game is not cut when the voice ends), then summed
with the voice.

Camera: each camera take is cut at every camera-region edge inside it
(`camera_windows.py`); a piece uses its region's box, or the session camera
when none; pieces whose box is not `enabled` are skipped, pieces under 0.05 s
dropped. Each piece is overlaid only while it lasts, corners rounded if
`rounded`. Without camera windows the video stream is copied.

## Relock

`POST /relock` accepts the video as it is now: new fingerprint, new
`pinned_intro_s`. With `shift_s`, every take `start_s` and every region moves
by it first (clamped at 0; regions ending before 0 are dropped). The page
offers `suggested_shift_s` when a re-render lengthened the intro.

## Targets (`narration/targets.py`)

`NarrationTarget` protocol: `kind`, `video_path(key)`, `work_dir(key)`,
`game(key)`, `video_url(key)`, `list_keys()`, `intro_s(key)` (length before
the first game, None if unknown), `markers(key)` (landmarks for the
timeline). Mixin `TargetDefaults` supplies `intro_s` = None and `markers` =
[]; override what the target knows. Register with `register_target`
(duplicate kind raises). Shipped: `story` (round chapters as markers) and
`joined` (games, chapters, inserts and `intro_s` from `<key>.join.json`).
Stores implement `NarrationStore` (`store.py`); routes get the store and
exporter through FastAPI dependencies so tests can swap them.

## MCP tools (`mcp/`)

- `narration_session`: read the session (takes, mix, regions, markers, stale).
- `narration_status`: older alias of `narration_session`.
- `narration_edit_take`: trim / move / nudge / gain / mute one take.
- `narration_split_take`: cut a take in two.
- `narration_delete_take`: delete a take.
- `narration_remove_take`: older alias of `narration_delete_take`.
- `narration_set_mix`: voice gain, game gain, duck ratio.
- `narration_set_game_regions`: replace the game-audio regions.
- `narration_set_camera`: show/hide and place the camera box.
- `narration_set_camera_regions`: replace the camera regions.
- `narration_relock`: accept the video, optionally shifting takes.
- `narration_export`: mix to `narrated.mp4` (waits for the job).

- `hook_picks_get` / `hook_picks_set` / `hook_picks_reset`: read, replace, discard the hook track of a joined video.
- `storyboard_get`: the video as games and chapters (chapter ids for picks).
- `story_join(apply_edits="<stem>", force_edits=)`: laptop only, applies the saved hook edits.

Recording is browser-only (microphone).

## Hook clip edits (joined videos)

Spec: `docs/specs/spec-clip-edits.md`. The owner edits the hook on the PC page;
nothing renders there. Code: `story/hook_edits.py` (file + track maths),
`narration/hook_service.py` (use-cases), `story/apply_edits.py` (laptop).

- File: `WORKSPACE/story/<stem>.edits.json`, written atomically:
  `{schema_version, video, base_digest, hook: {mode, title, picks}}`. A pick is
  `{id, chapter_id, at_s, start_s, end_s}`: `at_s` = where it sits on the
  hook track (swipes not counted); `start_s` / `end_s` are offsets from the
  chapter's base window in recording seconds (`+start_s` = starts earlier,
  `+end_s` = ends later). The same chapter may appear twice (a split), each
  pick with its own `id`. `base_digest` hashes the map's hook ids and base
  windows.
- Track rules: extend eats empty space first, then pushes later clips; trim
  and delete leave empty space; overlaps are pushed later. Empty space is an
  editor thing: the laptop joins the clips back to back in `at_s` order.
- Errors: 404 no hook data (needs a v2 join map with a `hook` block), 409 when
  the map changed since the edits (`stale_edits`; discard with DELETE, or the
  laptop passes `--force-edits`), 422 for bad picks on PUT (unknown
  `chapter_id`, duplicate pick `id`) and for an unreadable edits file on PUT,
  GET `/hook` and GET `/storyboard`; offsets past the neighbouring chapters
  are clamped, not refused.
- Laptop: `story-join --apply-edits <stem>` (MCP `story_join(apply_edits=)`).
  Refuses a changed map unless `--force-edits`, and refuses the join flags
  (`--out`, `--title`, `--hook*`, `--outro`, `--music*`, `--subscribe-after`;
  the HTTP route 422s on `rounds`/`hook`/`title`/... with `apply_edits`). One
  managed `extend` request (`source: "clip_edit"`) per chapter whose clip runs
  past its footage; each touched round re-renders once (with plays when its
  chapters carry `play_id`s). The re-join takes the picks, mode and title from
  the edits file and the options (title, subscribe, outro, music start) from
  the map. Offsets are re-based on the base window of the re-rendered chapter
  (an extend moves it), so the new map's base ± folded offsets is exactly what
  was rendered and a second apply with the same offsets adds no extend. It
  refuses (nothing re-joined) when the edits had picks and none resolved. The
  edits file is deleted after. The new map is v2: `hook` block (base windows
  in recording seconds, events, `at_s`, folded offsets) and `hook_rec_*` on
  every chapter with kills or deaths that is long enough for a hook, so
  chapters can be added to the hook later.
- A join map from before this feature has no hook block: the page shows
  markers only until the laptop re-joins on this branch.
