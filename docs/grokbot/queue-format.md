# Queue format

Reference for Lead and Editor. Every item is one JSON file in the `queue/` folder of the queue repo, named after its `id` (for example the id followed by `.json`). The PC runner validates each file as hostile input. A refused item gets a note with the reason in `rejected/` (the original file stays in `queue/`) and a `queue.rejected` event. Every example below is parsed by a test, so copy the shape exactly.

## Envelope (every kind)

| field | rule |
|---|---|
| `id` | a fresh uuid4 string, never reused |
| `kind` | `edit_plan`, `preview_request`, `verdict`, `deploy`, `sleep`, `wake` or `render_request` |
| `author_bot` | `lead`, `editor` or `devops` (the Viewer has no queue access) |
| `created` | ISO 8601 UTC timestamp |
| `schema_version` | the number 1 |

Unknown fields are rejected. A file over 64 KB is rejected.

## Hostile-string rules (apply to every string, keys included)

A string is rejected if it contains any of these:

- a web address (the letters http or https followed by a colon and two slashes)
- a backslash
- two slashes in a row
- a dot followed by a slash
- a slash directly after anything that is not a letter, digit, hyphen or underscore (so "and/or" is fine, a path is not)
- any of the characters semicolon, pipe, ampersand, less-than, greater-than
- a backtick
- the two characters dollar sign and open parenthesis

So: no links, no file paths, no shell-looking text. Refer to things by id, by clip reference, or in plain words. Put any link in a text message to Noodlz, never in a queue file.

## edit_plan

The Editor writes it. `compilation_id` and `base_version` come from the digest's `compilation_id` and `compilation_version` (or the `round.published` event), or from the `current` field of a `queue.rejected` event; if `base_version` is stale the runner rejects the plan and sends the current state back.

- `clip_ref` is the clip's 1-based number in the compilation as it was at `base_version`, written as a string: `"1"`, `"2"`. Never a digest ref, never a made-up name.
- Effect kinds the runner renders today: `zoom` (params `factor`, `roi`), `focus` (params `x`, `y`, `radius`, `dim`) and `caption` (param `text`, a paraphrase in your own words, never a quoted line). Any other kind is refused; put it under `rejected` instead.
- Every effect must have `at` equal to 0.0 (timed effects are not applied yet), and `cards` and `sfx` must stay empty.
- `music` is null or `{"song_id": "<library id>"}`: letters, digits, hyphen and underscore only, never a file name.
- `copy` is the Viewer's block, handed over by the Lead.

```json
{
  "id": "0b6f1c9e-3d52-4c1a-9e57-6a1d2f8b7c10",
  "kind": "edit_plan",
  "author_bot": "editor",
  "created": "2026-11-21T01:15:00Z",
  "schema_version": 1,
  "compilation_id": "comp-0042",
  "base_version": "14",
  "plan": {
    "format": "hype_montage",
    "source_clips": [
      {"clip_ref": "1", "start": 3.0, "end": 9.5, "intensity": 0.8},
      {"clip_ref": "2", "start": 0.0, "end": 9.0, "intensity": 0.6}
    ],
    "order": ["1", "2"],
    "music": null,
    "effects": [
      {"clip_ref": "1", "kind": "zoom", "at": 0.0, "params": {"factor": 1.3, "roi": "center"}},
      {"clip_ref": "2", "kind": "caption", "at": 0.0, "params": {"text": "one flash, two kills"}}
    ],
    "cards": [],
    "sfx": [],
    "rejected": [
      {"wanted": "slow motion on the last kill", "why": "not renderable yet"}
    ]
  },
  "copy": {
    "title": "He never saw it coming",
    "caption": "Quiet lane, loud finish",
    "hashtags": ["leagueoflegends", "adc"],
    "thumbnail_t": 6.5
  }
}
```

## preview_request

Lead or Editor. Use `asset_id`: it comes from the digest's `asset_id` field (or the `round.published` event); never guess one. Previews of a `compilation_id` window are held, not rendered, for now. The window must satisfy 0 <= `start_s` < `end_s` and be at most 20 seconds, in seconds of the recording (a digest clip's `start_s`/`end_s` are in the same seconds). `question` is what you want the preview to answer.

