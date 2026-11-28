# Narration Editor v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the narration page into a timeline editor: punch-in takes (trim, split, nudge, mute, gain, record-over), four lanes (frames, markers, takes with waveforms, game audio with level regions), a Web Audio preview that matches the ffmpeg export, a "play-through" hook mode for story-join, and shift-and-relock when the hook changes length.

**Architecture:** Every edit is a number in `session.json` (non-destructive; webm files never rewritten). Pure cores carry the math on both sides: `narration/edits.py` (punch-in, trim, split, shift) and `narration/mix.py` (argv) in Python; `lib/timeline.ts`, `lib/takeEdits.ts`, `lib/waveform.ts`, `lib/mixSchedule.ts` in TypeScript. The frame strip and game-audio peaks are rendered once per video fingerprint into `<narration>/cache/`. The page's Web Audio graph mirrors the ffmpeg graph. The hook change is a story-join option rendered on the laptop.

**Tech Stack:** Python 3.10+, FastAPI, pydantic v2, ffmpeg/ffprobe, numpy, pytest; React 19, TanStack Query 5, TypeScript 5.7, Tailwind, Web Audio, vitest (new dev dependency, bun).

**Spec:** `docs/specs/spec-narration-v2.md` (D1–D14). Base: `docs/specs/spec-narration.md`.

## Global Constraints

- Branch `feat/narration-v2` (D14). Push the branch; never merge to master.
- Tests: `cd api && uv run pytest -q` (baseline 2393 collected) and `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`; `cd web && ./node_modules/.bin/tsc -b && bun run test`. Worktree runs use the main venv: `PYTHONPATH=<worktree>/api/src` with a temp `WORKSPACE_DIR`.
- Never read `api/.env`. No new secrets. Takes, caches, cues and `narrated.mp4` stay under the narration folder; nothing uploads anywhere (§5).
- All ffmpeg argv from validated pydantic values; no free text into filter strings (§5).
- Take minimum after any edit: 0.3 s (`MIN_TAKE_S`). Nudge `offset_s` ∈ [−3, 3]. `gain` ∈ (0, 4]. `level` ∈ [0, 2]. `game_gain` ∈ [0, 2]. Regions non-overlapping → 422 (§4.2, §4.5, §4.9).
- Hook play mode: `HOOK_PLAY_TAIL_S = 2.0`, `HOOK_PLAY_MAX_S = 14.0`, no freeze, 0.25 s audio fade; chapters without events keep the first `HOOK_LEAD_S` (§4.1).
- Frame strip: tiles 160 px wide, `count ≤ 240`, cached as `<narration>/cache/frames-<fp>.jpg` + `.json`; peaks: 10 Hz, `<narration>/cache/peaks-<fp>.json` (§4.3).
- Old `session.json` files load unchanged (every new field has a default) (§4.9).
- MCP: every new mutating endpoint gets a tool, classified in `registry._PLUMBING_TOOLS` (`test_registry` enforces it); regenerate `arsenal.md` with `uv run python -m ai_video_editor.registry --write`.
- Docs change in the same commit as the feature (root CLAUDE.md).

## Review Focus

1. A take whose `media_in_s + duration_s` exceeds the webm's real length (after a tail trim was undone by a head trim): ffmpeg `atrim` yields a shorter stream, never an error; the page must clamp `duration_s` to `decoded.duration − media_in_s`. Test in Task 6 (`takeEdits.trimTail` clamps) and Task 2 (argv uses `atrim=start=…:duration=…`, not `end=`).
2. Punch-in exactly at an existing take's boundary (`new.start == old.end`): nothing is trimmed and nothing is deleted. Test in Task 1.
3. A game region or camera region that ends past the video (`end_s > duration`): accepted (the enable expression just never fires past the end) but clamped to the video length by the page. Test in Task 3 (API accepts) and Task 8 (lane clamps on save).
4. Relock with `shift_s` negative larger than a take's start: the take clamps at 0 and shrinks? No: it keeps its duration and clamps `start_s` to 0 (the owner can trim after). Test in Task 1.
5. `frames` / `peaks` requested twice at once (two tabs): one render; the second caller waits on the lock file and returns the same file. Test in Task 4 with a fake renderer counting calls.

---

### Task 1: Models and pure take math

**Files:**
- Modify: `api/src/ai_video_editor/narration/models.py` (Take, AudioMix, NarrationSession; add GameAudioRegion, CameraRegion, TakeEdit, Marker, FrameStrip, Peaks)
- Create: `api/src/ai_video_editor/narration/edits.py`
- Test: `api/tests/test_narration_edits.py`, `api/tests/test_narration_store.py` (additive: old session loads)

