# Narration timeline (`components/narration/timeline/`)

The zoomable timeline under the video on `/narrate/:kind/:key`
([`narration.md`](./narration.md)). Spec:
[`spec-narration-v2.md`](../../../docs/specs/spec-narration-v2.md) §4.3.

```
ruler      0:00 ........ 2:00 ........ 4:00 ........      [−] [+] [fit]
frames     [▒▒▒][▒▒▒][▒▒▒][▒▒▒][▒▒▒][▒▒▒][▒▒▒][▒▒▒]
markers    |Xayah 5/0/2 · The opening   |subscribe   |Yunara …     (top row)
           |X1 The opening starts …  |X1 we…  |K1 …              (cues)
takes      ▐▔▔waveform▔▔▌  ▐▔▔▌   ▐▔▔▔▔▔▔▔▔▌   (grey = muted)
game       ▁▂▃▅▆▅▃▂▁▂▃▅ with dips under the takes, [25%] regions
                     ▲ playhead
```

## Lanes

| Lane    | Shows                                                                                                                                                                                                                                | Click / drag                                        |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------- |
| ruler   | a labelled tick every 1–300 s (≥ 80 px apart), minor ticks between                                                                                                                                                                   | click or drag = seek / scrub                        |
| frames  | one video frame per `every_s`; zoomed out only every n-th frame, zoomed in a frame repeats across its span                                                                                                                           | click = seek                                        |
| markers | top row: game starts (accent, plus a hairline through every lane), chapters, inserts (dashed); bottom row: cues. Markers at the same second merge into one label; a label is cut where the next one starts, full text in the tooltip | click a marker = seek to it; click elsewhere = seek |
| takes   | one block per take at its effective start (`start_s + offset_s`): waveform, blue border, green border = has camera, grey = muted, white outline = selected                                                                           | see below                                           |
| game    | the video's own audio as the mix will play it: faint = original, solid = after game level, regions and the duck dip; blue band = a take is speaking; amber = level regions                                                           | see below                                           |

## Zoom and pan

- **wheel** pans (vertical or horizontal wheel), **ctrl/⌘ + wheel** zooms
  around the cursor; the page does not scroll while the pointer is over the
  lanes.
- **−** / **+** zoom around the playhead (or the middle when it is out of
  view); **fit** shows the whole video and follows the panel width.
- While the video plays, the view pages forward once the playhead passes 90%
  of the width.
- Zoom range: whole video … 400 px per second.

## Takes

- **click** = select, seek to its start and play it with the video.
- **drag the body** = nudge: the label shows the change (`+0.4 s`); one
  `PATCH .../takes/{id}` with `offset_s` on release. Limited to ±3 s and to
  not starting before 0:00.
- **drag an edge of the selected take** = trim: the label shows the new
  length; one `PATCH` with `start_s, media_in_s, duration_s` on release.
  Dragging back out restores trimmed audio, up to the end of the decoded
  recording. Minimum 0.3 s.
- While dragging, the block is a local draft; nothing is sent until release.
  The edit then shows at once (optimistic) and rolls back if the API refuses.
- Gain, mute, split at the playhead, nudge reset and delete live in the
  **take** panel on the right (`TakeInspector`).
- While recording the lanes ignore the pointer.

## Game-audio regions

- **drag on empty lane** = new region at 50%; it stops at its neighbours so
  regions never overlap.
- **drag a region edge** = resize (between its neighbours and the video,
  minimum 0.2 s).
- **click a region** = select it and step its level 100% → 50% → 25% → 0% →
  100% (other levels step to the next one below).
- **Delete / Backspace** removes the selected region (not while typing in a
  field).
- Every change sends the whole list (`PUT .../game-regions`), optimistic.

## Data sources

| Data           | From                                                                                                                                                                                                                        |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| frames         | `GET .../frames` → `{sprite_url, every_s, tile_w, tile_h, cols, count}`; the API renders the JPEG sprite once per video (first load takes a few seconds: "rendering frames…")                                               |
| game peaks     | `GET .../peaks` → `{rate_hz, peaks}` once per video; drawn normalised to the loudest moment                                                                                                                                 |
| markers        | `SessionView.markers`: the target's chapters / game starts / inserts plus `<narration>/cues.json`                                                                                                                           |
| take waveforms | each take's `.webm` fetched and decoded in the browser (`useTakeBuffers`) with the page's `AudioContext`, which the browser only allows after the first click or key press; reduced to a 200 Hz envelope once per recording |

## Code map

| File                 | Job                                                                                                 |
| -------------------- | --------------------------------------------------------------------------------------------------- |
| `Timeline.tsx`       | viewport state (fit / zoom / scroll), wheel, toolbar, follow-the-playhead, label gutter, lane stack |
| `Ruler.tsx`          | ticks + labels, scrub                                                                               |
| `FrameStripLane.tsx` | sprite tiles in view                                                                                |
| `MarkersLane.tsx`    | two marker rows, merged labels                                                                      |
| `TakesLane.tsx`      | take blocks, waveform envelopes per recording                                                       |
| `TakeBlock.tsx`      | one take: canvas waveform (visible part only), nudge / trim drag                                    |
| `GameAudioLane.tsx`  | canvas peaks as mixed, regions and their drags, Delete key                                          |
| `Playhead.tsx`       | the playhead line                                                                                   |
| `lane.ts`            | lane heights, pointer x, canvas sizing / mirror bars                                                |

Pure maths lives in `src/lib/` (`timeline.ts`, `takeEdits.ts`, `waveform.ts`,
`mixSchedule.ts`, all unit-tested); the lanes only draw a `Viewport` and call
back.
