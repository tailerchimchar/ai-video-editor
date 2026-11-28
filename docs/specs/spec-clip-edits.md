# Spec: clip edits from the narration timeline

**Status:** decided 2026-11-28 (interview + red team) · **Baseline:** `feat/narration-v2` @ 24f4004 · **Preview:** https://claude.ai/artifact/SXy8VfwPw2cMd8X91mmQm9 · **Playground (drag it):** https://claude.ai/artifact/M46eiu7XEc5XmSrAKYy6C3 · **Builds on:** [`spec-narration-v2.md`](./spec-narration-v2.md), [`spec-narration.md`](./spec-narration.md)

## 1. Problem

On the PC's narrate page (`/narrate/joined/<key>`) the owner can see where the hook clips and the chapters sit, but can only click them to seek. The hook of the current video ends at the wrong moment; the owner wants to drag the edge of each hook clip by +2 s the way Clipchamp extends a clip, and have the data follow.

What stands in the way today:

- Markers are points (`Marker{at_s,label,kind}`), not spans.
- The hook picks (`--hook 3:2 …`, mode, title) are not persisted anywhere. The join map records one `hook` insert of 13.8 s. Hook windows come from constants in `story/bookends.py` (`HOOK_LEAD_S 6`, `HOOK_PLAY_TAIL_S 2`, `HOOK_PLAY_MAX_S 14`) with no per-clip override, and are cut from the round's rendered `story.mp4`.
- Chapter windows can be changed only through `story/<round>/requests.json` (`extend` grows, `trim` shrinks, keyed by a game-clock time), then `story-render`, then `story-join`.
- No digest.json exists for the three current rounds; the story planner reads `plays/<asset>.json` (`impressive.rank` deterministic, `window` from the Gemini confirmer). No "relevance" field exists.
- The PC never renders (CLAUDE.md: PC = narration only). Nothing moves workspace JSON between the machines.

## 2. Goals

1. Hook clips and chapters are spans on the timeline; a selected span's edges drag by ±N s (0.1 s precision).
2. A drag becomes **data**, not a render: a net delta against the planner's window, stored on the PC, applied by one laptop command.
3. The same record carries what the owner called "the digest": window with its diff, impressiveness, relevance, a description. Facts are deterministic; Jev scores; the description is a template.
4. Repeated edits of one shape propose a rule and a constant change ("AI for determinism": learn the correction, do not re-drag every video).
5. Takes already recorded land in the right place after the re-join.

## 3. Non-goals

- Rendering anything on the PC. Reordering, adding or deleting hook clips or chapters from the timeline (chat / CLI as today).
- Backfilling the current pre-feature join map (D8): the laptop's play-mode re-join writes the first `clips[]`.
- Any automatic upload. A Drive "send edits" button only if the manual copy gets old (D5).
- Camera lane and effects lane (narration v2 phases 2 and 3).

## 4. Design

### 4.1 Two files, two owners (D1, D16)

| File | Owner | Holds |
|---|---|---|
| `WORKSPACE/story/<stem>.join.json` (schema_version 2) | laptop (`story-join`) | rendered truth: games, chapters, inserts, **`hook`** (mode, title, picks by chapter id) and **`clips[]`** (every hook clip and chapter as a span with its base window, applied delta, facts, rating) |
| `WORKSPACE/story/<stem>.edits.json` | PC (narrate page) | owner intent: one **net delta** per clip id measured from the base window, the Jev rating, the digest of the map the edits were made against, and the applied history |

The map is rewritten by every join; the edits file never is. `base_map_digest` (sha256 of the join map file, first 12 hex) ties them.

### 4.2 Stable ids (D13, D14)

- `chapter_id = "<round>:<play_id>"` for play chapters; `"<round>:<kind>@<game_start_s>"` for the others, e.g. `…:early@50.0`, `…:early@273.1`, `…:close@934.5`, `…:request@1205.0` (`Chapter.play_id` exists; the kind alone is not unique: the opening and the recall are both `early`, which the playground mock caught).
- Hook clip id: `"hook:<chapter_id>"`. Chapter clip id = `chapter_id`.
- Hook picks are stored as `{chapter_id, text}` plus the clip's deltas; `parse_pick("3:2")` still works on the CLI and is resolved to a `chapter_id` at join time. Story.mp4 seconds for the hook window are computed at join time from the (edited) chapter, so a re-rendered round cannot move the hook's footage.