**Interfaces:**
- Produces (models.py, additive):
  - `MIN_TAKE_S = 0.3`
  - `Take` + `media_in_s: float = Field(0.0, ge=0)`, `offset_s: float = Field(0.0, ge=-3, le=3)`, `gain: float = Field(1.0, gt=0, le=4)`, `muted: bool = False`; property `start_effective_s = start_s + offset_s`.
  - `class GameAudioRegion(BaseModel): id: str (KEY_PATTERN); start_s: float ge 0; end_s: float; level: float ge 0 le 2` with a model validator `end_s > start_s`.
  - `class CameraRegion(BaseModel): id, start_s, end_s, box: CameraBox` (same validator).
  - `AudioMix` + `game_gain: float = Field(1.0, ge=0, le=2)`.
  - `NarrationSession` + `game_regions: list[GameAudioRegion] = []`, `camera_regions: list[CameraRegion] = []`, `pinned_intro_s: float | None = None`.
  - `class TakeEdit(BaseModel)`: every field optional (`start_s, duration_s, media_in_s, offset_s, gain, muted`), same bounds as Take.
  - `class Marker(BaseModel): at_s: float; label: str; kind: Literal["chapter","game","cue","insert"]`.
  - `class FrameStrip(BaseModel): sprite_url: str; every_s: float; tile_w: int; tile_h: int; cols: int; count: int`.
  - `class Peaks(BaseModel): rate_hz: int; peaks: list[float]`.
- Produces (edits.py, pure, no I/O):
  - `class TakeTooShortError(ValueError)`, `class OverlapError(ValueError)`
  - `apply_edit(take: Take, edit: TakeEdit) -> Take` (returns a copy; raises TakeTooShortError when `duration_s < MIN_TAKE_S`)
  - `split_take(take: Take, at_s: float, new_id: str) -> tuple[Take, Take]` (`at_s` in effective-time; raises ValueError unless `start_effective + MIN ≤ at_s ≤ end_effective − MIN`)
  - `punch_in(takes: list[Take], new: Take, new_id: Callable[[], str]) -> list[Take]` (returns the list with `new` appended and overlapped takes trimmed / dropped / split; a take that would end under `MIN_TAKE_S` is dropped)
  - `shift_session(session: NarrationSession, shift_s: float) -> NarrationSession` (takes' `start_s`, regions' `start_s`/`end_s` += shift, clamped at 0; a region fully below 0 is dropped)
  - `check_no_overlap(regions: Sequence[GameAudioRegion | CameraRegion]) -> None` (raises OverlapError; touching ends are fine)
  - `media_refcount(takes: list[Take], media: str) -> int`

- [ ] **Step 1: Write the failing tests** (`api/tests/test_narration_edits.py`)

```python
def _take(id, start, dur, media="m.webm", **kw) -> Take:
    return Take(id=id, start_s=start, duration_s=dur, media=media, created_at="2026-11-27T00:00:00+00:00", **kw)

def test_old_session_json_loads_with_defaults(): ...  # NarrationSession.model_validate_json of a v1 dict → media_in_s 0, offset_s 0, gain 1, muted False, game_regions []

def test_apply_edit_tail_trim_and_bounds():
    t = apply_edit(_take("a", 10, 20), TakeEdit(duration_s=5))
    assert (t.start_s, t.duration_s, t.media_in_s) == (10, 5, 0)
    with pytest.raises(TakeTooShortError): apply_edit(_take("a", 10, 20), TakeEdit(duration_s=0.2))

def test_split_shares_media_and_offsets_media_in():
    a, b = split_take(_take("a", 10, 20, media_in_s=1.5), at_s=18, new_id="b")
    assert (a.duration_s, b.start_s, b.media_in_s, b.duration_s) == (8, 18, 9.5, 12)
    assert a.media == b.media == "m.webm"
    with pytest.raises(ValueError): split_take(_take("a", 10, 20), at_s=10.1, new_id="b")

def test_punch_in_trims_deletes_and_splits():
    old = [_take("A", 0, 25), _take("B", 30, 10), _take("C", 43, 30), _take("D", 50, 5, media="d.webm")]
    out = punch_in(old, _take("N", 18, 37), new_id=iter(["x"]).__next__)   # N covers 18..55
    by = {t.id: t for t in out}
    assert by["A"].duration_s == 18 and "B" not in by and "D" not in by
    assert by["C"].start_s == 55 and by["C"].media_in_s == 12 and by["C"].duration_s == 18
    assert by["N"].start_s == 18

def test_punch_in_spanning_take_is_split():
    out = punch_in([_take("A", 0, 60)], _take("N", 20, 10), new_id=iter(["x"]).__next__)
    ids = sorted(t.id for t in out); assert ids == ["A", "N", "x"]
    tail = next(t for t in out if t.id == "x"); assert (tail.start_s, tail.media_in_s, tail.duration_s) == (30, 30, 30)

def test_punch_in_at_exact_boundary_touches_nothing():
    out = punch_in([_take("A", 0, 10)], _take("N", 10, 5), new_id=iter(["x"]).__next__)
    assert {t.id: (t.start_s, t.duration_s) for t in out} == {"A": (0, 10), "N": (10, 5)}

def test_shift_session_clamps_at_zero_and_moves_regions(): ...   # take at 1.0 shifted −3 → start 0, duration kept; region 2..4 → 0..1; region 0..1 dropped; +13.8 moves everything
def test_check_no_overlap_allows_touching_rejects_overlap(): ...
def test_media_refcount(): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/test_narration_edits.py -q` → FAIL (import errors).
- [ ] **Step 3: Implement the model fields and `edits.py`** with the signatures above. `punch_in` algorithm: for each old take overlapping `[n.start, n.end)` in effective time: if old fully inside → drop; if old starts before and ends inside → tail-trim to `n.start`; if old starts inside and ends after → head-trim to `n.end` (move `start_s` and `media_in_s` by the cut); if old spans both → tail-trim the original and append a head-trimmed copy with `new_id()`; drop any result under `MIN_TAKE_S`. Overlap is computed on `start_effective_s`; trims keep `offset_s`.
- [ ] **Step 4: Run** the file → PASS; run `uv run pytest -q` → all pass (store/export/mix tests unaffected).
- [ ] **Step 5: Commit** `feat(narration): v2 models and pure take edits (punch-in, split, trim, shift)`.

