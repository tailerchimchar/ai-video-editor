# Spec: hook clip edits from the narration timeline

**Built** 2026-11-28/29 on `feat/clip-edits` (phase 1; commits from the plan run). Docs: `api/docs/narration.md` (Hook clip edits), `web/docs/features/narration.md`, `knowledge/series/Story-join.md`.

**Status:** decided 2026-11-28, revised the same day after two reviews and the owner's playground test · **Baseline:** `feat/narration-v2` @ 24f4004 · **Preview:** https://claude.ai/artifact/SXy8VfwPw2cMd8X91mmQm9 · **Playground (drag it):** https://claude.ai/artifact/M46eiu7XEc5XmSrAKYy6C3 · **Builds on:** [`spec-narration-v2.md`](./spec-narration-v2.md)

The first version of this spec covered hook clips, chapters, Jev ratings, a rule proposer and a takes remap in one go, with a hatched "pending" overlay on the timeline. The owner tried the mock and did not like the overlay ("in Clipchamp you can move and re-order; in this one you see the shadow"), and both reviews (UX, editing model) said the same two things: the layout should slide like Clipchamp, and two thirds of the spec is not needed to get "+2 s on both hook clips, done by me". This revision is **phase 1 = hook clips only**, with the rest kept as later phases.

## 1. Problem

On the PC's narrate page the owner can see where the hook clips sit but can only click them. The hook of the current video ends at the wrong moment; the owner wants to extend each hook clip by +2 s the way Clipchamp extends a clip, re-order the montage, and have the data follow, without asking in chat and without rendering on the PC (CLAUDE.md: PC = narration only).

Today:

- Hook picks (`--hook 3:2 …`, mode, title) are not persisted. The join map records one `hook` insert of 13.8 s. Windows come from constants in `story/bookends.py` (`HOOK_LEAD_S 6`, `HOOK_PLAY_TAIL_S 2`, `HOOK_PLAY_MAX_S 14`), cut from the round's rendered `story.mp4`.
- Markers on the narrate page are points; click = seek.
- Nothing moves workspace JSON between the machines.

## 2. Phase 1 goals

1. Hook clips are spans on the timeline. Edges trim or extend (0.1 s, Shift snaps 0.5 s); the body drags to re-order; × takes a clip out; a chapter's inspector has "add to the hook".
2. The timeline **slides** (ripple): the clip grows, everything after it moves, the ruler shows the new intro and total length. No overlay, no shadow.
3. A drag becomes data only: a small PC-owned edits file. One laptop command re-joins from it.
4. Preview without rendering: the playhead maps edited time to this mp4's time; seconds a hook gained that are not in the hook yet are played from the same moment in the game body (the chapter the hook was cut from).

## 3. Non-goals for phase 1 (later phases in §8)

Chapter edges and "skip this chapter"; takes remap for mid-video changes; Jev ratings and descriptions; the rule proposer; rendering on the PC; automatic upload; reordering chapters (the story is chronological and anchored to the Riot timeline; reorder breaks `_settle`, placement, YouTube chapters and the facts. Jev is only a scorer and never the reason).

## 4. Design

### 4.1 Join map v2 (laptop writes)

`JoinMap` gains `schema_version: 2` and a `hook` block; `write_join_files` is passed the picks it currently drops (`story/join.py`).

```jsonc
"schema_version": 2,
"hook": {
  "mode": "play", "title": "Want to know the trick to laning?",
  "picks": [ {
    "chapter_id": "2026-11-25_2359_yunara_6-0-5:p01",   // round + play chapter id (4.3)
    "round": "2026-11-25_2359_yunara_6-0-5", "chapter_index": 1,
    "rec_start_s": 2451.3, "rec_end_s": 2471.3,         // base window = hook_window(), RECORDING seconds
    "offset_s": 2215.0,                                   // recording - game clock, as used by this render
    "start_s": 0.0, "end_s": 0.0,                         // owner offsets already applied (+ = earlier / later)
    "video_start_s": 0.0, "video_end_s": 14.0,            // where it sits in this mp4
    "events": [ { "rec_s": 2463.0, "kind": "kill" }, { "rec_s": 2471.0, "kind": "kill" } ],  // for the ticks
    "chapter_rec_start_s": 2451.3, "chapter_rec_end_s": 2473.6   // the chapter's footage bounds
  } ]
}
```

