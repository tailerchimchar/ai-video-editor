# Spec: narration editor v2 (timeline, take editing, mix, hook montage)

Status: decided with the owner 2026-11-27 (interview, nine areas; the owner
left mid-interview with "no more questions, build what you need", so the last
three areas carry Claude's defaults, marked in the Decisions Log). Supersedes
nothing: `spec-narration.md` (v1) stays the base; this spec extends it.
Baseline commit: master `bc5b421`.

## 1. Problem

The owner narrates a 13-minute story-join (`joined_2026-11-26_xayah-kaisa-yunara`,
"How to destroy lane in 5 minutes") on the PC. v1 records takes pinned to the
video second and exports them, but:

1. The cold open shows two plays cut before their outcome and nothing else: no
   proof, no credential, and the owner is unsure what the opening should be.
2. A take is immutable once uploaded. There is no way to drop the last five
   seconds, redo a beat, split a take, nudge it, or mute it; only delete.
3. The timeline is one 40 px track of blocks. Nothing shows what the video is
   doing where, what the voice sounds like, or how loud the game is.
4. One global level for the voice and one duck setting for the whole video;
   neither can be changed from the page.
5. The camera box is one static box for the whole export.
6. Effects ("zoom out here") are story requests addressed in game clock and
   rendered on the laptop; nothing can be applied from the narration page.

## 2. Goals

- G1 The owner narrates **this** video within days: hook settled, takes
  recordable in pieces and fixable, per-take volume, a timeline that shows
  takes over the video, what you hear in the page is what exports.
- G2 Everything is non-destructive: webm files are never rewritten; edits are
  numbers in `session.json`.
- G3 v1's rules hold: SRP modules, OCP for kinds/games/stores, takes stay on
  this PC, nothing goes to Drive or Supabase from narration.
- G4 The PC does narration only (mic, camera, narrated export); joins and
  story renders stay on the laptop.

## 3. Non-goals (this spec)

- Smoothly animated camera box (keyframe tweening).
- Length-changing edits from the narration page (trim/extend/slow-mo of the
  video itself). Those stay story requests on the laptop.
- Text pops, captions or SFX authoring beyond the kinds listed in §4.8.
- Multi-user or cloud narration; mobile layout.
- A Clipchamp-grade general editor. This is a narration editor: the video is
  fixed, the lanes edit what sits on top of it.

## 4. Design

### 4.1 Hook: montage of plays, played through (owner)

The cold open becomes a 30-45 s montage of the best plays, each played
through its outcome, swipes between them, the opening line popping over clip 1,
no other text. The owner's voice (a narration take over `0..intro_s`) carries
the stat ("ten kills, zero deaths, first five minutes of three games") and one
credential sentence. No op.gg card (the Riot ID would be on screen), no
freeze/teaser in this mode.

- `HookPick` gains `mode: Literal["tease", "play"]` (default `tease`, the v1
  behaviour). `play` window: start = max(chapter start, first owner event −
  `HOOK_LEAD_S`), end = min(chapter end, last owner event + `HOOK_PLAY_TAIL_S`
  2.0), capped at `HOOK_PLAY_MAX_S` 14.0 from the end backwards. No freeze;
  0.25 s audio fade at the cut. Chapters with no events: first 6 s.
- CLI `story-join --hook-mode tease|play` applies to every `--hook` pick;
  `JoinBody.hook_mode` likewise; MCP `story_join(hook_mode=...)`.
- Default picks for this video (owner may swap): Yunara 2v3 (game 3 ch 2),
  Kai'sa 4v4 top (game 2 ch 2), Xayah 2v2 (game 1 ch 2), Yunara dive (game 3
  ch 5). Command the laptop runs:
  `story-join <xayah> <kaisa> <yunara> --hook 3:2 --hook 2:2 --hook 1:2 --hook 3:5 --hook-mode play --hook-title "Want to know the trick to laning?" --outro`
- The join map already records `intro_s`; the narration session pins it (see
  4.7) so a longer hook can shift takes instead of invalidating them.

### 4.2 Takes: punch-in model (owner)

Every Record→Stop is a take pinned to its start second (v1). New:

| Edit | Model change | Rule |
|---|---|---|
| Trim head/tail | `media_in_s` (seconds into the webm where the take begins), `duration_s` | head trim moves `start_s` and `media_in_s` by the same amount; tail trim shrinks `duration_s`; min 0.3 s |
| Split at the playhead | two takes share one `media` | A keeps `[media_in, media_in + (T−start))`; B starts at T with `media_in_s = A.media_in_s + (T − A.start_s)` |
| Nudge | `offset_s` in [−3, 3] | effective start = `start_s + offset_s`; the page shows the offset |
| Mute | `muted: bool` | excluded from the mix and the preview; drawn grey |
| Volume | `gain` in (0, 4] | `volume=` on that take's chain |
| Record-over (punch-in) | upload with `replace=true` (default) | the new take replaces whatever older takes cover its span: overlapped heads/tails are trimmed, fully covered takes deleted, a take spanning both sides is split. Pure function `punch_in(takes, new) -> takes` |

Media is reference-counted on delete: the webm is removed only when no other
take points at it. Takes shorter than 0.3 s after any edit are rejected (422).

### 4.3 Timeline (owner: four lanes, zoomable)

Under the video, one scrollable, zoomable timeline (`components/narration/timeline/`):

```
ruler      0:00 ........ 2:00 ........ 4:00 ........        [zoom - +] [fit]
frames     [▒▒▒][▒▒▒][▒▒▒][▒▒▒][▒▒▒][▒▒▒][▒▒▒][▒▒▒][▒▒▒]   frame strip (sprite)
markers    |Intro  |Xayah: The opening   |2v2   |…           chapters + cue ticks
takes      ▐▔▔waveform▔▔▌  ▐▔▔▌   ▐▔▔▔▔▔▔▔▔▌ (grey=muted)   trim handles, drag = nudge
game       ▁▂▃▅▆▅▃▂▁▂▃▅ with ducking dips, [30%] regions     drawn regions
camera     [default....][big........][hidden][default]      phase 2
                     ▲ playhead (rAF-driven)
```

- **Frame strip**: `GET /narration/{kind}/{key}/frames` returns
  `{sprite_url, every_s, tile_w, tile_h, cols, count}`; the API renders a JPEG
  sprite once per video fingerprint into `<narration>/cache/frames-<fp>.jpg`
  (ffmpeg `fps=1/every_s,scale=160:-2,tile=COLSx1`; `every_s` chosen so the
  count is ≤ 240). Served by the `/workspace` mount.
- **Game audio lane**: `GET .../peaks` returns `{rate_hz: 10, peaks: [0..1]}`
  computed once per fingerprint (ffmpeg → s16le mono 8 kHz → numpy abs max per
  window) into `<narration>/cache/peaks-<fp>.json`. Regions (4.5) are drawn on
  it; the preview's duck dips are drawn from the takes' spans.
- **Takes lane**: blocks at `start_s + offset_s`, width `duration_s`, waveform
  from `decodeAudioData` of the webm (cached per media in the page), green
  border when `has_camera`, grey when muted, selected block shows trim handles
  (edges) and drags as a nudge (±3 s, snaps back beyond). Right side of each
  selected block: gain slider, mute, split-at-playhead, delete.
- **Markers lane**: `GET .../markers` → `[{at_s, label, kind}]`. The target
  supplies chapters (`JoinedStoryTarget` from `.join.json`; `StoryRoundTarget`
  from `story.json`) and the store merges `<narration>/cues.json` (owner- or
  Claude-written beats, e.g. the cue sheet). Click = seek.
- **Zoom/scroll**: px-per-second in `lib/timeline.ts` (pure); wheel = pan,
  ctrl+wheel = zoom around the cursor, "fit" = whole video. Playhead from
  `requestAnimationFrame` reading `video.currentTime`, not `timeupdate`.

### 4.4 Preview: full mix in the browser (owner)

`hooks/useMixPreview.ts` builds a Web Audio graph when the page mounts:

- the `<video>` becomes a `MediaElementAudioSourceNode` → `gameGain` (global
  `game_gain` × region level at the playhead, updated on `timeupdate` and
  rAF) → `duckGain` → destination;
- each unmuted take: decoded `AudioBuffer` → `AudioBufferSourceNode` scheduled
  at `(start_s + offset_s) − video.currentTime` with `offset = media_in_s`,
  `duration = duration_s` → per-take `GainNode(gain)` → `narrBus` →
  `narration_gain` → destination;
- ducking: an `AnalyserNode` on `narrBus`; a rAF loop sets `duckGain` to
  `1/duck_ratio` (attack/release ramps from `AudioMix`) while the bus RMS
  exceeds `duck_threshold`. Close enough to `sidechaincompress` for judging a
  mix; the export is the reference.