---

### Task 2: mix.py v2 (take chains, game regions, camera windows)

**Files:**
- Modify: `api/src/ai_video_editor/narration/mix.py` (TakeInput, `_audio_graph`, `_camera_graph`, `export_cmd` signature)
- Modify: `api/src/ai_video_editor/narration/export.py:105-131` (`_take_inputs`, `_camera_box` → windows)
- Create: `api/src/ai_video_editor/narration/camera_windows.py`
- Test: `api/tests/test_narration_mix.py` (additive), `api/tests/test_narration_camera_windows.py`, `api/tests/test_narration_export.py` (additive)

**Interfaces:**
- Consumes: Task 1 models.
- Produces:
  - `TakeInput(path, start_s, duration_s, has_camera, media_in_s: float = 0.0, gain: float = 1.0)` — `start_s` is already the effective start (export adds `offset_s`); muted takes are never built into a TakeInput.
  - `export_cmd(video, takes, out, *, mix: AudioMix, game_regions: list[GameAudioRegion] = [], camera_windows: list[CameraWindow] = [], rounded: bool = True) -> list[str]` (replaces `camera_box`).
  - `camera_windows.CameraWindow(NamedTuple): input_index: int; take: TakeInput; start_s: float; end_s: float; box: PixelBox`
  - `camera_windows.windows_for(takes: list[tuple[int, TakeInput]], regions: list[CameraRegion], default: CameraBox, game: str, frame: tuple[int,int], box_for: BoxFor) -> list[CameraWindow]`: for each camera take, the take span is cut at region edges; a region with `box.enabled == False` yields no window; outside regions the `default` box is used only when `default.enabled`.

- [ ] **Step 1: Failing tests**

