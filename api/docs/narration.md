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
| POST `/takes` | raw `video/webm` or `audio/webm`; query `start_s`, `duration_s`, `has_camera`, `replace` (default true) | Take | 415 type, 413 over 500 MB |
| PATCH `/takes/{id}` | TakeEdit (any of start_s, duration_s, media_in_s, offset_s, gain, muted) | SessionView | 404, 422 (too short, starts before 0) |
| POST `/takes/{id}/split?at_s=` | | SessionView | 404, 422 (within 0.3 s of an end) |
| DELETE `/takes/{id}` | | SessionView | 404 |
| PUT `/camera` | CameraBox | SessionView | |
| PUT `/mix` | AudioMix | SessionView | |
| PUT `/game-regions` | list of GameAudioRegion (replaces all) | SessionView | 422 overlap |
| PUT `/camera-regions` | list of CameraRegion (replaces all) | SessionView | 422 overlap |
| POST `/relock` | `{shift_s}` (optional, default 0) | SessionView | |
| POST `/export` | `{force}` (optional) | `{job_id}` (job type `narration_export`) | 422 no takes, 409 stale unless forced |

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

Recording is browser-only (microphone).