### 4.3 Join map v2 (laptop)

```jsonc
"schema_version": 2,
"hook": { "mode": "play", "title": "Want to know the trick to laning?",
          "picks": [ { "chapter_id": "2026-11-25_2359_yunara_6-0-5:p07", "text": null } ] },
"clips": [ {
  "id": "hook:2026-11-25_2359_yunara_6-0-5:p07",
  "kind": "hook",                       // "hook" | "chapter"
  "round": "2026-11-25_2359_yunara_6-0-5",
  "chapter_id": "2026-11-25_2359_yunara_6-0-5:p07",
  "mode": "play",                       // hook only
  "video_start_s": 0.0, "video_end_s": 6.9,            // in this mp4
  "game_start_s": 236.3, "game_end_s": 258.6,          // footage shown, after applied edits
  "base_game_start_s": 236.3, "base_game_end_s": 256.6, // planner / hook_window before any owner edit
  "applied": { "head_s": 0.0, "tail_s": 2.0 },
  "facts": { "numbers": "2v3", "kind": "skirmish", "zone": "bot_lane", "owner_role": "initiator_kill",
             "outcome": "won 3-for-0", "kills": 2, "deaths": 0,
             "kill_times_s": [238.9, 241.7], "death_times_s": [], "items": [] },
  "rating": { "impressiveness": 8, "relevance": 7, "description": "…", "source": "jev", "rated_at": "…" }
} ]
```

`facts` are copied from the plan/plays (champion names only, never mic text). `rating` is copied forward from the edits file when the laptop applies, otherwise null. `JoinMap` stays a pydantic model; unknown-field tolerance is unchanged, but `--apply-edits` refuses a map without `schema_version >= 2` (D15).

### 4.4 Edits file (PC)

```jsonc
{ "schema_version": 1, "video": "joined_2026-11-26_xayah-kaisa-yunara",
  "base_map_digest": "sha256:1f3a…",
  "edits":   [ { "clip_id": "hook:…:p07", "head_s": 0.0, "tail_s": 2.0, "note": null,
                 "made_at": "2026-11-28T…Z", "rating": { "impressiveness": 8, "relevance": 7, "description": "…" } } ],
  "applied": [ ] }
```

- `head_s` > 0 starts earlier, `tail_s` > 0 ends later; both are **net from `base_game_*`** (D10). `PUT` replaces the entry for that clip id; `DELETE` removes it. Dragging +2, then +3, then back to +2 is one entry, `tail_s: 2.0`.
- The laptop moves entries to `applied` with `applied_at` and `map_digest`, and folds the delta into the new map's `applied` / `game_*`.

### 4.5 Validation (pure, `story/clip_edits.py`, mirrored in `web/src/lib/clipEdits.ts`)

- Length ≥ `HOOK_MIN_S` (2.0) for hooks, ≥ `MIN_CHAPTER_S` (3.0) for chapters (D19).
- A chapter edge clamps at its neighbour's current edge minus `CLAMP_GAP_S` (0.5) and at the game's bounds (D12). Chapters never merge from a drag.
- A hook clip must lie inside its chapter's **edited** window. Dragging it past the chapter's footage writes the hook delta **and** a chapter edit on the same `chapter_id` with the needed delta, in one `PUT` (D3). The chapter clamp applies to that cascade too.
- Owner edits ignore `HOOK_PLAY_MAX_S`.
- Edits are refused while a take is recording (page disables the handles; API has no such state, so this is a UI rule).

### 4.6 PC API and MCP (D7, CLAUDE.md MCP coverage rule)

| Endpoint | Does | MCP tool |
|---|---|---|
| `GET /narration/joined/{key}/clips` | clips from the map merged with the edits file: current intent per clip, `pending` flag, `pending_count`, the laptop command text, `suggested_rules` | `clip_edits_list` |
| `PUT /narration/joined/{key}/clips/{clip_id}/edit` body `{head_s, tail_s, note?}` | validates (4.5), writes the edits file, rates with Jev (4.7), appends to the correction log; 409 when `base_map_digest` no longer matches the map on disk (the map changed under the edits: the page shows "re-join landed, pending edits were made against the old map"; `?rebase=true` re-keys them onto the new map when every clip id still exists) | `clip_edit_set` |
| `DELETE /narration/joined/{key}/clips/{clip_id}/edit` | undo one pending edit | `clip_edit_clear` |