```python
def test_take_chain_trims_gains_and_delays():
    cmd = export_cmd(VIDEO, [TakeInput(Path("a.webm"), 12.0, 5.0, False, media_in_s=1.5, gain=0.8)], OUT, mix=AudioMix())
    assert "[1:a]atrim=start=1.5:duration=5.0,asetpts=PTS-STARTPTS,volume=0.8,aformat=channel_layouts=stereo,adelay=12000|12000[t1]" in _graph(cmd)

def test_game_gain_and_regions_precede_the_sidechain():
    cmd = export_cmd(VIDEO, [_take("a.webm", 0, 1)], OUT, mix=AudioMix(game_gain=0.5),
                     game_regions=[GameAudioRegion(id="r1", start_s=10, end_s=20, level=0.3)])
    g = _graph(cmd)
    assert "[0:a]volume=0.5,volume=0.3:enable='between(t,10.0,20.0)'[game]" in g and "[game][keyp]sidechaincompress" in g

def test_camera_windows_one_overlay_each():   # two windows for one take → [cam1], [cam2], scale to each box, enable between each window
def test_windows_for_cuts_take_at_region_edges_and_hides():
    # take 10..40 (input 1), regions: 15..20 big box, 25..30 hidden; default enabled → windows (10,15,default),(15,20,big),(20,25,default),(30,40,default)
def test_windows_for_default_disabled_only_regions_show(): ...
def test_export_passes_effective_start_and_skips_muted(): ...   # export._take_inputs: offset added, muted dropped
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Each camera window chain: `[k:v]trim=start={a-take.start_s+take.media_in_s}:end={b-take.start_s+take.media_in_s},setpts=PTS-STARTPTS+{a}/TB,scale=…,crop=…[,mask][camN]` then `overlay={box.x}:{box.y}:enable='between(t,{a},{b})':eof_action=pass`. Video is re-encoded when any window exists, else `-c:v copy`. `export.py`: build `TakeInput(start_s=t.start_effective_s, media_in_s=t.media_in_s, gain=t.gain)` for unmuted takes; compute windows with `probe` + `box_for` only when at least one camera take exists.
- [ ] **Step 4: Run** `uv run pytest tests/test_narration_mix.py tests/test_narration_camera_windows.py tests/test_narration_export.py -q` → PASS; then the full suite.
- [ ] **Step 5: Commit** `feat(narration): mix v2 — per-take trim/gain, game regions, camera windows`.

---

### Task 3: Service, router and MCP for take edits, mix, regions, relock-shift

**Files:**
- Modify: `api/src/ai_video_editor/narration/service.py` (add_take `replace`, `edit_take`, `split_take`, `set_mix`, `set_game_regions`, `set_camera_regions`, `relock(shift_s)`, `build_view` `suggested_shift_s`, `pinned_intro_s`)
- Modify: `api/src/ai_video_editor/narration/targets.py` (protocol gains optional `intro_s(key) -> float | None` and `markers(key) -> list[Marker]`; `JoinedStoryTarget` implements both from `.join.json`; `StoryRoundTarget.markers` from `story.json` chapters via `suggest.placement.video_second`)
- Modify: `api/src/ai_video_editor/routers/narration.py` (PATCH takes, POST split, PUT mix, PUT game-regions, PUT camera-regions, relock body, upload `replace` query, GET markers)
- Modify: `mcp/src/ai_video_editor_mcp/server.py` (tools), `api/src/ai_video_editor/registry.py` (`_PLUMBING_TOOLS`), `.claude/skills/editing-expert/arsenal.md` (regenerated)
- Test: `api/tests/test_narration_router.py` (additive), `api/tests/test_narration_targets.py` (additive), `api/tests/test_registry.py` (passes after regen)

**Interfaces:**
- Consumes: Task 1 `edits.*`, models; Task 2 export signature.
- Produces (service):
  - `add_take(..., replace: bool = True) -> Take` (uses `punch_in`; deletes media of dropped takes when refcount hits 0)
  - `edit_take(ref, target, store, take_id, edit: TakeEdit) -> NarrationSession` (404 → `TakeNotFoundError`; too short → `TakeTooShortError`)
  - `split_take(ref, target, store, take_id, at_s) -> NarrationSession`
  - `set_mix(ref, target, store, mix: AudioMix)`, `set_game_regions(..., regions)`, `set_camera_regions(..., regions)` (overlap → `OverlapError`)
  - `relock(ref, target, store, shift_s: float = 0.0)`
  - `SessionView` + `suggested_shift_s: float | None`, `markers: list[Marker]`
  - HTTP: `PATCH /takes/{id}` (TakeEdit → SessionView; 404/422), `POST /takes/{id}/split?at_s=` (SessionView; 422), `PUT /mix`, `PUT /game-regions`, `PUT /camera-regions` (422 on overlap), `POST /relock` body `{"shift_s": 0.0}` (optional), `POST /takes?…&replace=true`, `GET /markers`.
  - MCP tools (all thin HTTP wrappers): `narration_session(kind, key)`, `narration_edit_take(kind, key, take_id, start_s=None, duration_s=None, media_in_s=None, offset_s=None, gain=None, muted=None)`, `narration_split_take(kind, key, take_id, at_s)`, `narration_delete_take(kind, key, take_id)`, `narration_set_mix(kind, key, narration_gain=None, game_gain=None, duck_ratio=None)`, `narration_set_game_regions(kind, key, regions: list[dict])`, `narration_set_camera_regions(kind, key, regions: list[dict])`, `narration_relock(kind, key, shift_s=0.0)`, `narration_export(kind, key, force=False)` (waits for the job).

- [ ] **Step 1: Failing router tests** (fixture style of the existing file; FakeTarget gains `intro_s` returning a settable value and `markers` returning `[Marker(at_s=13.8, label="Xayah", kind="game")]`):

```python
def test_patch_take_trims_and_rejects_too_short(client, video): ...      # PATCH duration_s=1.0 → 200 and session.takes[0].duration_s == 1.0; duration_s=0.1 → 422
def test_split_take(client, video): ...                                   # POST split?at_s=  → two takes share media; at_s outside → 422
def test_upload_replace_punches_overlaps(client, video): ...               # two uploads overlapping → first trimmed; replace=false → both kept
def test_put_mix_and_regions_and_overlap_422(client, video): ...
def test_relock_with_shift_moves_takes_and_pins_intro(client, video, fake_target): ... # fake intro 0 → 13.8: GET shows suggested_shift_s 13.8; POST relock {shift_s: 13.8} → take start +13.8, suggested_shift_s None
def test_markers_endpoint(client, video): ...
def test_delete_take_keeps_shared_media(client, video): ...               # after split, deleting one half leaves the webm
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** service functions, router routes, target methods (`JoinedStoryTarget.intro_s` reads `<key>.join.json` `intro_s`; `markers` = one `game` marker per game at `video_start_s` with its label, one `chapter` marker per chapter (`title` cut at the first comma), one `insert` per insert), MCP tools, add the nine tool names to `_PLUMBING_TOOLS`, regenerate `arsenal.md`.
- [ ] **Step 4: Run** `uv run pytest -q` (all, incl. `test_registry`) and `cd mcp && uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `feat(narration): take edits, mix and regions API, relock with shift, markers; MCP tools`.

---

### Task 4: Frame strip, peaks and cues

**Files:**
- Create: `api/src/ai_video_editor/narration/strips.py`
- Modify: `api/src/ai_video_editor/narration/store.py` (`cache_dir(ref)`, `cues_path(ref)`), `api/src/ai_video_editor/narration/service.py` (`markers` merges cues), `api/src/ai_video_editor/routers/narration.py` (GET frames, GET peaks)
- Test: `api/tests/test_narration_strips.py`, `api/tests/test_narration_router.py` (additive)

**Interfaces:**
- Produces (strips.py):
  - `FRAME_TILE_W = 160`, `MAX_TILES = 240`, `PEAKS_RATE_HZ = 10`
  - `strip_plan(duration_s: float) -> tuple[float, int]` → `(every_s, count)` with `every_s = max(1.0, ceil(duration/MAX_TILES))`, `count = ceil(duration/every_s)`
  - `sprite_cmd(video: Path, out: Path, every_s: float, count: int) -> list[str]` (ffmpeg `-i video -vf fps=1/{every_s},scale={W}:-2,tile={count}x1 -frames:v 1 -q:v 4 out`)
  - `peaks_cmd(video: Path) -> list[str]` (ffmpeg → `-f s16le -ac 1 -ar 8000 pipe:1`)
  - `peaks_from_pcm(raw: bytes, rate_hz: int = PEAKS_RATE_HZ, sample_rate: int = 8000) -> list[float]` (max-abs per window, normalised to the global max, rounded to 3 dp)
  - `ensure_frames(video, cache_dir, fingerprint, *, run, probe_size) -> FrameStripMeta` and `ensure_peaks(video, cache_dir, fingerprint, *, run_capture) -> Peaks`: idempotent per fingerprint, guarded by `<cache_dir>/.lock-<fp>` (`FileLock`-free: `os.open` with `O_CREAT|O_EXCL`, wait up to 60 s, then reuse the file).
  - Store: `cache_dir(ref) -> Path` (`<narration>/cache`), `cues_path(ref) -> Path` (`<narration>/cues.json`, format `[{"at_s": 13.8, "label": "X1 opening"}]`).
  - HTTP: `GET /frames` → `FrameStrip` (`sprite_url` under the narration URL `/cache/frames-<fp>.jpg`); `GET /peaks` → `Peaks`.

- [ ] **Step 1: Failing tests**: `strip_plan(792.9) == (4.0, 199)`, `strip_plan(60) == (1.0, 60)`; `sprite_cmd` contains `fps=1/4.0,scale=160:-2,tile=199x1`; `peaks_from_pcm` of 2 s of alternating silence/full-scale at 8 kHz → 20 values with `[0.0, 1.0, …]`; `ensure_frames` called twice with a counting fake `run` renders once; router `GET /frames` returns `count` and a `sprite_url` ending `cache/frames-<fp>.jpg`; `GET /markers` includes a `cue` marker from `cues.json`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the suite → PASS.
- [ ] **Step 5: Commit** `feat(narration): frame strip, game-audio peaks and cue markers`.

---

### Task 5: Hook play mode (story-join)

**Files:**
- Modify: `api/src/ai_video_editor/story/bookends.py` (`HookPick.mode`, `hook_window(..., mode)`, `hook_cmd` no-freeze branch, `HOOK_PLAY_TAIL_S`, `HOOK_PLAY_MAX_S`), `api/src/ai_video_editor/story/join.py` (`hook_clips` passes mode; CLI `--hook-mode`), `api/src/ai_video_editor/routers/story.py` (`JoinBody.hook_mode`), `mcp/src/ai_video_editor_mcp/server.py` (`story_join(hook_mode="tease")`), `knowledge/series/Story-join.md` (hook paragraph), `api/docs/` story-join doc if present
- Test: `api/tests/test_story_bookends.py` (additive), `api/tests/test_story_join.py` (CLI flag parsed)

**Interfaces:**
- `HookMode = Literal["tease", "play"]`; `HookPick(game, chapter, text="", mode: HookMode = "tease")`; `HookClip` + `freeze: bool`.
- `hook_window(plan, chapter_index, card_s, mode: HookMode = "tease") -> tuple[float, float]`; play: `end = min(stop, last_event + 2.0)`, `begin = max(start, first_event − HOOK_LEAD_S, end − HOOK_PLAY_MAX_S)`; no events → `(start, min(stop, start + HOOK_LEAD_S))`.
- `hook_cmd(clip, out, *, title, font)`: when `not clip.freeze`, `tpad`/blur/blend are omitted, `total = d`, audio fade-out 0.25 s at the end.
- `hook_clips(rounds, picks)` sets `freeze = pick.mode == "tease"`.
- CLI `--hook-mode {tease,play}` default `tease`; `JoinBody.hook_mode: Literal["tease","play"] = "tease"`; `_run_join_job` sets each pick's mode via `dataclasses.replace`.

- [ ] **Step 1: Failing tests**: `test_play_mode_runs_through_the_last_event` (chapter 500..560, kills 520, 530 → window `(514, 532)` with 6 s lead and 2 s tail); `test_play_mode_caps_at_14_s` (kills 505, 540 → `(528, 542)`); `test_play_mode_cmd_has_no_freeze` (`hook_cmd` with `freeze=False` → no `tpad`, no `gblur`, `-t d`); `test_cli_hook_mode_sets_pick_mode` (monkeypatch `join_stories`, run `main(["r1","r2","--hook","1:1","--hook-mode","play"])` → picks `[HookPick(1,1,"",mode="play")]`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest tests/test_story_bookends.py tests/test_story_join.py -q` → PASS, then the suite.
- [ ] **Step 5: Commit** `feat(story-join): --hook-mode play — montage hook played through the outcome`.