- Re-schedule on play/seek/rate change and whenever the session changes.
  Old `useTakePlayback` (one take at a time) is removed.

### 4.5 Audio mix controls (owner)

- Per take: `gain`, `muted` (4.2).
- Game lane regions: `GameAudioRegion{id, start_s, end_s, level ∈ [0, 2]}`,
  non-overlapping (the API rejects overlaps, 422). Drag on the game lane to
  create, drag edges to resize, click to set the level, delete. Outside regions
  the global `AudioMix.game_gain` (new, default 1.0) applies.
- One **duck** slider mapped to `AudioMix.duck_ratio` (1 = off … 20 = hard),
  and a **voice** slider for `narration_gain`. `PUT .../mix` (new) saves
  `AudioMix`; `PUT .../game-regions` replaces the region list.
- Export: `[0:a]volume=game_gain` then one `volume=level:enable='between(t,a,b)'`
  per region, then the existing sidechain. Each take chain becomes
  `atrim=start=media_in:duration=dur,asetpts=PTS-STARTPTS,volume=gain,aformat,adelay`.
  Muted takes are not inputs at all.

### 4.6 Camera over time: regions, hard switch (owner; phase 2)

`CameraRegion{id, start_s, end_s, box: CameraBox}` on a camera lane; `box.enabled=false`
means hidden in that span. Outside regions the session's default `camera` applies.
Non-overlapping (422). Export: pure `camera_windows(take_span, regions, default)`
→ `[(a, b, PixelBox|None)]`; one overlay per window (`trim` the camera take to
the window, `setpts +a/TB`, scale to that box, mask, `overlay … enable=between(t,a,b)`).
The box editor over the video edits the region under the playhead (or the
default when none). `PUT .../camera-regions` replaces the list.

### 4.7 Re-render and staleness

- Fingerprint logic stays (v1). New: the session pins `pinned_intro_s`
  (from the target's `intro_s(key)`; `JoinedStoryTarget` reads `.join.json`,
  others return `None`) at the first take.
- `SessionView.suggested_shift_s` = current `intro_s − pinned_intro_s` when
  both exist and differ, else `null`. The stale banner offers **shift takes by
  Δ and relock** next to relock / delete all. `POST .../relock` takes
  `{shift_s: float = 0}`: every take's `start_s += shift_s` (clamped at 0),
  regions and camera regions shift the same way, then the fingerprint and
  `pinned_intro_s` re-pin.
- A hook change is the only expected length change before the games; the
  owner renders the montage hook **before** recording for this video anyway.

### 4.8 Effects from the timeline (Claude default; phase 3, not built now)

Length-preserving effects and SFX are applied **in the narration export on the
PC**, stored in the session in joined-video seconds:
`NarrationEffect{id, kind: zoom|zoom_out|focus|sfx, start_s, end_s, params}`
with per-kind whitelisted params (as `story.planner.ChapterEffect`). The
story renderer's zoom/zoom_out/focus filter builders move to a shared pure
module (`story/effects_fx.py`) that both `story/render.py` and `narration/mix.py`
call. SFX inputs come from `sfx_library` (kaching, death, whoosh). An effects
lane on the timeline creates them by dragging a span and picking a kind.
Length-changing edits (trim, extend, slow-mo) remain story requests (laptop)
behind an explicit "this will shift takes" confirmation. Not in this build.

### 4.9 Data model (models.py, additive)

```python
class Take(BaseModel):            # v1 fields unchanged
    media_in_s: float = 0.0       # ge 0
    offset_s: float = 0.0         # -3..3 (nudge)
    gain: float = 1.0             # gt 0, le 4
    muted: bool = False
    @property start_effective_s = start_s + offset_s

class GameAudioRegion(BaseModel): id, start_s (ge 0), end_s (gt start), level (ge 0, le 2)
class CameraRegion(BaseModel):    id, start_s, end_s, box: CameraBox
class AudioMix(BaseModel):        + game_gain: float = 1.0 (ge 0, le 2)
class NarrationSession(BaseModel):
    + game_regions: list[GameAudioRegion] = []
    + camera_regions: list[CameraRegion] = []
    + pinned_intro_s: float | None = None
class TakeEdit(BaseModel):        # PATCH body, all optional
    start_s, duration_s, media_in_s, offset_s, gain, muted
```

Old `session.json` files load unchanged (every new field has a default).

