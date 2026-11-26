# Narration recorder (`/narrate/:kind/:key`)

Talk (and optionally show your camera) over a finished video, then export
`narrated.mp4` with your voice mixed in and the game audio ducked under it.
Spec: [`docs/specs/spec-narration.md`](../../../docs/specs/spec-narration.md).

First kind is `story`, key = the round folder, e.g.
`/narrate/story/2026-11-23_2056_zeri_8-7-12`.

## How to use

1. Open the page; allow the microphone (and camera, if you turn it on).
2. **Wear headphones.** The video's sound plays while you record; speakers
   would bleed into the mic and end up in the take twice.
3. Seek to where you want to start and press **Record**. The video plays and
   the mic records. **Stop** pauses and saves the take, pinned to the second
   it started at. Pausing or seeking the video also ends the take (a seek
   ends it at the pre-seek time so it stays aligned).
4. Takes appear as blocks on the timeline under the video (green = has
   camera). Click a block to seek there and hear the take with the video;
   click the empty track to seek; **delete** removes a take.
5. **Record camera** adds the webcam to the next takes. The camera box over
   the video shows the live preview: drag it to move, drag its top-left
   corner to resize (aspect kept). It saves itself. **Show camera in export**
   decides whether the box appears in `narrated.mp4`; **reset box** returns
   to the game's default spot (League: above the minimap).
6. **Export narrated.mp4** starts a job; a link appears when it finishes.

If the video was re-rendered after you recorded, the page shows **stale**:
**relock takes** keeps them as they are, **delete all takes** starts over.
Export is refused until you pick one.

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

| File                               | Job                                                                                         |
| ---------------------------------- | ------------------------------------------------------------------------------------------- |
| `src/types/narration.ts`           | mirrors `narration/models.py` + `SessionView`                                               |
| `src/api/narration.ts`             | the HTTP calls + take / `narrated.mp4` URL builders                                         |
| `src/lib/cameraBox.ts`             | pure box geometry (fractions, move, aspect-kept resize)                                     |
| `src/hooks/useMediaStream.ts`      | `getUserMedia` (mic + optional camera)                                                      |
| `src/hooks/useMediaDevices.ts`     | microphone / camera lists                                                                   |
| `src/hooks/useMediaRecorder.ts`    | `MediaRecorder` start / stop → WebM `Blob`                                                  |
| `src/hooks/useAudioLevel.ts`       | mic level for the meter                                                                     |
| `src/hooks/useVideoClock.ts`       | video time / duration / playing                                                             |
| `src/hooks/useTakeCapture.ts`      | the record/stop sync rule with the video                                                    |
| `src/hooks/useTakePlayback.ts`     | play a take in sync with the video                                                          |
| `src/hooks/useNarrationSession.ts` | TanStack query + mutations                                                                  |
| `src/hooks/useJob.ts`              | export job polling                                                                          |
| `src/components/narration/*`       | RecordControls, TakeTimeline, CameraBoxEditor, CameraSettings, ExportPanel, LiveStreamVideo |
| `src/pages/NarrationPage.tsx`      | composes the above; kind/key from the route                                                 |

Adding a game or a narratable kind needs no web change: the kind and key come
from the route, the video URL and camera box from the API.