---

### Task 6: Web pure libs + vitest

**Files:**
- Create: `web/src/lib/timeline.ts`, `web/src/lib/takeEdits.ts`, `web/src/lib/waveform.ts`, `web/src/lib/mixSchedule.ts`, `web/src/lib/__tests__/{timeline,takeEdits,waveform,mixSchedule}.test.ts`, `web/vitest.config.ts`
- Modify: `web/package.json` (`"test": "vitest run"`, devDependency `vitest` ^2.1.8), `web/src/types/narration.ts` (mirror Task 1 fields + `GameAudioRegion`, `CameraRegion`, `TakeEdit`, `Marker`, `FrameStrip`, `Peaks`, `SessionView.suggested_shift_s`, `SessionView.markers`)

**Interfaces (all pure, no DOM):**
- `timeline.ts`: `interface Viewport { pxPerS: number; scrollS: number; widthPx: number }`; `sToPx(v, s)`, `pxToS(v, px)`, `zoomAround(v, factor, anchorPx, durationS): Viewport` (clamps `pxPerS` to `[widthPx/durationS, 400]`, keeps the second under `anchorPx` fixed, clamps `scrollS` to `[0, durationS − widthPx/pxPerS]`), `fit(widthPx, durationS): Viewport`, `tickEvery(pxPerS): number` (seconds between ruler ticks from `[1,2,5,10,15,30,60,120,300]`, first giving ≥ 80 px), `clampScroll(v, durationS)`.
- `takeEdits.ts`: `MIN_TAKE_S = 0.3`; `effectiveStart(t)`, `effectiveEnd(t)`; `trimHead(t, newStartS): Take`, `trimTail(t, newEndS, mediaDurationS?): Take` (clamps to media length when given), `splitAt(t, atS, newId): [Take, Take]`, `nudge(t, offsetS): Take` (clamped ±3), `punchIn(takes, newTake, newId): Take[]` — same semantics as Python Task 1 (tests use the same numbers).
- `waveform.ts`: `peaksForWidth(channel: Float32Array, columns: number): Float32Array` (max-abs per column, normalised to the global max); `mergeChannels(buffers: Float32Array[]): Float32Array`.
- `mixSchedule.ts`: `interface Scheduled { takeId: string; whenS: number; offsetS: number; durationS: number; gain: number }`; `scheduleFrom(takes, nowS): Scheduled[]` (unmuted takes with `end > nowS`; for a take already started, `offsetS = media_in_s + (nowS − start)` and `whenS = 0`; otherwise `whenS = start − nowS`, `offsetS = media_in_s`); `regionLevelAt(regions, gameGain, tS): number`; `isVoiceAt(takes, tS): boolean`.

