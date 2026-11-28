# Clip Edits Phase 1 (hook clips) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the PC narrate page the owner trims, extends, moves, deletes and adds the hook clips of a joined video on a track; the change is a small edits file; `story-join --apply-edits <stem>` on the laptop re-joins from it.

**Architecture:** The join map (laptop output) gains a `hook` block that persists the picks with their base windows in recording seconds, events and track positions. A PC-owned `<stem>.edits.json` holds the pick list with `at_s` positions and net offsets. Pure track maths lives once in Python (`story/hook_edits.py`) and once in TypeScript (`lib/hookTrack.ts`); the narration API exposes `GET/PUT/DELETE …/hook` and `GET …/storyboard`; the web adds a `HookLane`, a `HookInspector`, a `StoryboardPanel` and a changes badge; the laptop CLI applies the file.

**Tech Stack:** FastAPI + pydantic v2 + ffmpeg (api, `uv`), FastMCP (mcp), React 19 + TanStack Query 5 + vitest (web, `bun`).

**Spec:** `docs/specs/spec-clip-edits.md` (phase 1 sections §2, §4, §6, §7; decisions D1–D31). The mock everything mirrors: https://claude.ai/artifact/M46eiu7XEc5XmSrAKYy6C3 (source in this session's scratchpad `clip-edits-playground.html`; its `settle`, `applyTrim`, `dropAt`, `addToHook`, `closeGaps` are the reference behaviour).

## Global Constraints

- Branch `feat/clip-edits` (off `feat/narration-v2`); never commit to `master`; push the branch. Never read `api/.env`.
- Every new mutating endpoint gets an MCP tool in `mcp/src/ai_video_editor_mcp/server.py` (docstring as a user manual) and a line in `api/src/ai_video_editor/registry.py` `_PLUMBING_TOOLS` (`test_registry` fails otherwise); regenerate `uv run python -m ai_video_editor.registry --write`.
- Hook constants stay: `HOOK_LEAD_S 6.0`, `HOOK_PLAY_TAIL_S 2.0`, `HOOK_PLAY_MAX_S 14.0`, `HOOK_MIN_S 2.0`; new: `NEIGHBOUR_GAP_S = 0.5`, `SNAP_S = 0.4`, `SWIPE_S` (existing 0.5).
- Offsets: `start_s` > 0 = starts earlier, `end_s` > 0 = ends later, both net from the base window; `at_s` = position on the editor track in seconds; clips never overlap after `settle`.
- Chapter id: `"<round>:<play_id>"` for play chapters, `"<round>:<kind>@<rec_start_s:.1f>"` otherwise.
- Times in the map's hook block are **recording seconds**; game clock = recording − `offset_s` (shown only).
- Edits file: `WORKSPACE/story/<stem>.edits.json`, `schema_version 1`, `base_digest = "sha256:" + 12 hex` over the map's hook picks `(chapter_id, rec_start_s, rec_end_s)` sorted by chapter_id.
- Join map `schema_version 2` when written with a hook block; a map without `hook` is v1 and the page shows markers only.
- Docs in the same commit as the code they describe. `ruff check` / `ruff format` clean; `tsc -b` clean; vitest green.
- Web copy: "start"/"end" never "head"/"tail"; "empty space" never "slot"/"gap" in UI text; the badge reads `N hook change(s) · laptop applies`.
- Every pick has its own `id` (8 hex chars, `uuid4().hex[:8]`); the same `chapter_id` may appear in several picks (a split clip). Keyboard on the narrate page: `Delete`/`Backspace` delete the selected hook clip, `Ctrl+Z` undoes the last hook change, `Ctrl+Shift+Z` or `Ctrl+Y` redoes, `Ctrl+S` splits the selected hook clip at the playhead (always `preventDefault`); none of these fire while typing in an input or textarea or while recording.

## Review Focus

1. Picks `PUT` with overlapping `at_s` (owner typed JSON): must be settled (pushed right, never 422). Test in Task 2 (`test_settle_pushes_overlaps`) and Task 3 (`test_put_overlapping_picks_is_settled`).
2. A `PUT` naming a `chapter_id` the map does not know: 422 whose message contains the id. Task 3 (`test_put_unknown_chapter_422`).
3. A joined video whose map has no `hook` block (rendered before this feature): `GET …/hook` 404 with "no hook data"; the page keeps the markers lane. Task 3 (`test_get_hook_404_for_v1_map`), Task 4 (`HookLane` not rendered when `hook` query is 404).
4. Offsets that would run the window into the neighbouring chapter: clamped to 0.5 s short, the response carries the clamped values. Task 2 (`test_clamp_offsets_stops_at_neighbours`).
5. `--apply-edits` when the map changed under the edits (digest differs): refuses with a message naming both digests; `--force-edits` proceeds. Task 5 (`test_apply_edits_refuses_stale_digest`).

---

### Task 1: Join map v2 — hook block, chapter ids, event times

