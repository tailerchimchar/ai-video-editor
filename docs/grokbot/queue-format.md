# Queue format

Reference for Lead and Editor. Every item is one JSON file in the `queue/` folder of the queue repo, named after its `id` (for example the id followed by `.json`). The PC runner validates each file as hostile input. A refused item gets a note with the reason in `rejected/` (the original file stays in `queue/`) and a `queue.rejected` event. Every example below is parsed by a test, so copy the shape exactly.

## Envelope (every kind)

| field | rule |
|---|---|
| `id` | a fresh uuid4 string, never reused |
| `kind` | `edit_plan`, `preview_request`, `verdict`, `deploy`, `sleep` or `wake` |
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

## Digest (read only, written by the PC)

Not a queue item: the digest is what the PC publishes to Drive `digests/` after each panel round, and the only view of a round the Editor gets. All values below are invented. Per clip:

- `events`: Riot timeline events inside the clip window, `t_in_clip` in seconds from the clip start; actors are champion plus side (`you (Jhin)`, `ally Thresh`, `enemy Diana`). Types: `kill`, `death`, `assist`, `ally_death`, `enemy_death`, `building`, `elite`, `game_end`. Empty when no timeline matched or confidence is low.
- `game_state`: game clock at the clip start (negative when the clip starts before the game clock does), your gold and CS near the clip start and end, your level at the end, and whether the game ended inside the clip. Null when there is no timeline or the match confidence is `low`.
- `match_confidence`: `high`, `medium`, `low` or `none`. Treat `low` as a guess and never name an objective, a location or a game result from a `low` or `none` clip.
- `voice`: one sentence about what the player says or reacts to, written by a model from the speech in the window (or the analyst's first story beat when there is no speech). Never a quote; paraphrase it again in your own words.
- `energy`: `hype_rising`, `laughter`, `calm` or `unknown` (`panic` is reserved and not produced yet).

```json digest
{
  "game": "league",
  "round": "2026-11-22_2140_jhin_8-3-11",
  "built_at": "2026-11-22T23:05:00Z",
  "compilation_id": "comp-0042",
  "compilation_version": "3",
  "asset_id": "asset-0007",
  "clips": [
    {
      "ref": "24m10s",
      "event": "double kill",
      "start_s": 1438.0,
      "end_s": 1462.5,
      "panel": {"gaming_quality": 4, "hook_verdict": "pass", "format": "hype_montage", "include_p": 0.82},
      "critic": "ship_it",
      "voice": "The player counts the shots out loud and cheers when the fourth one lands.",
      "copy": {"title": "Four shots, two down", "caption": "Patience pays", "hashtags": ["leagueoflegends", "adc"], "thumbnail_t": 9.0},
      "drive": "1AbCdEfGhIjKlMnOp",
      "events": [
        {"type": "kill", "t_in_clip": 8.5, "actors": ["you (Jhin)", "enemy Diana"]},
        {"type": "kill", "t_in_clip": 12.0, "actors": ["you (Jhin)", "enemy Lee Sin"]},
        {"type": "elite", "t_in_clip": 19.5, "actors": ["ally Thresh"]}
      ],
      "game_state": {
        "game_time_s": 1402.0,
        "gold_start": 9150,
        "gold_end": 9930,
        "cs_start": 182,
        "cs_end": 190,
        "level": 13,
        "game_ended_in_clip": false
      },
      "energy": "laughter",
      "match_confidence": "high",
      "play_id": null
    }
  ]
}
```

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

## Day-one note

On day one the runner only acts on `preview_request`, `sleep` and `wake`. `edit_plan`, `verdict` and `deploy` are validated and held: the runner records them as deferred and sends `queue.deferred`, and nothing is written to the decisions log or the mentor. When Noodlz later turns on plans or verdicts, held items younger than 24 hours run then; older ones are marked expired (one `queue.expired` event with the count) and never run, so resubmit anything still wanted with a new id. A `queue.rejected` event tells you why a file was refused; fix the cause and write a new item with a new id.