- [ ] **Step 1: Write the failing tests** (vitest; mirror Task 1 numbers for `punchIn`, plus: `zoomAround` keeps the anchor second fixed; `fit(800, 792.9).pxPerS ≈ 1.009`; `tickEvery(1) === 120`, `tickEvery(10) === 10`; `peaksForWidth` of a 1000-sample ramp into 10 columns → increasing, last `1`; `scheduleFrom` mid-take offset math; `trimTail` clamps to `mediaDurationS − media_in_s`).
- [ ] **Step 2: Run** `bun run test` → FAIL (after `bun add -d vitest`).
- [ ] **Step 3: Implement.** **Step 4: Run** `bun run test` and `./node_modules/.bin/tsc -b` → PASS.
- [ ] **Step 5: Commit** `feat(web): narration v2 pure libs (timeline, take edits, waveform, mix schedule) + vitest`.

---

### Task 7: Web API client and data hooks

**Files:**
- Modify: `web/src/api/narration.ts` (`patchTake`, `splitTake`, `putMix`, `putGameRegions`, `putCameraRegions`, `relock(target, shiftS)`, `getFrames`, `getPeaks`, `getMarkers`, `uploadTake(..., replace)`), `web/src/hooks/useNarrationSession.ts` (mutations `editTake`, `splitTake`, `saveMix`, `saveGameRegions`, `saveCameraRegions`; `relockTakes` takes `shiftS`)
- Create: `web/src/hooks/useFrameStrip.ts`, `web/src/hooks/usePeaks.ts`, `web/src/hooks/useTakeBuffers.ts`, `web/src/hooks/usePlayhead.ts`