Recording seconds, not game clock: the game clock is `recording − offset_s` and the offset is fragile (`api/CLAUDE.md`, Riot gotchas); the map carries the offset it used so the page can still show game clock.

### 4.2 Edits file (PC writes): `WORKSPACE/story/<stem>.edits.json`

Each pick carries its track position `at_s` so the page reopens the same way; the laptop sorts by `at_s` and ignores the empty space between picks.

```jsonc
{ "schema_version": 1, "video": "joined_2026-11-26_xayah-kaisa-yunara",
  "base_digest": "sha256:1f3a9c…",      // over the map's hook pick ids + base windows, not the whole file
  "hook": { "mode": "play", "title": "Want to know the trick to laning?",
            "picks": [ { "id": "3f9a1c2b", "chapter_id": "…yunara…:p01", "at_s": 0.0,  "start_s": 0.0, "end_s": 2.0 },
                       { "id": "8d7e6f50", "chapter_id": "…kaisa…:p01",  "at_s": 30.0, "start_s": 0.0, "end_s": 0.0 } ] } }
```

The list **is** the hook: sorted by `at_s` it is the montage order, a missing pick is removed, a new one is added. `start_s`/`end_s` are net offsets from the base window (D10), so the file is idempotent and a re-join can never apply them twice. `PUT` replaces the whole list.

### 4.3 Ids

`chapter_id = "<round>:<play_id>"` for play chapters, `"<round>:<kind>@<rec_start_s>"` otherwise (two `early` chapters per game, so the kind alone collides). Hook picks are play chapters, so phase 1 only needs the first form. `play_id` stability across a `league-understand` re-run is unverified: `--apply-edits` also matches on `rec_start_s` within 1 s and reports a pick it cannot find instead of guessing.

### 4.4 Limits (pure, `story/hook_edits.py`, mirrored in `web/src/lib/hookEdits.ts`)

- Clip length ≥ `HOOK_MIN_S` (2.0). Owner edits ignore `HOOK_PLAY_MAX_S`.
- A hook may run past its chapter's own footage (the laptop extends the chapter, 4.6) but never into the neighbouring chapters: `rec_start ≥ prev.rec_end + 0.5`, `rec_end ≤ next.rec_start − 0.5`. The handle stops there.
- Edits are disabled while a take is recording (UI rule).

### 4.5 PC API and MCP

| Endpoint | Does | MCP tool |
|---|---|---|
| `GET /narration/joined/{key}/storyboard` | the whole video as JSON for the panel: hook slots, games, chapters with ids, windows, kill times, `in_hook` | `storyboard_get` |
| `GET /narration/joined/{key}/hook` | the map's hook block merged with the edits file: picks in current order with current offsets, per-pick `pending`, intro and duration as they will be, the laptop command text | `hook_picks_get` |
| `PUT /narration/joined/{key}/hook` body `{picks:[{chapter_id,start_s,end_s}]}` | validates (4.4), writes the edits file, returns the merged view; 409 when `base_digest` no longer matches the map on disk | `hook_picks_set` |
| `DELETE /narration/joined/{key}/hook` | discard all changes (delete the edits file) | `hook_picks_reset` |

`JoinedStoryTarget.hook()` reads the map's block (`narration/targets.py`). A v1 map (no `hook` block) returns `null` and the page shows markers as today.

### 4.6 Laptop: `story-join --apply-edits <stem>` (and `story_join(apply_edits=True)`)

