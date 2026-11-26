# Spec: narration recorder (voice + optional camera over a finished video)

Status: approved by the owner 2026-11-25 ("sure that's fine ... make the code
extendable to other games, scalable ... maintainable and readable using SRP
and OCP"). Jira Epic: see `AVE-68` (filed with this spec).

## What the owner gets

A web page `/narrate/<kind>/<key>` (first kind: `story`, key = round folder,
e.g. `/narrate/story/2026-11-23_2056_zeri_8-7-12`):

1. The video plays. **Record** starts playback and captures the mic (and the
   camera when the camera toggle is on). **Stop** pauses and saves the take,
   pinned to the video second it started at.
2. Takes appear on a timeline under the video: play back, delete, re-record.
3. A **camera box** overlay on the video: drag to move, drag the corner handle
   to resize. Default position comes from the game profile (League: just
   above the minimap, bottom right). Showing the camera in the export is
   optional (off unless the owner turns it on).
4. **Export** builds `narrated.mp4`: the original video with every take mixed
   in; the original audio (game + in-game mic) ducks automatically while the
   owner talks; the camera box is overlaid during takes that have camera.
5. If the underlying video changes after takes were recorded (a re-render),
   the page says the takes may no longer line up; export refuses until the
   owner re-locks (keep takes as they are) or deletes them.

Privacy: takes (voice and camera) stay on this PC, like mic transcripts.
Nothing here uploads to Drive or Supabase.

## Design rules (from the owner)

- **SRP**: one reason to change per module (see the map below).
- **OCP**: a new game = a new `profiles/<game>.toml` `[camera]` block; a new
  narratable thing (montage, short, IRL video) = a new `NarrationTarget`
  class registered by `kind`; a new storage backend (DB, cloud, multi-user)
  = a new `NarrationStore` implementation. No existing code edits for any of
  these.
- **DIP**: routers and export depend on the `NarrationTarget` /
  `NarrationStore` protocols, never on files or the story module directly.
- Pure cores (layout math, ffmpeg argv) are unit-tested without ffmpeg.

## Module map (`api/src/ai_video_editor/narration/`)

| Module | Responsibility | Changes when |
|---|---|---|
| `models.py` | Pydantic data: `TargetRef`, `Take`, `CameraBox`, `AudioMix`, `NarrationSession`, `PixelBox` | the data shape changes |
| `targets.py` | `NarrationTarget` protocol, registry (`register_target`, `get_target`), `StoryRoundTarget` | a new kind of video is narrated |
| `store.py` | `NarrationStore` protocol + `FileNarrationStore` (session.json + takes/) | storage moves (DB, cloud, per-user) |
| `layout.py` | camera box → pixels from the game profile `[camera]` defaults + owner overrides | box placement rules change |
| `mix.py` | pure ffmpeg argv for the export (ducking, take audio, camera overlay) | the mix recipe changes |
| `export.py` | orchestration: stale check → argv → run → `narrated.mp4` | the export flow changes |
| `routers/narration.py` | HTTP only | the API changes |
| `mcp server` tools | chat surface | the chat surface changes |

Profiles: `profiles/__init__.py` gains `CameraDefaults` and `Profile.camera`;
`league.toml` and `default.toml` gain a `[camera]` block.

## HTTP API (prefix `/api/v1/narration/{kind}/{key}`)

`kind` must be a registered target kind (else 404); `key` must match
`^[0-9A-Za-z_\-]+$` and resolve to an existing video (else 404).

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `` | — | `SessionView` (session + `video_url`, `game`, `stale`, `camera_px` default box for a 1920x1080 frame) |
| POST | `/takes?start_s=<float>&duration_s=<float>&has_camera=<bool>` | raw bytes, `Content-Type: video/webm` or `audio/webm` (max 500 MB) | `Take` |
| DELETE | `/takes/{take_id}` | — | `SessionView` |
| PUT | `/camera` | `CameraBox` | `SessionView` |
| POST | `/relock` | — | `SessionView` (fingerprint updated to the current video) |
| POST | `/export` | `{"force": false}` | `{"job_id"}` (jobs table, type `narration_export`; output `narrated.mp4`) |

Raw-body upload (no multipart) so no new dependency (`python-multipart` is not
installed). The browser `fetch`es the `Blob` directly.

## Files

`<target.work_dir(key)>/narration/session.json` and
`.../narration/takes/<take_id>.webm`; export writes
`.../narration/narrated.mp4`. For `story` targets `work_dir` is
`WORKSPACE/story/<round>/`, so the existing `/workspace` static mount serves
both the source video and takes to the browser (HTTP Range works).

## Mix recipe (mix.py)

- Take audio: `adelay` to `start_s`, `amix` all takes (normalize=0) → `[narr]`.
- Ducking: `sidechaincompress` on the original audio keyed by `[narr]`
  (`AudioMix`: threshold, ratio, attack, release; defaults duck the original
  by ~12 dB while the owner talks), then `amix` original-ducked + narration
  (normalize=0), `narration_gain` applied to `[narr]`.
- Camera (only when `CameraBox.enabled` and a take `has_camera`): each take's
  video `setpts=PTS-STARTPTS+start_s/TB`, scaled to the box, rounded corners
  (alpha mask), `overlay` at the box with `enable='between(t,start,end)'`.
- No camera shown → video stream copied (`-c:v copy`), audio re-encoded only.
  Camera shown → video re-encoded with `settings` encoder options.

## Story determinism (prerequisite)

Story re-renders currently re-run the early-game vision check, which returns
slightly different windows each time, so narration takes would drift. The
vision result is cached per asset so a re-render reproduces the same plan
unless the owner asks for a fresh check.