**Interfaces:**
- `useFrameStrip(target) → UseQueryResult<FrameStrip>` key `["narration", kind, key, "frames"]`; `usePeaks(target)` likewise `"peaks"`; markers come with `SessionView.markers`.
- `useTakeBuffers(view: SessionView | undefined, ctx: AudioContext | null) → { buffers: Map<string, AudioBuffer>; waveforms: Map<string, Float32Array> }` — fetches each `take.media` once (by media name), decodes with `ctx.decodeAudioData`, stores `mergeChannels` output; never refetches a media already decoded.
- `usePlayhead(video: HTMLVideoElement | null) → number` (rAF loop while playing; `timeupdate` while paused).
- Mutations: `editTake.mutate({takeId, edit: TakeEdit})` with optimistic update via `takeEdits.*`; `splitTake.mutate({takeId, atS})`; `saveMix.mutate(AudioMix)` (debounced by the caller); `saveGameRegions.mutate(GameAudioRegion[])`; `relockTakes.mutate(shiftS)`.

- [ ] **Step 1**: `tsc -b` is the test here (no DOM runner); write the types first so `tsc` fails on the missing functions, then implement. Record a one-line checklist in the commit body of which endpoints each function hits.
- [ ] **Step 2: Run** `./node_modules/.bin/tsc -b` → PASS. **Step 3: Commit** `feat(web): narration v2 API client and data hooks`.

---

### Task 8: Timeline lanes, take inspector, mix panel, shift-relock

**Files:**
- Create: `web/src/components/narration/timeline/Timeline.tsx`, `Ruler.tsx`, `FrameStripLane.tsx`, `MarkersLane.tsx`, `TakesLane.tsx`, `TakeBlock.tsx`, `GameAudioLane.tsx`, `Playhead.tsx`; `web/src/components/narration/TakeInspector.tsx`, `web/src/components/narration/MixPanel.tsx`
- Modify: `web/src/pages/NarrationPage.tsx` (replace `TakeTimeline`; selected take state; wire inspector, mix panel, record-over), `web/src/components/narration/ExportPanel.tsx` (+ `suggestedShiftS` and `onShiftRelock`), `web/src/components/narration/RecordControls.tsx` (hint "record-over replaces what it covers")
- Delete: `web/src/components/narration/TakeTimeline.tsx`
- Docs: `web/docs/features/narration.md` (rewrite "How to use" + code map), `web/docs/features/timeline.md` (new: lanes, zoom, interactions)