1. Load the map and the edits file; refuse when `schema_version < 2` or `base_digest` differs (unless `--force-edits`).
2. Resolve each pick to a chapter (4.3). For each pick: window = `hook_window(...)` then `start_s`/`end_s`, clamped per 4.4.
3. When a window runs past the chapter's footage, write **one managed** `extend` request `{kind:"extend", at:<chapter game clock>, after_s|before_s, source:"clip_edit"}` into that round's `requests.json`, replacing any earlier `clip_edit` entry for the same chapter (every `story-render` re-plans and re-applies `requests.json`, so one replaced entry is already a net change; no `set_window` kind is needed), then `story-render` that round. `_refill` must re-collect kill/death times for the grown chapter so `hook_window` sees them.
4. Re-join with the file's order, mode and title (`render_hook` numbers clips and puts the title on the first). Write the new map with `start_s`/`end_s` folded into the picks and the owner offsets reset to 0 in a fresh edits file (or delete it).
5. The CLI's `--hook 3:2` still works for a first join; afterwards the map is the source of picks.

### 4.7 Web (slots and ripple, D20, D29, D30)

The owner tested the ripple mock and asked for three changes (2026-11-28, late): a deleted clip leaves an **empty slot**, a trimmed clip leaves its empty space inside its slot, an extended clip **pushes** the later clips; the four 1 s arrow buttons go (the offset fields stay); and with nothing selected the panel shows the **whole storyboard as JSON**, read-only until a "write" button.

