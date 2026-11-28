# Narration recorder (`/narrate/:kind/:key`)

Talk (and optionally show your camera) over a finished video in as many
takes as you like, edit them on a timeline, then export `narrated.mp4` with
your voice mixed in and the game audio ducked under it.
Specs: [`spec-narration.md`](../../../docs/specs/spec-narration.md) (v1),
[`spec-narration-v2.md`](../../../docs/specs/spec-narration-v2.md) (takes in
pieces, timeline, mix).

Kinds:

- `story`, key = the round folder, e.g.
  `/narrate/story/2026-11-23_2056_zeri_8-7-12`.
- `joined`, key = the story-join file name without `.mp4`, e.g.
  `/narrate/joined/joined_2026-11-26_xayah-kaisa-yunara`. Takes live in
  `WORKSPACE/story/_joined/<key>/narration/`.

## How to use

1. Open the page; allow the microphone (and camera, if you turn it on).
2. **Wear headphones.** The video's sound plays while you record; speakers
   would bleed into the mic and end up in the take twice.
3. **Record in pieces.** Seek to where you want to start and press **Record**.
   The video plays and the mic records. **Stop** pauses and saves the take,
   pinned to the second it started at. Pausing or seeking the video also ends
   the take (a seek ends it at the pre-seek time so it stays aligned).
4. **Record over a mistake.** Recording over existing takes replaces what the
   new take covers: older takes under it are trimmed, split around it, or
   removed. Nothing outside the new take changes.
5. **Edit on the timeline** under the video (details in
   [`timeline.md`](./timeline.md)):
   - click a take = select it, seek there and hear it with the video;
   - drag a take = nudge it (±3 s, for lip-sync / timing), the label shows
     the change;
   - drag the edges of the selected take = trim it (dragging back out
     restores trimmed audio, up to the end of the recording);
   - the **take** panel on the right: gain, **mute** (a muted take is grey
     and left out of the export), **split at playhead** (playhead inside the
     take), the nudge with **reset**, **delete**.
6. **Shape the game audio** on the game lane: drag on empty space to draw a
   region (starts at 50%), click it to step 100% → 50% → 25% → 0%, drag its
   edges to resize, **Delete** removes the selected one. Regions never
   overlap (a new one stops at its neighbours).
7. **Mix** panel: **voice** (your level), **game** (the game's level outside
   regions), **duck** (how hard the game drops while you talk; 1 = off). The
   game lane draws the result: quieter inside regions, a dip (blue band)
   under every unmuted take. Changes save 300 ms after you stop moving.
8. **Record camera** adds the webcam to the next takes. The camera box over
   the video shows the live preview: drag it to move, drag its top-left
   corner to resize (aspect kept). It saves itself. **Show camera in export**
   decides whether the box appears in `narrated.mp4`; **reset box** returns
   to the game's default spot (League: above the minimap).
9. **Export narrated.mp4** starts a job; a link appears when it finishes.

If the video was re-rendered after you recorded, the page shows **stale**.
When the API knows how much longer the video got before the games (a new
hook), **shift takes by Δ s and relock** moves every take and region by that
much and relocks. **relock takes** keeps them where they are, **delete all
takes** starts over. Export is refused until you pick one.

## Editor suggestions (story videos only)

On a `story` video the sidebar shows **editor suggestions**. **Get editor
suggestions** starts the panel job (`POST /api/v1/story/{round}/suggestions`);
the panel polls until the set is `ready`, then lists 3-6 rows: effect kind,
game time, chapter, reason, play score, pop %. Ticking a row calls
`PUT .../suggestions/{id}`. **Apply & re-render** writes the ticked rows as
requests and starts the story render job; when it completes the session, the
videos list and the set refetch, so takes recorded before show the stale
banner. States: none / pending / ready / applied / failed.

There is no "jump there" button: suggestions carry game-clock seconds only, and
the API does not send where each chapter sits in the cut video, so a seek could
not be made reliable. Rows show the game time and chapter number instead.
Sending each chapter's video start from the API would allow the button later.

Files: `types/suggestions.ts`, `api/suggestions.ts` (key
`["suggestions", round]`), `hooks/useSuggestions.ts`,
`components/narration/SuggestionsPanel.tsx`. Backend:
`api/docs/story-suggestions.md`.

## Privacy

Takes (voice and camera) are uploaded only to the local API on this PC and
stored next to the video under `narration/takes/`. Nothing is sent to Drive,
Supabase or any other service.

## Code map (one job per file)

| File                                         | Job                                                                                      |
| -------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `src/types/narration.ts`                     | mirrors `narration/models.py` + `SessionView`                                            |
| `src/api/narration.ts`                       | the HTTP calls + take / `narrated.mp4` URL builders                                      |
| `src/lib/cameraBox.ts`                       | pure box geometry (fractions, move, aspect-kept resize)                                  |
| `src/lib/timeline.ts`                        | pure viewport maths: px ↔ s, zoom around a point, ticks                                  |
| `src/lib/takeEdits.ts`                       | pure trim / split / nudge / punch-in, mirroring the API                                  |
| `src/lib/waveform.ts`                        | pure peaks: envelope of a decoded take, peaks per pixel column                           |
| `src/lib/mixSchedule.ts`                     | pure: which takes play when, region level at a time                                      |
| `src/lib/regions.ts`                         | pure: clamp game-audio regions to the video's end                                        |
| `src/hooks/useMediaStream.ts`                | `getUserMedia` (mic + optional camera)                                                   |
| `src/hooks/useMediaDevices.ts`               | microphone / camera lists                                                                |
| `src/hooks/useMediaRecorder.ts`              | `MediaRecorder` start / stop → WebM `Blob`                                               |
| `src/hooks/useAudioLevel.ts`                 | mic level for the meter                                                                  |
| `src/hooks/useVideoClock.ts`                 | video time / duration / playing                                                          |
| `src/hooks/useVideoDuration.ts`              | video length only (the page does not re-render while playing)                            |
| `src/hooks/usePlayhead.ts`                   | rAF video time for the playhead                                                          |
| `src/hooks/useTakeCapture.ts`                | the record/stop sync rule with the video                                                 |
| `src/hooks/useTakePlayback.ts`               | play a take in sync with the video                                                       |
| `src/hooks/useAudioContext.ts`               | the page's `AudioContext`, made on the first gesture                                     |
| `src/hooks/useTakeBuffers.ts`                | fetch + decode each take's media once                                                    |
| `src/hooks/useFrameStrip.ts`, `usePeaks.ts`  | frame sprite + game peaks queries                                                        |
| `src/hooks/useDebouncedDraft.ts`             | slider value shown at once, saved after a pause                                          |
| `src/hooks/useNarrationSession.ts`           | TanStack query + mutations (take edits and regions optimistic)                           |
| `src/hooks/useJob.ts`                        | export job polling                                                                       |
| `src/components/narration/timeline/*`        | the timeline: `Timeline` + one file per lane ([`timeline.md`](./timeline.md))            |
| `src/components/narration/TakeInspector.tsx` | the selected take: gain, mute, split, nudge, delete                                      |
| `src/components/narration/MixPanel.tsx`      | voice / game / duck sliders                                                              |
| `src/components/narration/*` (rest)          | RecordControls, CameraBoxEditor, CameraSettings, ExportPanel, SliderRow, LiveStreamVideo |
| `src/pages/NarrationPage.tsx`                | composes the above; kind/key from the route                                              |

Adding a game or a narratable kind needs no web change: the kind and key come
from the route, the video URL and camera box from the API.
