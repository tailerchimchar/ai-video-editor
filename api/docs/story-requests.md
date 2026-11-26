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
| `effect` | `effect` (`letterbox`, `slowmo`, `zoom`, `focus`), `at`, `start?`, `end?`, `params={}` | Effect on the chapter containing `at`; range defaults to the whole chapter and is clamped to it. |

Order of application: drops, adds, extends, effects. A request that matches
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
