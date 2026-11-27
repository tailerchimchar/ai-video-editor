# Story requests

The owner's asks for a story live in `WORKSPACE/story/<round>/requests.json`.
`story-render` applies them on top of the automatic plan every time it
renders (skip with `--no-requests`). The render never writes the file, so a
re-render keeps every ask.

## Format

```json
{
  "requests": [
    {"kind": "add_chapter", "at": "20:05", "title": "Infinity Edge"},
    {"kind": "effect", "effect": "letterbox", "at": "34:30"}
  ]
}
```

Times are the **game clock** the owner sees: `"20:05"`, `"1:02:05"`, or
seconds as a number. Recording seconds = game clock + the timeline offset.

| kind | fields | does |
|---|---|---|
| `add_chapter` | `at`, `before_s=5`, `after_s=20`, `title?`, `note?` | New `request` chapter `[at-before, at+after]`. Overlapping existing chapters merge into the union and keep their title and kind. Default title `At <at>`. |
| `extend` | `at`, `before_s=0`, `after_s=0` | Grows the chapter containing `at`. |
| `drop` | `at` | Removes the chapter containing `at`. |
| `trim` | `at`, `start?`, `end?` (game clock) | Shrinks the chapter containing `at` to `start`..`end` (never grows it). Kill/death times outside go; effects, mic mutes and b-roll outside are dropped or clamped. Warns when no chapter or the window is empty. |
| `mute` | `at`, `start?`, `end?` (game clock) | Silences only the owner's mic over the range (default the whole chapter, clamped to it); game audio keeps playing. For off-game talk he will narrate over later. Several mutes add up. |
| `effect` | `effect` (`letterbox`, `slowmo`, `zoom`, `zoom_out`, `focus`), `at`, `start?`, `end?`, `params={}` | Effect on the chapter containing `at`; range defaults to the whole chapter and is clamped to it. `zoom_out` is the comic Isab-style zoom-out: the picture shrinks to `factor` (default 0.6, 0.2 to 1) inside black borders. |

Order of application: drops, adds, extends, trims, mutes, effects. A request that matches
no chapter prints a warning and is skipped. Request chapters are applied after
the time budget, so no budget drops them. An invalid file stops the render
(exit 2) and names the bad entry (`request #N`).

## Examples

"Show me walking out of base at 20:05" (offset -21.6 puts it at recording
1178.4 to 1203.4):

```json
{"kind": "add_chapter", "at": "20:05", "title": "Walking out of base"}
```

"Make the last teamfight cinematic":

```json
{"kind": "effect", "effect": "letterbox", "at": "34:30"}
```

"Cut the first 10 s of the chapter at 31:10" (it starts at 31:00):

```json
{"kind": "trim", "at": "31:10", "start": "31:10"}
```

"Mute my mic from 12:00 to 12:30, I was talking about dinner":

```json
{"kind": "mute", "at": "12:10", "start": "12:00", "end": "12:30"}
```