```json
{
  "id": "5d0a7e24-8b3f-4f6e-a1c2-93e4b7d05a11",
  "kind": "preview_request",
  "author_bot": "editor",
  "created": "2026-11-21T01:20:00Z",
  "schema_version": 1,
  "asset_id": "asset-0007",
  "start_s": 120.0,
  "end_s": 135.0,
  "question": "Does the opening beat land without the caption?"
}
```

## verdict

Lead only, and only to record what Noodlz himself said. `game` is `league` or `valorant` exactly. `clip_ref` is the digest ref of the clip (for example `21m05s`). `quote` is his text verbatim and `quote_ts` is when he sent it. Once verdict recording is on, a verdict with an empty quote goes to the decisions log only and never reaches the mentor. `verdict` is `payoff`, `false_positive` or `style`. Never put a bot default or a bot opinion in `quote`.

```json
{
  "id": "a3c91f07-52de-4b8a-8c40-1e7d6b92f3a2",
  "kind": "verdict",
  "author_bot": "lead",
  "created": "2026-11-21T02:00:00Z",
  "schema_version": 1,
  "game": "league",
  "clip_ref": "21m05s",
  "verdict": "false_positive",
  "rule": "A lone kill on a minion wave is not a highlight",
  "quote": "nah that one is nothing",
  "quote_ts": "2026-11-21T01:58:30Z"
}
```

## deploy

Written by the Lead on DevOps's request (DevOps has no queue token). `tag` is a release tag of the form v followed by three dot-separated numbers. The runner only acts when Noodlz or the Claude session approved it.

```json
{
  "id": "c8e2b5f1-7a46-4d03-b9e8-2f5a01d6c3b4",
  "kind": "deploy",
  "author_bot": "lead",
  "created": "2026-11-21T03:00:00Z",
  "schema_version": 1,
  "tag": "v0.3.2"
}
```

## sleep and wake

Lead only. No payload. Commit `sleep` when Noodlz texts sleep or the usage pool falls under his floor; commit `wake` when he texts wake.

```json
{
  "id": "e1f4a7b2-90c3-4e58-a6d1-47b8c2e90f35",
  "kind": "sleep",
  "author_bot": "lead",
  "created": "2026-11-21T04:00:00Z",
  "schema_version": 1
}
```

```json
{
  "id": "f7b3d9a0-15e8-4c6b-8d24-b0a96e1c7d58",
  "kind": "wake",
  "author_bot": "lead",
  "created": "2026-11-21T12:00:00Z",
  "schema_version": 1
}
```

## render_request

Editor only. Triggers a box render job on the Linux render box. The box downloads clips by their Drive file ids, renders both 9:16 and 16:9 outputs from the same edit plan, uploads results to the Drive "finished" folder, and emits `video.rendered`. No LLM or vision calls: pure ffmpeg render, deterministic.

- `asset_id`: the recording's asset id from the digest
- `edit_plan_id`: unique id for this edit plan
- `edit_plan_version`: version string for idempotency
- `game`: `league` or `valorant` exactly (determines 9:16 HUD layout)
- `clips`: 1–20 clips, each with `clip_ref` (1-based string), `drive_id` (Drive file id: letters/digits/hyphen/underscore, 10–128 chars, from `round.published`), `start_s`, `end_s` with `0 <= start_s < end_s` and window ≤ 180 s
- `concat_order`: 1–20 `clip_ref` values in render order
- `music_song_id`: optional library id (letters, digits, hyphen, underscore only; never a path)
- The PC runner validates and defers these items (`queue.deferred`). The Linux box runs them via `uv run box-render <queue-item.json>` with `GROKBOT_BOX_RENDER_ENABLED=true`.