- **A track, like a video editor** (owner, after testing the slot version: "if I extend a clip into the empty space it should enter it; make it like an actual rendering software video model"). The hook lane is a track. Every clip has a position `at_s` on it and a length; empty space is simply where no clip is. Rules, all pure (`lib/hookTrack.ts`, mirrored in `story/hook_edits.py`):
  - clips never overlap: after any change, a clip that overlaps the one before it is pushed right, and so on down the track (`settle`);
  - **extend the end**: the clip grows in place; it eats empty space first and pushes the next clips only when it reaches them;
  - **extend the start**: the start moves left into empty space; at the previous clip's end it stops and the extra length goes to the right instead (pushing);
  - **trim**: the clip shrinks, nothing else moves, empty space appears;
  - **delete** (×, Delete/Backspace, the panel's button): the clip goes, the space stays;
  - **move** (body drag): the clip is lifted and dropped at the pointer, snapping to neighbour edges within 0.4 s; dropped onto another clip it lands before or after it depending on which half, and everything after is pushed; the space it left stays empty;
  - "add to the hook" lands in the first empty space that fits, else at the end of the track; "close the empty spaces" compacts in track order;
  - **split** (`Ctrl+S`): the selected clip is cut at the playhead into two clips of the same chapter, each at least 2 s; the left piece keeps its id, the right piece gets a new one and sits right after it; both behave like any clip (delete one, move it, trim it);
  - **undo / redo** (`Ctrl+Z`, `Ctrl+Shift+Z` or `Ctrl+Y`): the page keeps a history of the pick list (100 steps); each step is one PUT.
  Empty space is an editor thing: the laptop joins the clips back to back in track order, and the preview line shows both lengths ("intro 13.8 → 40.5 s (14.0 s of empty space here)").
- **Storyboard JSON.** With nothing selected the right panel shows the whole video as JSON (`GET /narration/joined/{key}/storyboard`: the hook picks with `at_s`, every game and chapter with ids, windows, kill and death times and `in_hook`). A "write" button turns it into a textarea; "apply" parses it and takes `hook.picks` only in phase 1 (ids must exist and be play chapters; `at_s` is the place on the track, missing = right after the previous pick; offsets are clamped; the result is settled), "cancel" discards. Errors show inline, nothing is written until apply succeeds.

- `MarkersLane` keeps cues; a new `HookLane` draws the track from the edits (not from the rendered mp4): each clip at its `at_s`; game 1 starts at the end of the track; every chapter marker and the frame strip slide by `track_end − rendered_intro`.
- Hook span: edges trim/extend with 12 px handles (TakeBlock pattern, listeners on `window`, draft until release, one `PUT`); body drag re-orders with a ghost and a slot marker; × removes; an orange outline and a dot mark a changed clip; kill/death ticks on every span (green/red) so the owner sees whether the extension catches the kill.
- Inspector: "shows 4:04.0 – 4:20.1 (game clock)", "as rendered …", the clip's place on the track, the diff in words ("ends 2.1 s later"), offset fields labelled "offset (s)" (no arrow buttons: the owner drags), reset, delete (leave space), a one-line note when the clip runs past the chapter's footage, and a "storyboard" button back to the JSON view. A chapter's inspector gets "add to the hook" (play chapters only).
- Header badge "N hook changes · laptop applies" → popover: one line per change (trim, re-order, add, delete, empty spaces) with undo, close the empty spaces, discard all, the laptop command and the edits file path as text. Keyboard: Delete/Backspace deletes the selected hook clip, Escape deselects.
- Preview: the frame strip tiles over seconds a hook gained are drawn as placeholders; the preview line says where the playhead plays from. Playback of the edited timeline is a seek map: inside a hook's rendered window → the mp4's hook seconds; gained seconds → the same recording moment in the chapter later in the mp4 (`video_second` of the chapter); game body → mp4 time minus the intro delta. A small jump at each boundary is accepted.

### 4.8 Takes

A hook-only change shifts everything after the hook by the intro delta, which the existing stale/shift-relock handles. **Re-ordering** keeps the intro length, so the suggested shift is 0 while takes recorded over the hook now sit over different footage: takes whose start lies inside the hook region are flagged `over_hook_changed` in the session view and drawn red until re-recorded or deleted. Takes remap by clip is phase 2.

### 4.9 Sync

Manual (D5): the popover shows the file path and the command; the owner copies the small JSON to the laptop and the re-joined mp4 + map back. A Drive "send edits" button only if asked.

## 5. Security and privacy

The edits file holds chapter ids and seconds; the map's hook block holds champion-free timing facts (`events` kinds and times). No mic text, no summoner names, nothing uploads anywhere automatically.

## 6. Error handling

| Case | Behaviour |
|---|---|
| Map changed under the edits | `PUT` 409; banner "the laptop re-joined since these changes; discard or re-apply them" with both buttons |
| A pick's chapter cannot be found on the laptop | `--apply-edits` lists it, applies the rest, keeps it in the file |
| Laptop on master | no `--apply-edits`, map stays v1, page shows markers only |
| Hook runs past the chapter and the chapter's neighbour is close | the clamp (4.4) stops the drag; the laptop's `_settle` can therefore never merge chapters because of a hook edit |

## 7. Testing

- API pure: `hook_edits.py` clamp, min length, neighbour clamp, net offsets, digest; `build_join_map` v2 writes the hook block from fixture plans; `hook_clips` applies offsets after `hook_window`; managed `extend` is replaced not appended; `apply_edits` dry-run on a fixture map (reorder, add, remove, offsets, a missing chapter).
- Web vitest: `lib/hookEdits.ts` (layout/ripple, clamp, seek map).
- Smoke on the real map once the laptop's play-mode join lands: four hook spans, drag +2 s on clip 1, file written, badge says 1, intro shown 54.0 → 56.0.

## 8. Later phases

- **Phase 2, chapters:** chapter edges (one managed `extend`/`trim` per chapter, replaced each apply), "skip this chapter" (`drop`), non-play ids, takes remap by (clip, offset), the 409 rebase path.
- **Phase 3, "AI for determinism":** Jev `ScoreQuestion`s for impressiveness and relevance from the facts the map carries (PC side, the owner's wish: "PC should use Jev too"); a templated description; a deterministic rule proposer from the net edits (the playground already shows one: three hook clips ending later propose `HOOK_PLAY_TAIL_S 2.0 → 4.0`). Accepting a rule stays a chat decision: vault note plus constant, one commit.

## Decisions Log

| ID | Topic | Decision | Rationale | Source | Date |
|---|---|---|---|---|---|
| D1 | Source of truth | A per-joined-video record: the join map carries the hook block as rendered truth, a sibling edits file carries intent | Picks are saved nowhere today | Interview | 2026-11-28 |
| D2 | AI role | Re-rate (Jev) and propose rules from repeated edits | "AI for determinism" | Interview (moved to phase 3 by D22) | 2026-11-28 |
| D3 | Hook past chapter | The laptop extends the chapter when a hook needs more footage | Hooks are cut from story.mp4 | Interview | 2026-11-28 |
| D4 | Pending UI | ~~Hatched overlay, nothing moves~~ → superseded by D20 | Owner disliked the shadow | Interview | 2026-11-28 |
| D5 | Sync | Manual now; explicit Drive button later only if needed | Owner: "don't worry too much" | Interview | 2026-11-28 |
| D6 | Takes across re-join | Remap by (clip, offset) | Uniform shift is wrong mid-video | Interview (phase 2 by D22) | 2026-11-28 |
| D7 | Laptop step | One command `story-join --apply-edits <stem>` | One step between sessions | Interview | 2026-11-28 |
| D8 | First case | Wait for the play-mode re-join; no backfill | Hook changes mode anyway | Interview | 2026-11-28 |
| D9 | Chapters too | Chapter edges through the same record | Same path | Interview (phase 2 by D22) | 2026-11-28 |
| D10 | Idempotence | Net offsets from the base window; the list is the hook | Re-joins rewrite the map | Red Team | 2026-11-28 |
| D11 | Requests | ~~New `set_window`, pinned~~ → one managed `extend`/`trim` per chapter, replaced each apply (D24) | Every render re-plans then re-applies requests, so a replaced entry is already net; requests run after the budget | Red Team, corrected by review | 2026-11-28 |
| D12 | Overlap | Clamp at the neighbour (0.5 s); never merge from a drag | Renderer merges overlaps | Red Team (owner chose) | 2026-11-28 |
| D13 | Hook anchor | Picks keyed by chapter id; story seconds at join time | story.mp4 seconds move on re-render | Red Team | 2026-11-28 |
| D14 | Chapter id | `round:play_id`; `round:kind@rec_start_s` for the rest; match on `rec_start_s` as fallback | Kind alone collides; `play_id` stability unverified | Red Team + playground + review | 2026-11-28 |
| D15 | Version skew | `schema_version`; apply refuses v1; edits in a separate file | master would drop unknown fields | Red Team | 2026-11-28 |
| D16 | Two truths | Map = laptop output, edits = PC intent, digest ties them | | Red Team | 2026-11-28 |
| D17 | Rating place | PC rates with Jev from facts the map carries | Owner: "PC should use Jev too, keep it easy" | Red Team (phase 3) | 2026-11-28 |
| D18 | Privacy | Champion names only, no mic text, nothing leaves the PC by itself | CLAUDE.md | Red Team | 2026-11-28 |
| D19 | Drag feel | 0.1 s, Shift 0.5 s, 1 s arrow buttons, min 2.0 s | Matches takes lane | Interview (Claude default) | 2026-11-28 |
| D20 | Ripple, not shadow | The timeline lays itself out from the edits: clips grow, everything after slides, ruler shows new intro and length | Owner: "in Clipchamp you can move and re-order; here you see the shadow, I don't like that"; UX review #2 | Interview | 2026-11-28 |
| D21 | Reorder / remove / add | Hook clips drag to re-order, × removes, chapters offer "add to the hook"; chapters never reorder | The montage order is the main editing decision; stories are chronological | Interview (both reviews) | 2026-11-28 |
| D22 | Phase 1 = hooks only | Chapters, takes remap, Jev, rule proposer move to phases 2 and 3 | Owner: "is this the minimal way?"; both reviews: two thirds premature | Interview | 2026-11-28 |
| D23 | Preview by seek map | Gained hook seconds play from the same moment in the game body; no render | The footage is already in the mp4 (UX review #1) | Interview | 2026-11-28 |
| D24 | Managed extend | One `extend`/`trim` per chapter tagged `source:clip_edit`, replaced each apply; no `set_window`, no pinning | Editing review: render re-plans then re-applies requests; requests run after the budget | Interview | 2026-11-28 |
| D25 | Recording seconds | Base windows and events stored in recording seconds with the offset used | Game clock depends on a fragile offset (editing review risk 5) | Interview | 2026-11-28 |
| D26 | Digest scope | `base_digest` over hook ids + base windows, not the file | A re-join that changes nothing must not invalidate edits (risk 4) | Interview | 2026-11-28 |
| D27 | Reorder and takes | Hook-region takes flagged when the order changes (intro delta is 0) | Editing review | 2026-11-28 |
| D29 | A track, not slots | Clips sit at `at_s` on a track; extend eats empty space then pushes; trim and delete leave space; move snaps and pushes; the laptop joins in track order | Owner: "if I delete a clip, just leave an empty space … if I extend, push the other clips", then "if I extend into the empty space it should enter it … like an actual rendering software video model" | Interview | 2026-11-28 |
| D30 | Storyboard JSON | Nothing selected → the whole video as JSON, read-only; "write" opens a textarea, apply takes `hook.picks` only in phase 1 | Owner: "see the json of the whole game … non-editable unless I click write" | Interview | 2026-11-28 |
| D32 | Undo | Ctrl+Z undoes the last hook change, Ctrl+Shift+Z / Ctrl+Y redoes; history lives in the page | Owner: "also pls implement ctrl + z" | Interview | 2026-11-28 |
| D33 | Split | Ctrl+S splits the selected hook clip at the playhead into two picks of the same chapter (each ≥ 2 s); either piece can be deleted, moved or trimmed; picks carry their own `id` | Owner: "ctrl + s for splitting a clip … so if I split a clip, I can delete one of the splits" | Interview | 2026-11-28 |
| D31 | No arrow buttons | The four 1 s buttons are removed; offset fields stay | Owner: "I will just drag if I need"; "offset I'm iffy with but keep it" | Interview | 2026-11-28 |
| D28 | Wording | "start / end", "starts 1 s earlier / ends 2 s later", fields labelled "offset (s)"; handles visible on hover, 12 px | UX review #4, #5, #7 | Interview | 2026-11-28 |

## Dependency Graph & Implementation Order

```
T1 join map v2 hook block + write picks ──┬──> T3 PC API + MCP (hook get/set/reset) ──> T4 web HookLane + inspector + badge + seek map
T2 hook_edits.py (clamp, offsets, digest) ─┤
T1 + T2 ───────────────────────────────────┴──> T5 story-join --apply-edits (managed extend, re-render, re-join from map)
T3..T5 ────────────────────────────────────────> T6 docs + vault + laptop handoff
```

1. **T1 Join map v2** (laptop side, pure): `JoinHook`/`JoinHookPick`, `schema_version`, `write_join_files(..., hook=...)`, recording-second windows and events; test on fixture plans.
2. **T2 `story/hook_edits.py`**: models for the edits file (picks with `at_s`), `clamp_pick`, `settle`, `apply_offsets`, `base_digest`, pure tests.
3. **T3 PC API + MCP + registry**: `GET/PUT/DELETE /narration/joined/{key}/hook` + `GET …/storyboard`, `JoinedStoryTarget.hook()`, tools `hook_picks_get/set/reset`, `storyboard_get`.
4. **T4 Web**: `lib/hookTrack.ts` (+ vitest: settle, extend eats space then pushes, start-extend stops at the previous clip, trim, delete, move/snap/drop, add-to-hook placement, close gaps, seek map), `HookLane` (track: empty space, body drag with snap and push, Delete key), `HookInspector`, `StoryboardPanel` (JSON, write/apply/cancel), "add to the hook" on chapters, badge popover (undo, close the empty spaces, discard), preview line, `useHookPicks`; hook-region takes flagged on reorder.
5. **T5 `story-join --apply-edits`** + `story_join(apply_edits)`: digest check, managed `extend`, `story-render` of touched rounds, re-join from the map, fold offsets.
6. **T6 Docs**: `api/docs/narration.md`, story-join docs, `web/docs/features/timeline.md`, `knowledge/series/Story-join.md`, laptop handoff.