Only `joined` targets have clips. Story targets return `[]` for now (chapters of a single story could join later with the same model).

### 4.7 AI for determinism (D2, D17, D18)

- **Rating runs on the PC right after the drag.** Inputs: the clip's `facts`, the base and edited windows, the video's thesis. Two typed `ScoreQuestion`s through Jev (`decisions/`): `impressiveness` (numbers faced, outcome, kills/deaths, role, objective after) and `relevance` to the thesis. The thesis = the join title plus the "The thesis" bullets of the vault script whose `video:` front-matter names this mp4 (`knowledge/videos/*.md`); title only when no script matches.
- **Description is a template**, never model prose: `"<numbers> <kind> in <zone>; <champion> <role verb>; <k> kills, <d> deaths; <delta sentence>"` where the delta sentence names what the new window gains or loses from `kill_times_s`/`death_times_s` ("+2.0 s tail shows the second kill land", "−1.0 s head drops the walk-up").
- **Jev down or over cap:** the edit commits with `rating: null` and `rating_error`; the inspector shows the previous rating greyed. One Jev call per committed edit, cached by (clip_id, head_s, tail_s).
- **Learning:** each `PUT` appends `{made_at, clip kind, mode, edge, delta_s, numbers, outcome}` to `WORKSPACE/story/_joined/<stem>/clip-corrections.jsonl`. `suggested_rules` is deterministic: for each (kind, mode, edge) with ≥ `RULE_MIN_N` (3) edits of the same sign, propose the median as the new constant (`HOOK_PLAY_TAIL_S 2.0 → 4.0 (n=3)`, `HOOK_LEAD_S`, `EVENT_POST_S`). Accepting is a chat decision: a `knowledge/` note and the constant change in the same commit. Nothing changes a constant by itself.
- **Privacy:** facts and descriptions carry champion names only and no mic text; the record stays on the PC until the owner sends it.

### 4.8 Laptop: `story-join --apply-edits <stem>` (D7, D11)

1. Load the map and the edits file; refuse when `schema_version < 2` or `base_map_digest` ≠ digest of the current map (unless `--force-edits`).
2. For every chapter with an edit (direct or cascaded), write **one** managed request into that round's `requests.json`: `{"kind": "set_window", "chapter": "<chapter_id>", "start": <game clock>, "end": <game clock>, "source": "clip_edit"}`, replacing any earlier `clip_edit` entry for the same chapter. Hand-written requests are untouched. `set_window` is absolute, applied after `extend`/`trim`, and marks the chapter pinned so the 360 s budget cannot drop it.
3. `story-render` each touched round.
4. Re-join with the map's `hook` picks and title; hook windows = `hook_window(...)` for the (edited) chapter, then `head_s`/`tail_s` applied, clamped to the chapter's footage.
5. Write the new map: `clips[]` with `applied` folded in and `rating` copied from the edits; move the edits to `applied`.
6. MCP: `story_join(apply_edits=True)`.

### 4.9 Takes across a re-join (D6)

- The first take pins `clips_snapshot` (id, video_start_s, video_end_s of every clip) in `session.json` next to `video_fingerprint` and `pinned_intro_s`.
- On stale with a snapshot and a new map, `relock` offers **remap**: each take's start maps to (clip_id, offset inside that clip) and lands at the new clip's `video_start_s + offset` (clamped inside the new clip). Takes over the hook map through their hook clip. A take whose clip is missing from the new map is flagged `orphaned` (drawn red, left in place).
- Sessions without a snapshot keep today's uniform intro shift.

### 4.10 Web (D4, D19)

- `MarkersLane` becomes `ClipsLane`: two rows (hook clips; chapters), cues stay below. Spans are buttons (click = seek, as today). The selected span shows head/tail handles (TakeBlock pattern: pointer capture, local draft, one `PUT` on release, live "+2.0 s" label). Shift snaps to 0.5 s.
- Pending edits draw as a hatched extension at the span's edge in video seconds; **nothing downstream moves**; the frame strip is unchanged.
- `ClipInspector` (right column, shown when a clip is selected): name, kind, game/chapter, window in game clock with the diff `head +0.0 · tail +2.0`, ±1 s buttons, numeric fields, `reset`, note, impressiveness, relevance, description, `pending · re-join on the laptop` or the applied state.
- Header badge `N pending edits · re-join on the laptop` opens a popover: the list with undo per edit, `suggested_rules`, the edits file path and the laptop command as copyable text (no upload button).
- Pure `lib/clipEdits.ts` (vitest): clamp, min length, hook→chapter cascade, net delta, px↔s for the hatched extension.