### 4.10 HTTP API (additive, prefix `/api/v1/narration/{kind}/{key}`)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/takes?…&replace=true` | webm (v1) | `Take`; with `replace` the overlapped takes are punched |
| PATCH | `/takes/{id}` | `TakeEdit` | `SessionView`; 404 unknown, 422 invalid (too short, offset out of range) |
| POST | `/takes/{id}/split?at_s=` | — | `SessionView`; 422 when `at_s` is not strictly inside the take |
| PUT | `/mix` | `AudioMix` | `SessionView` |
| PUT | `/game-regions` | `list[GameAudioRegion]` | `SessionView`; 422 on overlap |
| PUT | `/camera-regions` | `list[CameraRegion]` | `SessionView`; 422 on overlap (phase 2) |
| POST | `/relock` | `{shift_s: 0}` | `SessionView` |
| GET | `/frames` | — | `FrameStrip{sprite_url, every_s, tile_w, tile_h, cols, count}` (renders on first call) |
| GET | `/peaks` | — | `{rate_hz, peaks}` (computed on first call) |
| GET | `/markers` | — | `list[Marker{at_s, label, kind}]` |

`NarrationTarget` gains optional `markers(key)` and `intro_s(key)` (default
`[]` / `None` through a base mixin, so existing targets need no edit). The
store gains `cache_dir(ref)` and `cues_path(ref)`.

MCP (policy: every mutating endpoint has a tool; all classified in
`registry.NON_EFFECT_TOOLS`): `narration_session`, `narration_edit_take`,
`narration_split_take`, `narration_delete_take`, `narration_set_mix`,
`narration_set_game_regions`, `narration_set_camera_regions`,
`narration_relock`, `narration_export`. Story: `story_join` gains `hook_mode`.

### 4.11 Web module map (one job per file)

| File | Job |
|---|---|
| `lib/timeline.ts` | pure: px↔s, zoom around a point, clamp scroll, tick spacing |
| `lib/takeEdits.ts` | pure: trim/split/nudge/punch-in math mirroring the API (optimistic UI) |
| `lib/waveform.ts` | pure: peaks from `Float32Array` channel data for a given px width |
| `lib/mixSchedule.ts` | pure: which takes play at t, their buffer offset and when-time; region level at t |
| `hooks/useFrameStrip.ts`, `usePeaks.ts`, `useMarkers.ts` | TanStack queries |
| `hooks/useTakeBuffers.ts` | fetch + decode each take's webm once (`Map<media, AudioBuffer>`) |
| `hooks/useMixPreview.ts` | the Web Audio graph (4.4) |
| `hooks/usePlayhead.ts` | rAF clock |
| `components/narration/timeline/Timeline.tsx` | the scroll/zoom container, ruler, playhead, lane stack |
| `…/FrameStripLane.tsx`, `TakesLane.tsx`, `TakeBlock.tsx`, `GameAudioLane.tsx`, `MarkersLane.tsx`, `CameraLane.tsx` | one lane each |
| `components/narration/TakeInspector.tsx` | selected take: gain, mute, split, nudge readout, delete |
| `components/narration/MixPanel.tsx` | voice, game, duck sliders |
| `components/narration/ExportPanel.tsx` | + "shift takes by Δ and relock" |

Vitest is added (dev dependency) for `lib/*.ts` only; components stay untested
(no DOM test runner yet). `bun run test` runs it; `tsc -b` stays the gate.

### 4.12 Phasing

- **Phase 1 (this video)**: 4.1, 4.2, 4.3 (all four lanes), 4.4, 4.5, 4.7, the
  API/MCP in 4.10 minus camera regions, vitest.
- **Phase 2**: 4.6 camera regions + camera lane.
- **Phase 3**: 4.8 effects lane (new spec section when picked up).

## 5. Security and privacy

- Takes, caches (sprite, peaks), cues and `narrated.mp4` live under the
  narration folder on this PC; nothing in this spec uploads anywhere.
- All ffmpeg argv is built from validated Pydantic values (bounded floats,
  whitelisted kinds); no free text reaches a filter string. `cues.json` labels
  are displayed only, never rendered by ffmpeg.
- Region ids and take ids match `KEY_PATTERN`; media names are regex-checked
  (v1).
- The 500 MB upload cap stays; the sprite and peaks endpoints are idempotent
  per fingerprint and never run concurrently for the same key (a lock file).

## 6. Testing