**Interfaces:**
- `<Timeline durationS view={Viewport} onView takes selectedId onSelect onSeek frames peaks markers regions onRegionsChange onTakeEdit onSplit playheadS waveforms />` composes the lanes; the lane components are presentational and receive the `Viewport`.
- Interactions: click empty lane = seek; wheel = pan, ctrl+wheel = zoom (`zoomAround`), "fit" button; takes: click = select, drag body = nudge (shows `+0.4 s` while dragging, commits `editTake` on release), drag edges = trim (commits on release, `trimTail` clamps to the decoded length), muted = grey, camera = green border; game lane: drag on empty = new region (level 0.5), drag edges = resize, click = cycle level 1.0→0.5→0.25→0.0, delete key removes the selected region, overlaps are prevented client-side (snap to neighbours); markers: click = seek, labels truncated with title tooltips.
- `TakeInspector`: gain slider (0–2, log-ish), mute toggle, "split at playhead" (enabled when the playhead is inside the take), nudge readout with reset, delete.
- `MixPanel`: voice (`narration_gain` 0–2), game (`game_gain` 0–2), duck (`duck_ratio` 1–20); debounced `saveMix` 300 ms.
- `ExportPanel`: when `suggestedShiftS != null` show "video got Δ s longer before the games — shift takes and relock" calling `relockTakes.mutate(Δ)`.

- [ ] **Step 1**: build the lanes bottom-up against `tsc`; use `<canvas>` for the waveform in `TakeBlock` and the peaks in `GameAudioLane` (redraw on viewport change via `useEffect`), plain divs for blocks/regions/markers, the sprite as a `background-image` with `background-position` per tile in `FrameStripLane`.
- [ ] **Step 2: Run** `tsc -b` and `bun run test` → PASS; open `/narrate/joined/joined_2026-11-26_xayah-kaisa-yunara` with `bun run dev` and check: lanes render, zoom/pan, a recorded take shows a waveform, trim/nudge/split/mute round-trip through the API (check `session.json`), a region saves. Note findings in the commit body.
- [ ] **Step 3: Commit** `feat(web): narration timeline (frames, markers, takes, game audio), take inspector, mix panel, shift-relock`.

---

### Task 9: Web Audio mix preview

**Files:**
- Create: `web/src/hooks/useMixPreview.ts`
- Modify: `web/src/pages/NarrationPage.tsx` (replace `useTakePlayback`; mute the preview while recording so the mic does not hear itself through speakers — the page already warns about headphones)
- Delete: `web/src/hooks/useTakePlayback.ts`
- Docs: `web/docs/features/narration.md` (preview section)

**Interfaces:**
- `useMixPreview({ video, session, buffers, enabled }) → { ctx: AudioContext | null }`: one `AudioContext` created on the first user gesture (Record or play); `createMediaElementSource(video)` once per element → `gameGain` → `duckGain` → `destination`; per unmuted take an `AudioBufferSourceNode` from `scheduleFrom(takes, video.currentTime)` on `play`/`seeked`/`ratechange`/session change, each through its own `GainNode(take.gain)` → `narrBus(GainNode(narration_gain))` → `destination`; an `AnalyserNode` on `narrBus`; a rAF loop computes RMS and sets `duckGain.gain` to `1/duck_ratio` when RMS > `duck_threshold` (ramps: `setTargetAtTime` with attack/release from `AudioMix`), and sets `gameGain.gain` to `regionLevelAt(regions, game_gain, t)`; everything stops on `pause`.
- `enabled=false` (while recording) disconnects `narrBus` so takes are silent but the game audio still plays.

- [ ] **Step 1**: implement against `tsc`; manual check with two overlapping-free takes and one region: both takes audible in sync, level drops inside the region, game dips while a take plays, scrubbing re-schedules.
- [ ] **Step 2: Run** `tsc -b`, `bun run test` → PASS. **Step 3: Commit** `feat(web): full-mix preview in the browser (Web Audio)`.

---

### Task 10: Docs, arsenal, knowledge, deploy

**Files:**
- Modify: `api/CLAUDE.md` (narration lines in "New game flow": 6b mentions punch-in and the timeline), `api/docs/narration.md` (create or extend: API table from spec §4.10, cache files, cues.json), `knowledge/series/Narration.md` (how the owner records now: pieces, record-over, regions, duck; the hook montage decision), `knowledge/series/Story-join.md` (hook mode play), `docs/handoff/laptop-2026-11-27.md` (the exact `story-join` command for the montage hook + "pull `feat/narration-v2`")
- Regenerate `.claude/skills/editing-expert/arsenal.md` if Task 3/5 did not.

- [ ] **Step 1**: write the docs; run all three test suites + ruff + `tsc -b`.
- [ ] **Step 2: Commit** `docs: narration v2 (timeline, punch-in, mix), story-join hook mode, laptop handoff`; push `feat/narration-v2`.
- [ ] **Step 3**: deploy the branch to the PC runner checkout (`git checkout <sha>` + `uv sync` with both tasks stopped, restart `AVE API` / `AVE Grok Runner`), restart `bun run dev`, write `cues.json` for the joined video from the cue sheet (Task 4 format).