```json
{
  "id": "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f8a",
  "kind": "render_request",
  "author_bot": "editor",
  "created": "2026-11-21T02:30:00Z",
  "schema_version": 1,
  "asset_id": "asset-dryrun-0001",
  "edit_plan_id": "plan-0001",
  "edit_plan_version": "1",
  "game": "league",
  "clips": [
    {"clip_ref": "1", "drive_id": "FAKE-drive-clip-0001", "start_s": 3.0, "end_s": 9.5},
    {"clip_ref": "2", "drive_id": "FAKE-drive-clip-0002", "start_s": 0.0, "end_s": 9.0}
  ],
  "concat_order": ["1", "2"],
  "music_song_id": null
}
```

---

## Events you receive

Events are webhook POSTs to the Lead. Each has `{"event", "payload", "ts"}`.

### video.rendered

Emitted by the box after a successful render. Both aspect ratios are always present.

| field | type | meaning |
|---|---|---|
| `edit_plan_id` | string | the render_request's edit_plan_id |
| `edit_plan_version` | string | the render_request's edit_plan_version |
| `asset_id` | string | the recording's asset id |
| `outputs` | list | one entry per aspect ratio |
| `outputs[].aspect` | string | `"9:16"` or `"16:9"` |
| `outputs[].drive_id` | string | Drive file id of the uploaded mp4 |
| `outputs[].duration_seconds` | float | rendered video duration |
| `outputs[].file_size_bytes` | int | file size |
| `timings` | object | `download_seconds`, `render_seconds`, `upload_seconds`, `total_seconds` |

```json
{
  "event": "video.rendered",
  "payload": {
    "edit_plan_id": "plan-0001",
    "edit_plan_version": "1",
    "asset_id": "asset-dryrun-0001",
    "outputs": [
      {"aspect": "9:16", "drive_id": "FAKE-drive-out-9x16", "duration_seconds": 18.5, "file_size_bytes": 4200000},
      {"aspect": "16:9", "drive_id": "FAKE-drive-out-16x9", "duration_seconds": 18.5, "file_size_bytes": 5100000}
    ],
    "timings": {"download_seconds": 12.3, "render_seconds": 45.6, "upload_seconds": 8.9, "total_seconds": 67.8}
  },
  "ts": "2026-11-21T02:35:00+00:00"
}
```

### render.failed

Emitted when a box render fails at any stage.

| field | type | meaning |
|---|---|---|
| `edit_plan_id` | string | the render_request's edit_plan_id |
| `edit_plan_version` | string | the render_request's edit_plan_version |
| `asset_id` | string | the recording's asset id |
| `stage` | string | `"busy"`, `"download"`, `"render"`, `"upload"`, or `"internal"` |
| `error` | string | human-readable error message |

```json
{
  "event": "render.failed",
  "payload": {
    "edit_plan_id": "plan-0001",
    "edit_plan_version": "1",
    "asset_id": "asset-dryrun-0001",
    "stage": "download",
    "error": "Drive download failed: clip 0: HTTP 404"
  },
  "ts": "2026-11-21T02:31:00+00:00"
}
```

### Approve/reject flow

After receiving `video.rendered`, the Lead shows both outputs to Noodlz for approval. Noodlz can:

1. **Approve**: The Lead marks the render as approved and proceeds to publish. No queue item needed: the Lead tracks approval state internally.

2. **Reject with feedback**: Noodlz texts what's wrong. The Lead records a `verdict` item (kind `style`) with the feedback, then the Editor can submit a new `render_request` with a fresh `edit_plan_id`.

The box render scratch files are auto-deleted only after successful upload. On failure, scratch files are kept for debugging (the owner explicitly approved this policy).

---

## Day-one note

On day one the runner only acts on `preview_request`, `sleep` and `wake`. `edit_plan`, `verdict`, `deploy` and `render_request` are validated and held: the runner records them as deferred and sends `queue.deferred`, and nothing is written to the decisions log or the mentor. When Noodlz later turns on plans or verdicts, held items younger than 24 hours run then; older ones are marked expired (one `queue.expired` event with the count) and never run, so resubmit anything still wanted with a new id. A `queue.rejected` event tells you why a file was refused; fix the cause and write a new item with a new id.