- API: pure tests for `punch_in`, trim/split math, `camera_windows`, the
  mix argv (atrim/volume/region chains), `shift` relock, sprite/peaks argv;
  router tests for PATCH/split/PUT mix/regions/relock-shift with the fake
  target (as `test_narration_router.py`); hook `play` window tests in
  `test_story_join*`.
- Web: vitest for `lib/timeline.ts`, `lib/takeEdits.ts`, `lib/waveform.ts`,
  `lib/mixSchedule.ts`; `tsc -b` for everything.
- Manual: narrate 30 s of this video on the PC, punch-in over a beat, export,
  listen for alignment.

## Decisions Log

| ID | Topic | Decision | Rationale | Source | Date |
|---|---|---|---|---|---|
| D1 | Scope | Narrate this video soon: hook, take pieces + fixes, per-take volume, visible timeline first; the rest after it ships | The video is this weekend's deliverable | Interview | 2026-11-27 |
| D2 | Hook | Montage of 3-4 best plays played through their outcomes, swipes between, voice over it with the stat and one credential sentence | A tease with no proof convinces nobody; a montage with outcomes is proof; one spoken credential is not arrogant for a guide | Interview | 2026-11-27 |
| D3 | Hook text | Opening line only; no stat pop, no per-clip teasers, no op.gg card | Keep it clean; the Riot ID must not appear on screen | Interview | 2026-11-27 |
| D4 | Take model | Punch-in: pieces + non-destructive trim + record-over replaces overlapped takes; no pause button | Keeps v1's sync rule; matches DAW habits; avoids seek-while-paused alignment bugs | Interview | 2026-11-27 |
| D5 | Take edits | Split at playhead, nudge capped at ±3 s with the offset shown, mute | Cut a bad middle, line up with the action, A/B two versions | Interview | 2026-11-27 |
| D6 | Lanes | Frame strip, takes with waveforms, game audio, markers (chapters + cues) | The owner wants to see what happens where and voice vs game | Interview | 2026-11-27 |
| D7 | Preview | Full mix in the browser via Web Audio incl. ducking | What you hear is what exports; export re-encodes 13 min otherwise | Interview | 2026-11-27 |
| D8 | Game audio | Regions on the game lane with a level each + one global duck slider | "Softer here, louder there" without per-take coupling | Interview | 2026-11-27 |
| D9 | Camera over time | Regions with a hard switch; default box outside regions; phase 2 | One overlay per window in ffmpeg; smooth tweening costs per-frame expressions | Interview | 2026-11-27 |
| D10 | Timeline effects | Length-preserving effects + SFX applied in the narration export on the PC (session, joined seconds); length-changing edits stay story requests on the laptop; phase 3 | Keeps takes aligned, no laptop round-trip, fits the machine split | Interview (Claude default, owner left) | 2026-11-27 |
| D11 | Staleness | Pin `intro_s`; offer "shift takes by Δ and relock" | A longer hook is the one expected pre-game length change | Interview (Claude default) | 2026-11-27 |
| D12 | Testing | pytest pure + router tests; vitest for pure web libs only | No DOM runner today; pure math is where the bugs would be | Interview (Claude default) | 2026-11-27 |
| D13 | Red team | Skipped | Owner asked for no more questions | Interview | 2026-11-27 |
| D14 | Branching | Work on `feat/narration-v2`, push, merge only when the owner says | Root CLAUDE.md branch rule (bc5b421) | Interview | 2026-11-27 |

## Dependency Graph & Implementation Order

```
T1 models + pure take math (punch_in, trim, split, shift)      ─┐
T2 mix.py v2 (atrim/gain/regions/camera windows) ←T1            ├─ wave 1 (API)
T3 service + router + MCP (PATCH, split, mix, regions, relock)  ←T1,T2
T4 frames / peaks / markers endpoints + target mixin + store cache ←T1
T5 hook play mode (bookends, join CLI/API/MCP)                   (independent)
T6 web libs (timeline, takeEdits, waveform, mixSchedule) + vitest (independent)
T7 web data hooks + types/api (frames, peaks, markers, mutations) ←T3,T4,T6
T8 timeline lanes + inspector + mix panel + relock shift          ←T7
T9 mix preview (Web Audio)                                        ←T7
T10 docs (web/docs narration.md, api/docs, knowledge Narration.md) + arsenal regen ←all
```

Waves: W1 = T1, T5, T6 in parallel → W2 = T2, T4 → W3 = T3, T7 → W4 = T8, T9 → T10.
Phase 2 (camera regions) is T11/T12 after W4, same shape as T2/T3 + a lane.