**Files:**
- Modify: `api/src/ai_video_editor/story/join_map.py` (models + `build_join_map`)
- Modify: `api/src/ai_video_editor/story/bookends.py` (`HookPick` offsets, `hook_window_rec`)
- Modify: `api/src/ai_video_editor/story/join.py` (`hook_clips` applies offsets; `join_stories`/`write_join_files` pass the hook block and options)
- Test: `api/tests/test_story_join_map.py`, `api/tests/test_story_bookends.py`, `api/tests/test_story_join.py`

**Interfaces:**
- Produces in `join_map.py`:
  - `def chapter_id(round_name: str, chapter: Chapter) -> str`
  - `class JoinHookEvent(BaseModel): rec_s: float; kind: Literal["kill", "death"]`
  - `class JoinHookPick(BaseModel): id: str; chapter_id: str; round: str; chapter_index: int; rec_start_s: float; rec_end_s: float; offset_s: float; start_s: float = 0.0; end_s: float = 0.0; at_s: float; video_start_s: float; video_end_s: float; events: list[JoinHookEvent] = []; chapter_rec_start_s: float; chapter_rec_end_s: float; text: str = ""`
  - `class JoinHook(BaseModel): mode: Literal["tease", "play"] = "tease"; title: str | None = None; picks: list[JoinHookPick] = []`
  - `class JoinOptions(BaseModel): subscribe_after: int | None = 1; outro: bool = False; music_start_s: float = 0.0`
  - `JoinChapter` gains `kill_times_s: list[float] = []` and `death_times_s: list[float] = []` (game clock, rounded to 0.1)
  - `JoinMap` gains `schema_version: int = 1`, `hook: JoinHook | None = None`, `options: JoinOptions = JoinOptions()`
  - `build_join_map(..., hook: JoinHook | None = None, options: JoinOptions | None = None)` sets `schema_version=2` whenever `hook` is given
  - `def hook_block(rounds, plans, picks: list[HookPick], clips: list[HookClip], starts_s: list[float], durations_s: list[float], offsets_s: list[float], *, mode, title) -> JoinHook` (pure; `id` = `uuid4().hex[:8]` per pick, or the pick's own id when `HookPick.id` is set; `at_s` = cumulative clip lengths without the swipe; `video_start_s = starts_s[i]`, `video_end_s = starts_s[i] + durations_s[i]`; events = the chapter's kill/death times inside the chapter)
- Produces in `bookends.py`:
  - `HookPick` gains `start_s: float = 0.0`, `end_s: float = 0.0`, `id: str | None = None`
  - `def hook_window_rec(plan: StoryPlan, chapter_index: int, mode: HookMode = "tease") -> tuple[float, float]` (recording seconds; the existing `hook_window` becomes a wrapper that converts both ends with `video_second`)
  - `HookClip` gains `rec_start_s: float` and `rec_end_s: float`