### 4.11 Sync (D5)

Manual. The popover shows the edits file path and `story-join --apply-edits <stem>`. The owner copies the file to the laptop and the re-joined mp4 + map back, as today. Future (only if asked): an explicit "send edits to laptop" button that uploads the small JSON to Drive `renders/story-join/`.

## 5. Security and privacy

- The record carries champion names and timeline facts only; no mic transcript text, no summoner names, no VLM prose. Jev inputs are those facts; outputs are integers.
- Nothing uploads anywhere automatically (CLAUDE.md). The edits file and the correction log stay in `WORKSPACE`.
- Clip ids and request bodies are validated by pydantic; no raw args reach ffmpeg.

## 6. Error handling

| Case | Behaviour |
|---|---|
| Map changed under pending edits | `PUT` 409; page banner; `?rebase=true` when every clip id survives |
| Clip id missing on the laptop (chapter dropped by a re-understand) | `--apply-edits` lists the orphaned edits and continues with the rest; they stay pending |
| Jev failure | edit commits, `rating: null`, `rating_error` string |
| Drag below min length / into the neighbour | handle clamps live; API rejects anything the UI let through (422) |
| Laptop on master | no `--apply-edits`; the map stays v1; the PC page shows no clips lane for a v1 map (markers as today) |

## 7. Testing

- API pure: `clip_edits.py` clamp, min length, cascade, net delta, digest check, rule proposer; `set_window` applied after extend/trim and pinned; `build_join_map` v2 clips with base windows and facts from a fixture plan; `hook_window` from an edited chapter; remap of takes (hook grows, mid-video chapter grows, dropped clip → orphaned).
- Web vitest: `lib/clipEdits.ts`.
- Smoke on the real map once the laptop's play-mode join lands: four hook clips as spans, drag +2 s on hook 1, edits file written, Jev rating present, badge counts 1.

## 8. Edge cases

- Two edits touching the same chapter (a hook cascade and a direct chapter drag): one chapter entry; the later `PUT` wins and must still contain the hook.
- A hook clip whose chapter is the first chapter of a game: clamping at the game's bound, `head_s` cannot reach before 0:00 of the recording.
- A `set_window` that shrinks a chapter below `MIN_CHAPTER_S` is refused on the laptop too.
- Swipe overlap (0.5 s) is already inside `video_start_s`; the hatched extension is drawn in video seconds and may overlap the next span visually. Acceptable by D4.

## Decisions Log