- Produces in `join.py`: `hook_clips(rounds, picks)` applies `p.start_s`/`p.end_s` to the recording window (clamped to the chapter's `[start_s, end_s]`, minimum `HOOK_MIN_S`), `join_stories(..., music_start_s, subscribe_after, ...)` passes `hook_block(...)` and `JoinOptions(...)` to `write_join_files(..., hook=, options=)`.

- [ ] **Step 1: Failing tests for `chapter_id` and the map's hook block**

```python
# tests/test_story_join_map.py
def test_chapter_id_uses_play_id_or_kind_and_start():
    assert chapter_id("r1", Chapter(title="x", start_s=240.6, end_s=260.7, kind="lane", play_id="p01")) == "r1:p01"
    assert chapter_id("r1", Chapter(title="The opening", start_s=50.0, end_s=204.1, kind="early")) == "r1:early@50.0"

def test_map_v2_carries_hook_block_and_event_times():
    jm = _map(hook=JoinHook(mode="play", title="t", picks=[JoinHookPick(
        id="a1b2c3d4", chapter_id="r1:p01", round="r1", chapter_index=1, rec_start_s=300.0, rec_end_s=312.0,
        offset_s=-10.0, at_s=0.0, video_start_s=0.0, video_end_s=12.0,
        events=[JoinHookEvent(rec_s=310.0, kind="death")], chapter_rec_start_s=300.0, chapter_rec_end_s=330.0)]))
    assert jm.schema_version == 2 and jm.hook.picks[0].chapter_id == "r1:p01" and len(jm.hook.picks[0].id) == 8
    assert jm.games[0].chapters[0].kill_times_s == [110.0]   # rec 100 - offset -10
    assert JoinMap.model_validate_json(jm.model_dump_json()).hook == jm.hook

def test_map_without_hook_is_v1():
    assert _map().schema_version == 1 and _map().hook is None
```
(`_map` gains a `hook=None` passthrough.)

- [ ] **Step 2: Run them** — `cd api && uv run pytest tests/test_story_join_map.py -q` → FAIL (ImportError / AttributeError).
- [ ] **Step 3: Implement the models, `chapter_id`, the two `JoinChapter` lists and the `build_join_map` parameters** in `join_map.py`.
- [ ] **Step 4: Run** → PASS. Existing tests in the file must still pass (the `hook` insert and `intro_s` behaviour is unchanged).
- [ ] **Step 5: Failing tests for `hook_window_rec` and offsets**

```python
# tests/test_story_bookends.py
def test_hook_window_rec_play_mode_matches_video_window():
    plan = _plan(_ch(100, 140, kills=[120, 128]))
    assert hook_window_rec(plan, 0, "play") == (114.0, 130.0)
    assert hook_window(plan, 0, 0.0, "play") == (14.0, 30.0)

def test_hook_pick_carries_offsets():
    p = parse_pick("3:2")
    assert (p.start_s, p.end_s) == (0.0, 0.0)
    assert dataclasses.replace(p, end_s=2.0).end_s == 2.0
```

```python
# tests/test_story_join.py
def test_hook_clips_apply_offsets_inside_the_chapter(ws, monkeypatch):
    plan = StoryPlan(chapters=[Chapter(title="c", start_s=100, end_s=140, kind="fight", kill_times_s=[120, 128], play_id="p00")], total_s=40, mic_stream=None, audio="mixed", cards=False)
    monkeypatch.setattr(join, "_load_plan", lambda r: plan)
    clip = join.hook_clips(["r1"], [HookPick(1, 1, "", mode="play", start_s=3.0, end_s=20.0)])[0]
    assert (clip.rec_start_s, clip.rec_end_s) == (111.0, 140.0)   # end clamped to the chapter
    assert (clip.start_s, clip.end_s) == (11.0, 40.0)

def test_join_writes_hook_block_and_options(ws, fakes, tmp_path, monkeypatch):
    # like test_hook_and_outro_wrap_the_games, but assert write_join_files got hook=JoinHook(picks with at_s [0.0, len1]) and options.outro True
```
- [ ] **Step 6: Run** → FAIL.
- [ ] **Step 7: Implement** `hook_window_rec` (move the existing logic to recording seconds; `hook_window` converts with `video_second`), the `HookPick` fields, `HookClip.rec_*`, the offset application in `hook_clips`, `hook_block()` and the plumbing in `join_stories` → `write_join_files(hook=, options=)` → `build_join_map(hook=, options=)`.
- [ ] **Step 8: Run** `uv run pytest tests/test_story_join_map.py tests/test_story_bookends.py tests/test_story_join.py tests/test_story_router.py -q` → PASS; `uv run ruff check src/ && uv run ruff format --check src/` clean.
- [ ] **Step 9: Commit** `feat(story-join): join map v2 with the hook block (picks, recording windows, events, options)`.

---

### Task 2: `story/hook_edits.py` — the edits file and the track maths (pure)

**Files:**
- Create: `api/src/ai_video_editor/story/hook_edits.py`
- Test: `api/tests/test_story_hook_edits.py`

**Interfaces:**
- Consumes: `JoinMap`, `JoinHook`, `JoinHookPick` (Task 1), `HOOK_MIN_S` from `bookends`.
- Produces:
  - `NEIGHBOUR_GAP_S = 0.5`, `EDITS_SUFFIX = ".edits.json"`
  - `class EditPick(BaseModel): id: str = Field(default_factory=lambda: uuid4().hex[:8]); chapter_id: str; at_s: float = 0.0; start_s: float = 0.0; end_s: float = 0.0`
  - `class EditHook(BaseModel): mode: str = "play"; title: str | None = None; picks: list[EditPick] = []`
  - `class HookEdits(BaseModel): schema_version: int = 1; video: str; base_digest: str; hook: EditHook`
  - `def base_digest(hook: JoinHook) -> str`
  - `def edits_path(video_stem: str) -> Path` (`WORKSPACE/story/<stem>.edits.json`)
  - `def load_edits(video_stem: str) -> HookEdits | None`, `def save_edits(e: HookEdits) -> Path`, `def delete_edits(video_stem: str) -> None`
  - `def base_len(p: JoinHookPick) -> float`
  - `def limits(jm: JoinMap, p: JoinHookPick) -> tuple[float, float]` (recording seconds the window may reach: previous chapter end + 0.5 .. next chapter start − 0.5, from `jm.games[].chapters` and `p.offset_s`; 0 / +inf at the ends)
  - `def clamp_offsets(jm: JoinMap, p: JoinHookPick, start_s: float, end_s: float, keep: Literal["start", "end"]) -> tuple[float, float]` (window within `limits`, length ≥ `HOOK_MIN_S`; `keep="start"` moves the end when too short)
  - `def settle(picks: list[EditPick], lengths: dict[str, float]) -> list[EditPick]` (sorted by `at_s`; each pick pushed to the previous one's end when overlapping; `at_s ≥ 0`; rounded to 0.1)
  - `def validate_picks(jm: JoinMap, picks: list[EditPick]) -> list[EditPick]` (unknown chapter → `ValueError("unknown chapter_id: …")`, duplicate pick `id` → `ValueError`; the same chapter in several picks is allowed (a split); offsets clamped, then settled)
  - `def split_pick(jm: JoinMap, picks: list[EditPick], pick_id: str, at_track_s: float) -> list[EditPick]` (the pick that holds `at_track_s` becomes two picks of the same chapter: left keeps `id`, `at_s`, `start_s` and gets `end_s = (rec_t − base_rec_end)`; right gets a new id, `at_s = left.at_s + left_len`, `start_s = (base_rec_start − rec_t)`, the old `end_s`; `rec_t = rec_start_of_pick + (at_track_s − at_s)`; both pieces ≥ `HOOK_MIN_S` else `ValueError("too close to the edge to split")`)
  - `class HookPickView(BaseModel): chapter_id, round, chapter_index, label: str (chapter title up to the first comma), at_s, start_s, end_s, len_s, rec_start_s, rec_end_s, base_rec_start_s, base_rec_end_s, offset_s, events, chapter_rec_start_s, chapter_rec_end_s, limit_lo_rec_s, limit_hi_rec_s (from `limits()`), video_start_s: float | None, video_end_s: float | None, pending: bool, beyond_chapter_s: float`
  - `class HookView(BaseModel): mode: str; title: str | None; rendered_intro_s: float; track_end_s: float; intro_after_s: float (Σ len − n·SWIPE_S); picks: list[HookPickView]; base_digest: str; edits_digest: str | None; stale_edits: bool; dirty: bool; empty_space_s: float; laptop_command: str; edits_file: str`
  - `def hook_view(jm: JoinMap, video_stem: str, edits: HookEdits | None) -> HookView` (edits None → the rendered picks at their `at_s`, `dirty False`)
  - `def fresh_edits(jm: JoinMap, video_stem: str) -> HookEdits` (the rendered picks as an edits file)

- [ ] **Step 1: Failing tests**

```python
# tests/test_story_hook_edits.py  (fixture: _jm() = a JoinMap with one game of 3 chapters at rec 100-140 (p00, kills 120,128), 160-180 (early@160.0), 200-230 (p02), offset 0; hook block with p00 and p02 in play mode, at_s 0 and 16)
def test_base_digest_depends_on_ids_and_windows_only(): ...  # changing jm.title keeps it; changing rec_end_s changes it; "sha256:" + 12 hex
def test_settle_pushes_overlaps():
    out = settle([EditPick(chapter_id="a", at_s=0), EditPick(chapter_id="b", at_s=5), EditPick(chapter_id="c", at_s=30)], {"a": 14, "b": 14, "c": 14})
    assert [p.at_s for p in out] == [0.0, 14.0, 30.0]
def test_settle_sorts_by_at_and_clamps_negative(): ...
def test_clamp_offsets_stops_at_neighbours():
    jm = _jm(); p = jm.hook.picks[0]        # window 114-130 inside chapter 100-140, next chapter at 160
    assert clamp_offsets(jm, p, 0.0, 40.0, "start") == (0.0, 29.5)   # 130 + 29.5 = 159.5
    assert clamp_offsets(jm, p, 30.0, 0.0, "end") == (14.0, 0.0)     # cannot start before 100
def test_clamp_offsets_keeps_min_length(): ...   # end_s = -20 with keep="start" → window 114..116
def test_validate_picks_rejects_unknown_chapter_and_duplicate_pick_ids(): ...  # message contains the id; the same chapter twice with different ids passes
def test_split_pick_makes_two_pieces_of_the_same_chapter():
    jm = _jm(); p = jm.hook.picks[0]              # base window 114-130 at at_s 0
    out = split_pick(jm, fresh_edits(jm, "joined_x").hook.picks, p.id, 6.0)
    left, right = out[0], out[1]
    assert (left.id, left.at_s, left.start_s, left.end_s) == (p.id, 0.0, 0.0, -10.0)      # 114..120
    assert (right.chapter_id, right.at_s, right.start_s, right.end_s) == (p.chapter_id, 6.0, -6.0, 0.0)  # 120..130
    assert right.id != left.id and len(right.id) == 8
def test_split_pick_too_close_to_an_edge_raises(): ...   # at 1.0 s → ValueError
def test_hook_view_marks_pending_and_sums_the_track():
    v = hook_view(_jm(), "joined_x", HookEdits(video="joined_x", base_digest=base_digest(_jm().hook), hook=EditHook(picks=[EditPick(chapter_id="r1:p00", at_s=0, end_s=2.0), EditPick(chapter_id="r1:p02", at_s=30)])))
    assert v.picks[0].pending and v.picks[0].len_s == 18.0 and v.picks[1].at_s == 30.0
    assert v.track_end_s == 30.0 + base_len(_jm().hook.picks[1]) and v.empty_space_s == 12.0 and v.dirty
def test_hook_view_stale_when_digest_differs(): ...
def test_edits_round_trip_on_disk(tmp_path, monkeypatch): ...  # save, load, delete
```
- [ ] **Step 2: Run** `uv run pytest tests/test_story_hook_edits.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `hook_edits.py` per the interfaces. `laptop_command = f"story-join --apply-edits {video_stem}"`.
- [ ] **Step 4: Run** → PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(story): hook edits file and pure track maths (settle, clamp, view)`.

---

### Task 3: PC API, MCP tools, registry

**Files:**
- Modify: `api/src/ai_video_editor/narration/targets.py` (`NarrationTarget` protocol + `TargetDefaults` gain `join_map(key) -> JoinMap | None` returning `None`; `JoinedStoryTarget.join_map` = the existing `_join_map`)
- Create: `api/src/ai_video_editor/narration/hook_service.py`
- Modify: `api/src/ai_video_editor/routers/narration.py` (routes + docstring list)
- Modify: `mcp/src/ai_video_editor_mcp/server.py`, `api/src/ai_video_editor/registry.py` (+ regenerate `.claude/skills/editing-expert/arsenal.md`)
- Test: `api/tests/test_narration_hook_router.py` (new), `mcp/tests/test_server.py`, `api/tests/test_registry.py` (existing, must stay green)

**Interfaces:**
- Consumes: Task 2 (`hook_view`, `validate_picks`, `load_edits`, `save_edits`, `delete_edits`, `fresh_edits`, `base_digest`, `HookView`, `EditPick`).
- Produces (`hook_service.py`):
  - `class HookUnavailable(LookupError)` (map missing or v1)
  - `class StaleEdits(RuntimeError)`
  - `def get_hook(target, key) -> HookView`
  - `def set_hook_picks(target, key, picks: list[EditPick], *, mode: str | None = None, title: str | None = None) -> HookView` (loads the map; refuses with `StaleEdits` when an edits file exists whose digest ≠ the map's; validates; writes the file with the map's digest)
  - `def reset_hook(target, key) -> HookView`
  - `class StoryboardChapter(BaseModel): id, title, kind, game_start_s, game_end_s, video_start_s, kill_times_s, death_times_s, in_hook: bool`
  - `class StoryboardGame(BaseModel): round, label, chapters`
  - `class Storyboard(BaseModel): schema_version, video, title, rendered_intro_s, intro_after_s, outro_s, hook: HookView, games: list[StoryboardGame]`
  - `def storyboard(target, key) -> Storyboard`
- Routes (all under `/api/v1/narration/{kind}/{key}`): `GET /hook` → `HookView` (404 "no hook data for this video" on `HookUnavailable`), `PUT /hook` body `{"picks": [EditPick…], "mode"?: str, "title"?: str}` → `HookView` (422 on `ValueError`, 409 on `StaleEdits`), `DELETE /hook` → `HookView`, `GET /storyboard` → `Storyboard` (404 as above). A split is a `PUT` of the new list (the web computes it with `split_pick`'s mirror); no split endpoint.
- MCP tools: `hook_picks_get(key)`, `hook_picks_set(key, picks: list[dict], mode: str | None = None, title: str | None = None)`, `hook_picks_reset(key)`, `storyboard_get(key)` — all `kind="joined"`; docstrings explain `at_s`, `start_s`/`end_s` signs, empty space, and that the laptop applies with `story-join --apply-edits`.
- Registry: the four names in `_PLUMBING_TOOLS`.

- [ ] **Step 1: Failing router tests** (fixture: a sandbox `WORKSPACE/story/joined_t.mp4` + `joined_t.join.json` written from Task 2's `_jm()` with `video` pointing at the mp4; use the real `joined` target)

```python
def test_get_hook_returns_rendered_picks(client, joined_map): r = client.get(BASE + "/hook"); assert r.status_code == 200 and r.json()["dirty"] is False and len(r.json()["picks"]) == 2
def test_get_hook_404_for_v1_map(client, joined_map_v1): assert client.get(BASE + "/hook").status_code == 404
def test_put_overlapping_picks_is_settled(client, joined_map):
    r = client.put(BASE + "/hook", json={"picks": [{"chapter_id": "r1:p00", "at_s": 0}, {"chapter_id": "r1:p02", "at_s": 3}]})
    assert r.status_code == 200 and [p["at_s"] for p in r.json()["picks"]] == [0.0, 16.0]
    assert (ws / "story" / "joined_t.edits.json").is_file()
def test_put_unknown_chapter_422(client, joined_map): r = client.put(..., json={"picks": [{"chapter_id": "r1:nope"}]}); assert r.status_code == 422 and "r1:nope" in r.json()["detail"]
def test_put_stale_edits_409_and_delete_clears(client, joined_map): ...  # write an edits file with digest "sha256:000000000000", PUT → 409; DELETE → 200, dirty False; PUT → 200
def test_put_accepts_the_same_chapter_twice_after_a_split(client, joined_map): ...  # two picks, same chapter_id, different ids → 200, two picks back
def test_storyboard_lists_games_and_in_hook(client, joined_map): sb = client.get(BASE + "/storyboard").json(); assert sb["games"][0]["chapters"][0]["in_hook"] is True and sb["hook"]["picks"]
```
- [ ] **Step 2: Run** → FAIL (404 routes).
- [ ] **Step 3: Implement** `hook_service.py`, the target protocol method, the routes (add the four lines to the router's docstring table).
- [ ] **Step 4: Run** `uv run pytest tests/test_narration_hook_router.py tests/test_narration_router.py tests/test_narration_targets.py -q` → PASS.
- [ ] **Step 5: MCP tools + registry**: add the four tools next to `narration_relock` using `_narration_url("joined", key, "/hook")`; add a `mcp/tests/test_server.py` test `test_hook_picks_set_puts_the_list` asserting `("PUT", "/api/v1/narration/joined/j1/hook")` and the JSON body; add the names to `_PLUMBING_TOOLS`; run `cd api && uv run python -m ai_video_editor.registry --write`.
- [ ] **Step 6: Run** `cd mcp && uv run pytest -q` and `cd api && uv run pytest tests/test_registry.py -q` → PASS.
- [ ] **Step 7: Commit** `feat(narration): hook picks API (get/put/delete), storyboard, MCP tools`.

---

### Task 4: Web — HookLane track, inspector, storyboard panel, badge

**Files:**
- Create: `web/src/lib/hookTrack.ts`, `web/src/lib/__tests__/hookTrack.test.ts`, `web/src/lib/history.ts`, `web/src/lib/__tests__/history.test.ts`
- Create: `web/src/hooks/useHookPicks.ts`, `web/src/hooks/useStoryboard.ts`
- Create: `web/src/components/narration/timeline/HookLane.tsx`, `web/src/components/narration/HookInspector.tsx`, `web/src/components/narration/StoryboardPanel.tsx`, `web/src/components/narration/HookChangesBadge.tsx`
- Modify: `web/src/types/narration.ts` (mirror `HookView`, `HookPickView`, `EditPick`, `Storyboard*`), `web/src/api/narration.ts` (`getHook`, `putHook`, `resetHook`, `getStoryboard`), `web/src/components/narration/timeline/lane.ts` (`LANE_HEIGHT.hook = 56`), `web/src/components/narration/timeline/Timeline.tsx` (optional hook lane between frames and markers; `shiftS` for markers after the rendered intro and for the frame strip), `web/src/components/narration/timeline/FrameStripLane.tsx` and `MarkersLane.tsx` (`shiftS?: number`), `web/src/pages/NarrationPage.tsx` (wiring; the right column shows `StoryboardPanel` when nothing is selected, `HookInspector` for a hook clip, `TakeInspector` for a take)

**Interfaces:**
- Consumes: the API of Task 3.
- Produces (`lib/hookTrack.ts`, pure, mirrors `hook_edits.py` and the mock):
  - `interface TrackClip { id: string; chapter_id: string; at_s: number; start_s: number; end_s: number }` (a new id is `crypto.randomUUID().replace(/-/g, "").slice(0, 8)`)
  - `splitAt(clips, clip, tTrack, view): TrackClip[]` (mirror of `split_pick`: left keeps the id, right gets a new one; throws when a piece would be shorter than 2.0 s)
  - `lib/history.ts`: `createHistory<T>(limit = 100)` with `push(state)`, `undo(): T | null`, `redo(): T | null`, `canUndo`, `canRedo` (pure, tested: push/undo/redo order, redo cleared by a new push, limit)
  - `settle(clips: TrackClip[], lens: (c: TrackClip) => number): TrackClip[]`
  - `clampOffsets(p: HookPickView, start_s: number, end_s: number, keep: "start" | "end"): { start_s; end_s }` (limits = the view's `limit_lo_rec_s`/`limit_hi_rec_s`)
  - `applyTrim(clips, clip, edge: "start" | "end", start_s, end_s, view): TrackClip[]` (end: in place then settle; start: `at_s` moves left into empty space, stops at the previous clip's end)
  - `dropAt(clips, clip, wantAt): number` (snap within `SNAP_S = 0.4` to neighbour edges; onto another clip → before/after by half; then settle)
  - `addToHook(clips, pick, lens): TrackClip[]` (first empty space that fits, else the end)
  - `closeGaps(clips, lens): TrackClip[]`
  - `layout(clips, view): { hooks: {clip, vs, ve, len, n}[]; gaps: {vs, ve}[]; trackEnd; introAfter; shift }` with `shift = trackEnd − view.rendered_intro_s`
  - `seekTarget(t: number, lay, view, storyboard?): number | null` (hook seconds inside a clip's rendered window → that pick's `video_start_s + (t − vs)`; gained seconds → the chapter's `video_start_s + (rec − chapter_rec_start_s)` when the storyboard is loaded, else null; after the track → `t − shift`)
  - `changes(view): { text: string; undo: (clips) => TrackClip[] }[]` (trim lines, deleted, added, re-ordered, empty spaces)

- [ ] **Step 1: Failing vitest** `hookTrack.test.ts` with cases: settle pushes overlaps; extend end eats empty space then pushes the next clip; extend start stops at the previous clip and grows right; trim leaves space; dropAt snaps to a neighbour edge, lands before/after a clip by half; addToHook fills the first fitting space; closeGaps; splitAt makes two pieces of one chapter and refuses near an edge; layout shift; seekTarget for the three regions. `history.test.ts`: push/undo/redo, redo cleared by a new push, limit. Run `cd web && bun run test` (or `./node_modules/.bin/vitest run`) → FAIL.
- [ ] **Step 2: Implement `hookTrack.ts`** → tests PASS.
- [ ] **Step 3: Types + API + hooks**: `useHookPicks(target)` = query `["narration", kind, key, "hook"]` (`retry: false`; a 404 leaves `data` undefined and `isHookless` true) + mutations `setPicks(picks)` (optimistic via `settle` + `layout`, 409 → error line "the laptop re-joined since these changes: discard or re-apply") and `reset()`; `useStoryboard(target)` query `["narration", kind, key, "storyboard"]`.
- [ ] **Step 4: `HookLane.tsx`**: canvas-free DOM lane like the mock: clip spans (number, champion, numbers from the label), kill/death ticks, orange outline when `pending`, 12 px edge handles (pointer listeners on `window`, draft until release, one `setPicks` on release), body drag with ghost + snap, × when selected, Delete/Backspace + Escape; `Ctrl+Z` undo, `Ctrl+Shift+Z`/`Ctrl+Y` redo (history of pick lists kept in `useHookPicks`, every undo/redo is a `setPicks`), `Ctrl+S` split at the playhead (`preventDefault` always; only when a hook clip is selected and the playhead is inside it; the error line shows the "too close" message); empty spaces drawn dashed; a split chapter's pieces are labelled "part 1", "part 2" in track order; click = select (`onSelectHook(chapter_id)`) and seek through `seekTarget`. Disabled while recording.
- [ ] **Step 5: `HookInspector.tsx`** (name, game label, place on the track, shows/as rendered in game clock via `offset_s`, diff sentence, offset fields "offset (s)", reset, delete (leave space), the beyond-chapter note, a "storyboard" button, details with the edits JSON), `StoryboardPanel.tsx` (read-only `<pre>` of the storyboard; "write" → textarea; "apply" parses, takes `hook.picks` only, errors inline; "cancel"), `HookChangesBadge.tsx` (count, popover with `changes()` + undo, "close the empty spaces", "discard all changes", `laptop_command` and `edits_file` as text; a warning line when takes exist with `start_s < rendered_intro_s` and the hook is dirty).
- [ ] **Step 6: Wire `Timeline`/`NarrationPage`**: hook lane only when `hook.data`; `shiftS` to markers (those with `at_s ≥ rendered_intro_s`) and the frame strip; the page's preview line above the timeline: `intro 13.8 → 40.5 s (14.0 s of empty space here) · video … → …`; selecting a take clears the hook selection and vice versa.
- [ ] **Step 7: Verify** `cd web && ./node_modules/.bin/tsc -b && ./node_modules/.bin/vitest run` clean; open `http://localhost:5173/narrate/joined/<key>` against a v1 map (markers only, no errors) — a v2 map smoke needs Task 5's output or a hand-made map in the workspace: write one from Task 2's `_jm()` shape next to the real mp4 under a test key `joined_t` with the mp4 copied, and check: four spans, drag end +2 s, badge "1 hook change", edits file written, reload keeps it.
- [ ] **Step 8: Commit** `feat(web): hook track lane, inspector, storyboard JSON panel, changes badge`.

---

### Task 5: `story-join --apply-edits <stem>` (laptop) + `story_join(apply_edits=)`

**Files:**
- Create: `api/src/ai_video_editor/story/apply_edits.py`
- Modify: `api/src/ai_video_editor/story/requests.py` (`_Timed` gains `source: str | None = None`), `api/src/ai_video_editor/story/join.py` (`main` accepts `--apply-edits STEM` and `--force-edits`; `rounds` becomes `nargs="*"`), `api/src/ai_video_editor/routers/story.py` (`JoinBody.apply_edits: str | None = None`, `force_edits: bool = False`), `mcp/src/ai_video_editor_mcp/server.py` (`story_join(apply_edits: str | None = None, force_edits: bool = False)`)
- Test: `api/tests/test_story_apply_edits.py` (new), `api/tests/test_story_join.py`, `api/tests/test_story_requests.py`, `api/tests/test_story_router.py`, `mcp/tests/test_server.py`

**Interfaces:**
- Consumes: Task 1 (`JoinMap.hook/options`, `HookPick.start_s/end_s`, `join_stories`), Task 2 (`load_edits`, `base_digest`, `delete_edits`), `story.cli.render_round`, `requests.load_requests/save_requests`.
- Produces: `def apply_edits(video_stem: str, *, force: bool = False, say=print, render=render_round, join=join_stories) -> Path`:
  1. map = `JoinMap` from `WORKSPACE/story/<stem>.join.json`; `StoryRenderError` when missing, `schema_version < 2` or `hook is None`.
  2. edits = `load_edits(stem)`; `StoryRenderError("no edits file")` when None; when `edits.base_digest != base_digest(map.hook)` and not `force`: `StoryRenderError("edits were made on <a>, the map is <b>; re-join first or pass --force-edits")`.
  3. picks sorted by `at_s`; each resolved to a chapter: by `chapter_id` in the round's current `story.json` (`chapter_id()`), else by `rec_start_s` within 1.0 s of the map's `chapter_rec_start_s`; unresolved → listed in `say` and skipped.
  4. wanted recording window = base ± offsets; when it runs past `[chapter.start_s, chapter.end_s]`: one managed `Extend(at=<game clock of the chapter start + 0.5>, before_s=…, after_s=…, source="clip_edit")` replaces any `clip_edit` entry whose `at` is inside that chapter; save; `render(asset_id=plan.asset_id, round=round)` once per touched round.
  5. `join(rounds, default_out(rounds), subscribe_after=map.options.subscribe_after, title=map.title, hook=[HookPick(game=i+1, chapter=idx+1, text=p.text, mode=map.hook.mode, start_s=…, end_s=…)…], hook_title=map.hook.title, outro=map.options.outro, music_start_s=map.options.music_start_s)`; then `delete_edits(stem)`; return the mp4 path.
- CLI: `story-join --apply-edits joined_x [--force-edits]` (no rounds); HTTP `POST /story/join {"apply_edits": "joined_x"}` runs it in the job; MCP `story_join(apply_edits="joined_x")`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_story_apply_edits.py  (fixtures write a v2 map + edits file + story.json per round under a sandbox workspace; render/join are fakes recording their calls)
def test_apply_edits_refuses_stale_digest(ws): ...  # message names both digests; force=True proceeds
def test_apply_edits_reorders_and_passes_offsets(ws): ...  # picks at_s [30, 0] → join called with the second pick first, HookPick.end_s == 2.0
def test_apply_edits_extends_the_chapter_once_and_renders_that_round(ws): ...  # end_s past the chapter → one Extend(source="clip_edit") in requests.json, replaced on a second run, render called once with that round
def test_apply_edits_skips_unknown_chapters_and_says_so(ws): ...
def test_apply_edits_deletes_the_file_after_the_join(ws): ...
def test_apply_edits_passes_both_pieces_of_a_split_chapter(ws): ...  # two picks of one chapter → two HookPicks for the same (game, chapter) with their own offsets, in track order
# tests/test_story_requests.py
def test_extend_keeps_source_tag_round_trip(): ...
# tests/test_story_join.py
def test_cli_apply_edits_needs_no_rounds(ws, monkeypatch): ...  # main(["--apply-edits", "joined_x"]) calls apply_edits
```
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `apply_edits.py`, the `source` field, the CLI/HTTP/MCP plumbing. **Step 4: Run** the five test files + `mcp` tests → PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(story-join): --apply-edits re-joins from the owner's hook edits (managed extend, re-render, fold offsets)`.

---

### Task 6: Docs, vault, handoff

**Files:**
- Modify: `api/docs/narration.md` (hook picks API, edits file, storyboard), `api/CLAUDE.md` (6c: "edit the hook on the PC page, then `story-join --apply-edits`"), `web/docs/features/narration.md` + `web/docs/features/timeline.md` (hook lane, inspector, storyboard panel, badge, track rules), `knowledge/series/Story-join.md` (how the owner edits the hook; empty space closes on the laptop), `knowledge/series/Narration.md` (one line), `docs/handoff/laptop-2026-11-27.md` (the apply-edits step), `docs/specs/spec-clip-edits.md` (status line: built, commit range)

- [ ] **Step 1: Write the docs** (one section each; the vault page gets the owner's two quotes: "leave an empty space", "like an actual rendering software video model").
- [ ] **Step 2: Verify** `cd api && uv run pytest -q` (expect only the three known `.env`-dependent failures in the main checkout), `cd mcp && uv run pytest -q`, `cd web && ./node_modules/.bin/tsc -b && ./node_modules/.bin/vitest run`.
- [ ] **Step 3: Commit** `docs: hook clip edits on the narration timeline (phase 1)`.