| ID | Topic | Decision | Rationale | Source | Date |
|---|---|---|---|---|---|
| D1 | Source of truth | Per-joined-video clip list; the join map carries clips as rendered truth, a sibling edits file carries intent | Picks are saved nowhere today; the planner never reads the digest; a per-video record is what the owner pictured | Interview | 2026-11-28 |
| D2 | AI role | Re-rate (Jev) on each committed drag and log corrections; repeated shapes propose a rule and a constant change | "AI for determinism": facts stay deterministic, the model scores, repeats become rules | Interview | 2026-11-28 |
| D3 | Hook past chapter | The drag also extends the chapter (one drag, two entries) | Hooks are cut from story.mp4, so the chapter must have the footage | Interview | 2026-11-28 |
| D4 | Pending UI | Hatched extension at the edge, nothing downstream moves, badge lists pending edits with undo | Honest about the PC not rendering | Interview | 2026-11-28 |
| D5 | Sync | Manual now; explicit Drive button later only if needed | Owner: "don't worry too much … drive on an explicit button only if we need" | Interview | 2026-11-28 |
| D6 | Takes across re-join | Snapshot of clips at first take; remap by (clip_id, offset); orphans flagged | A uniform intro shift is wrong for mid-video chapter changes | Interview (Claude ruling) | 2026-11-28 |
| D7 | Laptop step | One command `story-join --apply-edits <stem>` | One step the owner can run between sessions | Interview (Claude ruling) | 2026-11-28 |
| D8 | First case | Wait for the play-mode re-join; no backfill of the old map | The hook changes mode anyway; play mode already shows the outcome | Interview | 2026-11-28 |
| D9 | Chapters too | Chapter spans drag through the same record | The hook cascade needs the chapter path anyway | Interview (Claude ruling) | 2026-11-28 |
| D10 | Idempotence | Edits are net deltas from the planner base window; one entry per clip id | Re-joins rewrite the map; stacked relative requests double-apply | Red Team | 2026-11-28 |
| D11 | Requests | New absolute `set_window` request, one managed entry per chapter, pinned against the budget | `extend` only grows, `trim` only shrinks, both keyed by game clock | Red Team | 2026-11-28 |
| D12 | Overlap | Clamp at the neighbour (0.5 s gap); never merge from a drag | The renderer merges overlapping chapters and ids would break | Red Team (owner chose) | 2026-11-28 |
| D13 | Hook anchor | Picks keyed by chapter id in game-clock terms; story seconds computed at join time | story.mp4 seconds move when an earlier chapter changes | Red Team | 2026-11-28 |
| D14 | Chapter id | `round:play_id` for plays; `round:kind@game_start_s` for the rest | Titles change; indexes shift when the budget drops a play; a game has two `early` chapters, so the kind alone collides | Red Team (fixed after the playground mock) | 2026-11-28 |
| D15 | Version skew | `schema_version` on the map; apply refuses unversioned maps; edits in a separate file | A laptop on master would silently drop unknown fields | Red Team | 2026-11-28 |
| D16 | Two truths | Join map = laptop output, edits file = PC intent; base digest ties them | One file edited by both machines would be overwritten | Red Team | 2026-11-28 |
| D17 | Rating time and place | PC rates right after the drag from facts the map carries; description is a template; Jev failure never blocks | Owner: "shouldn't PC also be able to use Jev? let's make it easy"; the laptop writes the facts so the PC has honest inputs | Red Team | 2026-11-28 |
| D18 | Privacy | Facts and descriptions: champion names only, no mic text; the record stays on the PC until sent | CLAUDE.md rules | Red Team | 2026-11-28 |
| D19 | Drag feel | 0.1 s precision, Shift snaps 0.5 s, ±1 s buttons, min 2.0 s hook / 3.0 s chapter | Matches the takes lane | Interview (Claude default) | 2026-11-28 |

## Dependency Graph & Implementation Order

```
T1 join map v2 (ids, clips, hook, schema_version)  ──┬──> T4 PC API + MCP ──> T6 web lane + inspector + badge
T2 requests set_window + pinned                      ├──> T5 story-join --apply-edits
T3 clip_edits.py (models, validation, Jev, rules) ───┘         │
T1 + narration session ───────────────────────────────────> T7 takes remap on relock
T4..T7 ───────────────────────────────────────────────────> T8 docs + vault + handoff
```

1. **T1 Join map v2** (laptop side, pure): `chapter_id()`, `JoinClip`, `JoinHook`, `schema_version`, facts from the plan; `build_join_map` test on fixture plans.
2. **T2 `set_window` request**: model, apply order, pinned flag; `story-join` resolves `--hook 3:2` to chapter ids.
3. **T3 `story/clip_edits.py`**: `ClipEdits` models, `validate_edit` (clamp, min, cascade), `net_window`, `map_digest`, Jev `ScoreQuestion`s, description template, `clip-corrections.jsonl` + `suggested_rules`.
4. **T4 PC API + MCP + registry**: the three endpoints, `clip_edits_list` / `clip_edit_set` / `clip_edit_clear`, `JoinedStoryTarget.clips()`.
5. **T5 `story-join --apply-edits`** + `story_join(apply_edits)`.
6. **T6 Web**: `lib/clipEdits.ts` + tests, `ClipsLane`, `ClipInspector`, pending badge, `useClipEdits`.
7. **T7 Takes remap**: `clips_snapshot` in `session.json`, `relock(mode="remap")`, `orphaned` on `Take`, red in `TakesLane`.
8. **T8 Docs**: `api/docs/narration.md`, story-join docs, `web/docs/features/timeline.md`, `knowledge/series/Story-join.md`, laptop handoff.
