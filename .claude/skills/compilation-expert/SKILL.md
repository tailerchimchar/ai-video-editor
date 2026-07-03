# Compilation Expert

---
name: compilation-expert
description: Use when working on how compilations are rendered, edited, reordered, or introduced; the drag-and-drop filmstrip; the Twitch ingest → analyze → compile flow; multi-game VOD splits; the auto-cleanup safety model; or the sources-vs-compilations gallery split.
---

## What a compilation is

A compilation is one rendered highlight reel (`compilation.mp4`) plus its mutable spec, its append-only edit journal, and its per-clip render cache. All four live in one folder under `WORKSPACE/compilations/<asset-stem>_<ts>/`. There is no compilation-shaped row in the DB that stores clip order or effects — the `compilations` table only carries `id`, `asset_id`, `output_path`, `params`, `created_at` (`database.py:115-121`). The truth about what's in the reel is `spec.json`.

Anything that mutates the reel mutates the spec, saves it, re-renders (partial if possible), appends to the journal. That five-step pattern lives in `_do_edit` (`routers/edits.py:579-604`) and every mutator endpoint funnels through it.

## The lifecycle

1. **Initial compile.** `POST /edit/compile` (`routers/edits.py:423-439`) → background job → `build_compilation(...)` (`compile.py:658-718`) → writes `spec.json` + renders `compilation.mp4`. A journal entry with `action="initial_compile"` is appended so revert has a v1 to walk back to (`routers/edits.py:378-388`).
2. **Iterative edits.** Each mutating endpoint under `/edit/compile/{id}/…` loads the spec, applies a pure mutator from `compile.py`, saves, calls `render_spec(new_spec, folder, dirty_clip_ids=dirty)`, and appends to the journal with an `action` key + `details` dict (`routers/edits.py:579-604`).
3. **Revert.** `POST /edit/compile/{id}/revert` walks the journal back N steps (or to an explicit version), restores that spec snapshot, re-renders with every clip marked dirty, and — critically — journals the revert itself so revert is also undoable (`routers/edits.py:1352-1396`).

The lifecycle has no "publish" or "finalize" step. `show_clip_numbers` starts True (`compile.py:671`, `routers/edits.py:317`) so the first render carries `#01`/`#02`/… corner labels for iteration; flip them off via `POST /edit/compile/{id}/labels {"enabled": false}` when the reel is done (`routers/edits.py:755-773`). The cached parts are keyed with a `_nN` suffix per label state, so toggling back is instant (no re-encode).

## Disk layout

Every compilation is a self-contained folder:

```
WORKSPACE/compilations/<asset-stem>_<ts>/
    spec.json              <-- mutable source of truth
    spec_history.jsonl     <-- append-only journal, one line per snapshot
    compilation.mp4        <-- rendered output (may be absent on failure)
    index.json             <-- last render's summary (paths, kept_total, error)
    thumbnail.jpg          <-- poster from mid-reel; auto-extracted
    _cleanup.log           <-- JSON-lines audit of every non-dry-run deletion
    _parts/
        part_<id8>.mp4       <-- unlabeled variant, one per clip
        part_<id8>_n<N>.mp4  <-- labeled variant matching the current #NN
    _thumbnails/
        <clip-id>.jpg       <-- filmstrip tiles, one per clip
```

**When something's missing:**
- **No `spec.json`.** `load_spec` raises. Every editing endpoint 404s or 500s because `_load_compilation_folder` returns the folder but subsequent loads fail. A compilation without its spec is functionally dead — the DB row still exists but no mutation can run.
- **No `compilation.mp4`.** `_load_compilation_folder` (`routers/edits.py:487-498`) 404s with "Compilation not found or never rendered". This is the initial-compile-failed state or a mid-render crash.
- **No `spec_history.jsonl`.** `read_journal` returns `[]` (`compile_journal.py:83-99`). `list_compilation_history` renders an empty list; revert refuses with "no journal entries — nothing to revert to" (`compile_journal.py:266-269`).
- **No `_parts/`.** `render_spec` creates it lazily (`compile.py:559`). Every clip renders fresh next time.
- **No `_thumbnails/`.** Filmstrip tiles show a placeholder dash. `POST /edit/compile/{id}/thumbnails/regenerate` backfills (`routers/edits.py:1416-1430`).

## The render pipeline

`render_spec(spec, folder, dirty_clip_ids=None)` (`compile.py:538-655`) is the whole engine. Three passes:

**Pass 1 — per-clip render.** For each clip in `spec["clips"]`, compute a cache-hit filename `_parts/part_<id8>[_nN].mp4`. If the file exists AND (`dirty_clip_ids is None or clip.id not in dirty`), reuse it. Otherwise call `_render_clip_part` (`compile.py:358-402`) which runs ffmpeg with:

```
-ss <clip.start> -i <clip.asset_path> -t <duration>
-vf <filterchain>
-c:v <settings.ffmpeg_video_codec> <codec_opts> -c:a aac -b:a 128k
```

The filterchain is built by `_build_clip_filterchain` (`compile.py:272-355`) in a strict order:
1. **Effects (zoom, focus).** `crop → scale=iw*2:ih*2` for zoom (upscale post-crop so quality holds); `geq=lum='if(lt(hypot(...),r),lum,lum*dim)'` for focus. Caption effects are pulled OUT here and re-injected in pass 2 with the segment renderer.
2. **Captions.** `drawtext` filter chain via `caption_filters(...)`. Both segments AND `effect.kind == "caption"` overlays go through the same renderer.
3. **Fades.** `fade=t=in:st=0:d=<f>` and `fade=t=out:st=<dur-f>:d=<f>` — set by `spec.fade_seconds` (default 0.3s).
4. **Aspect tail.** For `aspect="9:16"`, `crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=720:1280`. For 16:9 this is a no-op.
5. **Iteration label.** Yellow `#NN` in the top-left, ONLY when `show_clip_numbers` is on. Painted last so it sits on top of the aspect-cropped canvas.

**Pass 2 — concat.** `_concat(part_paths, out_path)` (`compile.py:405-431`) writes a temp file list and runs `ffmpeg -f concat -safe 0 -i list.txt -c copy`. Stream-copy demux/mux, no re-encode. This is why per-clip renders exist: a single-clip edit re-encodes ONE part (~seconds) and re-concats (~sub-second), instead of re-encoding the whole 3-minute reel.

**Pass 3 — optional music mix.** When `spec.music_path` is set, the concat writes to an intermediate `concat_audio_only.mp4`, then `_mix_music` (`compile.py:434-461`) lays the music track under the mixed audio with `amix=inputs=2:duration=first:dropout_transition=0`. Music volume defaults to 0.25 (`compile.py:474`, `_mix_music:435`). The intermediate is deleted after mix.

**Pass 4 — auto-cleanup + thumbnails.** On successful concat, `safe_cleanup_for_render` prunes orphan parts (see below), `cleanup_orphan_clip_thumbnails` prunes stale filmstrip tiles, `safe_extract_thumbnail` grabs a poster from mid-reel, `safe_extract_clip_thumbnails` fills any missing filmstrip tiles. All four are best-effort — a failure post-concat never fails the render.

**Why `dirty_clip_ids`.** Cleaner than a mtime check and immune to disk-clock skew. The mutators return `(new_spec, dirty_set)` where the set is exactly the clip ids that need re-encoding. `render_spec(spec, folder, dirty)` re-encodes those, cache-hits the rest, and concats. For a `remove_clip` mutation the dirty set is empty (`compile.py:836-840`) — no re-encode, just re-concat. For `set_clip_numbers` the dirty set is EVERY clip id (`compile.py:1128-1130`) because the label overlay changes the filter chain.

## The auto-cleanup safety model

`_parts/` grows every time a clip is removed, an insert shifts later clips' `#NN` indexes, or a revert restores an older spec that references different clip ids. Left unswept it fills disk. `render_spec` calls `safe_cleanup_for_render` (`compile.py:622-629`), which wraps `cleanup_compilation` (`compile_cleanup.py:243-311`) — the ONLY auto-deleting code in the repo.

Five defense-in-depth guardrails (`compile_cleanup.py:20-35`):

1. **Workspace containment.** The folder MUST be a direct child of `<workspace_dir>/compilations/`. `is_safe_compilation_folder` (`compile_cleanup.py:117-140`) resolves symlinks first, rejects the root itself, rejects deeper paths (`compilations/<id>/_parts` is a subfolder, not the compilation folder). A bug passing `C:\Windows` raises `CleanupSafetyError` and no unlink happens.
2. **Strict filename regex.** Only files matching `^part_[0-9a-f]{8}(_n\d+)?\.mp4$` are eligible. Notes, debris, notes.txt, future cache shapes are LEFT ALONE (`compile_cleanup.py:63`). Better to leak a few KB than mis-delete.
3. **Symlink refusal.** `path.is_symlink()` short-circuits eligibility (`compile_cleanup.py:170-179`, `compile_cleanup.py:206-208`). A symlink planted inside `_parts/` gets surfaced in the `skipped` list, never unlinked.
4. **Audit log.** Every non-dry-run deletion appends one JSON-lines record to `<folder>/_cleanup.log` (`compile_cleanup.py:229-240`). Best-effort: log write failure is suppressed so a permission blip can't fail the render.
5. **Errors don't propagate to renders.** `safe_cleanup_for_render` catches `CleanupSafetyError` AND any other exception (`compile_cleanup.py:314-330`), returning `{"ok": False, "error": ..., "reason": "safety"}`. A misconfigured workspace can't undo a successful render.

`valid_part_filenames(spec)` (`compile_cleanup.py:146-167`) is the pure "what to keep" function. For each clip it keeps BOTH `part_<id8>.mp4` AND `part_<id8>_n<idx>.mp4` where `idx` is the clip's current 1-based position — preserving the instant-flip property (`show_clip_numbers` toggle picks the cached counterpart with no re-encode).

`POST /edit/compile/{id}/cleanup {"dry_run": true}` exposes a preview (`routers/edits.py:1439-1452`). Non-dry-run is idempotent.

## Iterative editing — the mutator catalogue

Every mutator in `compile.py` is pure: takes a spec, returns `(new_spec, dirty_ids)`. The routers wrap them in `_do_edit`. Complete list:

| Mutator | Endpoint | Journal action | Dirty set |
|---|---|---|---|
| `add_effect` | `POST /effect` | `add_effect:<kind>` | just this clip |
| `extend_clip` | `POST /extend` | `extend_clip` | just this clip |
| `remove_clip` | `POST /remove` | `remove_clip` | empty (concat-only) |
| `insert_clip` | `POST /insert` | `insert_clip` | the new clip's id |
| `set_intro_clip` | `POST /intro` | `set_intro` | new intro's id (replaces if one exists at pos 0) |
| `insert_intro_at_position` | `POST /insert_intro` | `insert_intro_at_position` | new intro's id |
| `clear_intro_clip` | `DELETE /intro` | `clear_intro` | empty |
| `reorder_clips` | `POST /reorder` | `reorder:<mode>` | ALL non-intro ids (label re-encode) |
| `reorder_clips_explicit` | `POST /reorder_explicit` | `reorder:explicit` | ALL clip ids |
| `set_clip_numbers` | `POST /labels` | `labels:on\|off` | ALL clip ids |
| `add_caption_segment` | `POST /clip_captions/add` | `add_caption` | just this clip |
| `remove_caption_segment` | `POST /clip_captions/remove` | `remove_caption` | just this clip |
| `tiktokify_clip` | `POST /clip_captions/tiktokify` | `tiktokify` | just this clip |
| `set_clip_captions` | `POST /clip_captions` | `edit_captions` | just this clip |
| `set_caption_mode` | `POST /caption_mode` | `caption_mode:<mode>` | just this clip |

Every mutator uses `json.loads(json.dumps(spec))` at the top to deep-copy so the caller's spec object stays untouched (`compile.py:821`). This is the pattern — don't mutate in place.

## The filmstrip UI

`web/src/components/ClipFilmstrip.tsx` is the horizontal strip below the video. Three gestures, mutually exclusive by z-stack:

- **Click on tile body** → `onSelect(clipId)` → the right panel loads the clip's editor + the video seeks to that clip's reel start (`CompilationViewer.tsx:118-134`).
- **Edge drag (8px strip on left/right)** → `startEdgeDrag('left'|'right')` (`ClipFilmstrip.tsx:253-293`). Live preview scales the tile with `transform: translateX + width`. Commits on `pointerup` via `onExtend(clipId, {before, after})` where positive-left = extend backward in source, positive-right = extend forward.
- **Middle drag (tile body)** → HTML5 native drag-and-drop with `draggable={true}`. `handleDragOver` computes left-half-vs-right-half of the hovered tile to place the drop indicator; `handleDrop` builds the FULL new order (splice out, splice in with adjusted index if moving down) and fires `onReorder(newOrder)` (`ClipFilmstrip.tsx:107-150`).

Pixels-per-second is derived from `TILE_WIDTH_PX (176) / clip.duration`. A 10px drag on an 8-second tile = ~0.45s. Deltas are rounded to 0.1s on commit (`ClipFilmstrip.tsx:283-284`).

**Why reorder sends the FULL order, not a swap.** `reorder_clips_explicit` validates that the requested id set exactly matches the current spec's id set (`compile.py:1029-1039`). A stale UI can't silently lose or duplicate a clip. Any mismatch raises `ValueError` → 400 with a specific "missing: [...] unknown: [...]" body. If we sent a swap `(from, to)` instead, a divergent client wouldn't know.

**Playing vs selected.** These are different. `selectedId` is what the editor panel points at. `playingId` is what the `<video>` is currently playing — derived from `currentReelTime` walking cumulative clip durations (`CompilationViewer.tsx:140-151`). The filmstrip tile shows a pulse dot for `playingId` and an accent border for `selectedId`. Auto-scroll centers the playing tile as playback advances (`ClipFilmstrip.tsx:73-82`).

**Cache-busting.** `videoVersion = history.data?.history.length` (`CompilationViewer.tsx:63`). Bumps by 1 with every edit. The `<video src>` and each `<img src>` for filmstrip thumbnails get `?v=${videoVersion}` appended so the new render + new thumbs load, not the cached ones.

## The intro system

Intros live in their own folder under the workspace, not the compilation folder: `WORKSPACE/intros/<name>/{intro.json, intro.mp4, source/logo.png, source/music.mp3}` (`intros/__init__.py:1-20`). They are NOT indexed assets — no DB row, no `asset_id`. The compilation spec carries `asset_id: None` on intro clips with `event_type="intro"` and `asset_path` pointing directly at `intro.mp4` (`compile.py:919-934`).

**Two placement modes:**
- `POST /edit/compile/{id}/intro` (`routers/edits.py:929-960`) — prepend or replace at position 1. If an existing clip at index 0 has `event_type="intro"`, it's REPLACED, not stacked (`compile.py:934-938`). Use for the standard "brand my reel" case.
- `POST /edit/compile/{id}/insert_intro` (`routers/edits.py:1253-1308`) — insert at any position without replace semantics. Use for chapter cards / mid-reel transitions ("add the intro after clip #3"). Takes either `after_clip` (resolves via clip_ref rules) or `position` (1-based); exactly one, never both.

**Default intro.** Set via `POST /intros/default`. Retrieved by `get_default_intro_name` (`intros/__init__.py:36`). Both endpoints above fall back to the default when `intro_name` is omitted; the fallback is centralised in `_resolve_intro_or_404` (`routers/edits.py:905-926`).

**`DELETE /edit/compile/{id}/intro`** removes the intro at position 0 if there is one. No-op otherwise. Empty dirty set — just re-concat.

**Reorder respects intros.** `spec_reorder_clips` (`compile.py:1045-1117`) treats every `event_type="intro"` clip as positionally fixed. It sorts only the gameplay clips between/around them. This preserves the mental model "the intro is a chapter marker, not gameplay." `spec_reorder_clips_explicit` does NOT respect that — if the user drag-and-dropped an intro to a different position, that's intentional.

## Twitch ingest workflow

`POST /assets/ingest_url {"url": "...", "game": "league"}` (`routers/assets.py:527-559`, was `526-559` before recent duration column) kicks off a job that:

1. Validates URL against `^https?://[^\s/$.?#].\S*$` (`routers/assets.py:239`).
2. Sanitises `game` to `[a-z0-9_-]+` (`routers/assets.py:242`) — prevents path traversal.
3. Resolves yt-dlp via `_resolve_yt_dlp_invocation` (`routers/assets.py:245-269`): CLI first (`shutil.which("yt-dlp")`), Python module fallback (`python -m yt_dlp`) tested against each of `python`, `py`, `python3` on PATH. Returns `None` if not installed → job fails with a clear "install yt-dlp" message.
4. Writes to `OUTPLAYED_MEDIA_DIR/<game>/%(title).80B_%(id)s.%(ext)s`. The vod-id keeps re-downloads idempotent (yt-dlp skips existing files).
5. Parses `--print after_move:filepath` output to know the final path (post any post-processing rename).
6. Inserts an asset row with `source_origin='downloaded'` (or UPDATEs an existing scan-registered row).

Poll `/api/v1/jobs/{id}` for status. On success `output_path` carries the new asset id.

**yt-dlp is a soft dependency.** Not listed in `pyproject.toml`. The user installs it globally OR into any Python on PATH. Fail-safe: absence is caught at request time (not startup), reported clearly, and doesn't crash the app.

## Multi-game VOD splits

`POST /assets/{id}/split` (`routers/assets.py`) scans a long recording for game boundaries and slices it into per-game children. The whole module is `api/src/ai_video_editor/splitter.py`:

- `detect_game_boundaries(path)` runs `ffmpeg -vf blackdetect=d=2.0:pix_th=0.10 -f null -` and parses stderr for `black_start:...:black_end:...:black_duration:...` lines. Default min-black-duration 2s catches loading screens and return-to-lobby fades, skips quick scene cuts. 10-minute subprocess timeout (`splitter.py:54`).
- `intervals_to_segments(intervals, duration)` converts to `GameSegment[]` using each black interval's midpoint as a cut point, then drops any segment shorter than `MIN_GAME_LENGTH_SECONDS` (60s, `splitter.py:49`). Renumbers so indexes are contiguous.
- `split_segment(src, out, start, end)` runs `ffmpeg -ss <start> -i src -to <duration> -c copy -avoid_negative_ts make_zero out`. Stream-copy (no re-encode) so a 30-min segment writes in seconds, not minutes. Drawback: cut points snap to nearest keyframe (0-2s drift). Acceptable for game boundaries.
- `child_filename(parent, seg)` → `scrim.mp4` + segment 2 → `scrim_game2.mp4`.

**Where children live.** BESIDE the parent, still in `OUTPLAYED_MEDIA_DIR`. A split child is a legitimate NEW source file — you'd want to analyze and compile from it. The source-immutability rule allows this because the parent isn't modified; the children are additions.

**Each child gets its own asset row** with `parent_asset_id` pointing to the parent (`routers/assets.py:445-464`), `source_origin='imported'` (because the file was written to `OUTPLAYED_MEDIA_DIR`), and `duration_seconds` set from the planned segment duration (`routers/assets.py:449-460`) rather than re-probed (more accurate — the cut file may drift 0-2s from the plan due to keyframe snapping).

**UI gate.** The split button in the Sources gallery only shows for assets with `duration_seconds > 3600` (`web/src/components/AssetCard.tsx`, `canSplit`). Server-side the endpoint still works on any file — it just returns "no split needed: detected N black intervals, M segments after filtering" for short files (`routers/assets.py:412-425`). The gate is purely visual hygiene: single League/Val games are 30-50 min, so anything longer than 1hr is a plausible multi-game candidate.

**When it returns "no split needed".** Either no black intervals were detected (single continuous game) or all detected segments failed the 60s minimum (UI artifacts, not real games). Reported as `completed`, not `failed` — nothing to fix.

## The candidate-first architecture as it applies to compilations

Read `api/CLAUDE.md` for the full explanation. The short version:

```
recordings ──> candidate generators (cheap, deterministic, no LLM)
                 ├─ outplayed_clip   (Outplayed already cut it)
                 ├─ riot_api         (exact kill/death timestamps)
                 ├─ audio_peak       (loud regions in full VODs)
                 └─ transcript_keyword (Whisper STT → hype/funny cues)
                          │
                          ▼
              HighlightCandidate rows (one table, source-tagged)
                          │
                          ▼
              LLM ranker (1 call/video) ──> keep + funny/hype/story
                          │                  scores + reason
                          ▼
              rankings.json          <-- pinned per-asset ranker output
                          │
                          ▼
              build_compilation()   → spec.json → render_spec → .mp4
```

`build_compilation` reads `rankings.json` at `WORKSPACE/rankings/<asset_id>.json` (`routers/edits.py:326-334`). If it's missing, the compile job fails with "No rankings — run POST /assets/{id}/rank first." The ranker is the ONLY LLM call in the pipeline; the compile stage is fully deterministic.

**Post-rank clustering.** `cluster_ranked_candidates` (`compile.py:140-228`) merges kept rankings whose windows overlap or sit within `settings.cluster_gap_seconds` (default 30s). This kills the "10 short clips of one teamfight" problem. The cluster anchor prefers `riot_api` (highest-confidence ground truth), falling back to the highest-hype member. Set `cluster_gap_seconds=0` to disable clustering.

**Per-event windowing.** `spec_from_rankings` widens each candidate's suggested window by `settings.event_window_overrides[event_type]` (`compile.py:494-500`). A `kill` gets `(3s pre, 8s post)`; an `ace` gets `(5s pre, 12s post)` to milk the celebration; a `death` gets a symmetric `(4s, 4s)` for context (`config.py:113-135`). Unknown event types fall back to no padding (the ranker's raw window survives).

## Order modes

`plan_clips` (`compile.py:42-82`) takes the kept rankings and orders them:

- **`hook`** *(default)* — highest-hype clip FIRST, the rest chronological. Maximises the first-3-seconds retention signal on algorithmic platforms.
- **`hype`** — all clips by hype score descending. Peaked-early-then-trails-off arc.
- **`chronological`** — story order, what happened first plays first. Better when context matters.
- **`narrative`** — three sections in recording order: intro (first ~10 min: warmup/greeting) → main (chronological body) → outro (last ~10 min: post-game). Tunable via `settings.narrative_*`. When `limit` is set, intro + outro are preserved and the limit applies to main so structural sections always survive.

`reorder_clips` supports the same modes plus `funny` and `story` for post-compile re-ordering. Intros stay put (see intro section above).

## Iteration labels

`spec.show_clip_numbers` (bool) toggles the `#NN` corner overlay on every clip. First render starts with labels ON (`compile.py:671`, `routers/edits.py:317`) so the user can say "zoom #04" instead of tracking reel timestamps. `POST /edit/compile/{id}/labels {"enabled": false}` flips it via `spec_set_clip_numbers` (`compile.py:1120-1130`).

**Why cached parts survive the toggle.** Part filenames embed the label state as `_nN` suffix: `part_a1b2c3d4.mp4` (unlabeled) and `part_a1b2c3d4_n3.mp4` (labeled at position 3). Toggling picks the counterpart from cache — no re-encode when both variants exist. `valid_part_filenames` keeps both for every clip so cleanup doesn't sweep the "off" variant while you're on (`compile_cleanup.py:159-167`).

**Journal action key.** `labels:on` or `labels:off` (`routers/edits.py:769-770`).

## Music mix

`spec.music_path` (string or None) + `spec.music_volume` (float, default 0.25). `_mix_music` (`compile.py:434-461`) writes the concat to `concat_audio_only.mp4`, then mixes: `[1:a]volume=<v>[m];[0:a][m]amix=inputs=2:duration=first`. `duration=first` clamps to video length. Both streams mix; source audio stays at full volume, music is dimmed to `music_volume`.

The intermediate is deleted after mix (`compile.py:611`). No music path → the concat writes directly to `compilation.mp4`.

There is no music-mutator today. To add music to an existing compilation, edit `spec.json` directly and call `POST /edit/compile/{id}/revert` back to the current state (or add a mutator + endpoint following the pattern).

## Sources vs Compilations galleries

Two top-level pages, tabs in the header (`web/src/components/GalleryTabs.tsx`):

- **`/` — Compilations gallery** (`web/src/pages/CompilationsList.tsx`). Rendered reels. Each tile shows the compilation's thumbnail, source-recording stem, render timestamp. Click → `/compile/{id}` opens `CompilationViewer`.
- **`/assets` — Sources gallery** (`web/src/pages/AssetsList.tsx`). Raw recordings. Each tile (`AssetCard.tsx`) shows a poster frame + filename + game + duration + created-at. Filters: game (all/league/valorant) + origin (all/imported/downloaded). Affordances vary: `source_origin='downloaded'` gets a "downloaded" badge; deleted files get a "file deleted" badge; long recordings (`duration_seconds > 3600`) get the "split into games" button.

**Different affordances.** Compilations have delete/re-render/edit. Sources have split/analyze/delete-source. Don't cross the wires — a compilation isn't an asset even though it lives on disk as an .mp4.

## `source_origin` semantics

Every asset row carries `source_origin` (`database.py:20`):

- **`imported`** *(default)* — the file was found by `POST /assets/scan` walking `OUTPLAYED_MEDIA_DIR`. Also includes split children (they're new files but were "created" locally, not downloaded from the internet). These files are **sacred** — the cleanup tool refuses to auto-delete them.
- **`downloaded`** — the file came from `POST /assets/ingest_url` (yt-dlp). These CAN be auto-deleted via `POST /assets/{id}/delete_source` (`routers/assets.py:157-213`). Guardrails on that endpoint: origin must be `downloaded`, `source_deleted_at` must be NULL, resolved path MUST be inside `OUTPLAYED_MEDIA_DIR` (defense against corrupted DB rows pointing at `/etc/shadow`), file must exist.

`source_deleted_at` is set once when the file is deleted; never unset. The row survives so compilations referencing the asset keep their FK — you just can't re-cut new clips from the source.

## The source-files-immutable rule

Read from `OUTPLAYED_MEDIA_DIR`, write to `WORKSPACE_DIR`. This is enforced by architecture, not code: the ffmpeg wrappers take source paths as input and output paths as output; no wrapper writes back to its input. The one apparent exception — multi-game splits writing children under `OUTPLAYED_MEDIA_DIR` — doesn't modify the parent file. Children are additions, not mutations.

**Why it matters.** The user's Outplayed library is the ground truth. Re-runs must be idempotent. A corrupted spec, a bad ranker, a bug in the compile pipeline — none of these can put the user in a state where they've lost gameplay footage. The workspace can always be nuked and rebuilt from sources.

## Common commands

```bash
uv run dev                              # API server :8000
uv run ruff check src/ && uv run ruff format src/
.venv/Scripts/python.exe -m pytest -q   # backend tests
bun run dev                             # web dev server :5173
bun tsc --noEmit                        # type check
bun x prettier --write src/             # format
```

Auto-reload note: `uv run dev` does NOT run with `--reload` on purpose (long jobs die on reload). `uv run dev-reload` opts in when no long jobs are in flight (`api/src/ai_video_editor/cli.py:19-29`). The frontend is Vite HMR — save-to-reload always works.

## Extension recipes

**Adding a new spec mutator.** Follow the pattern in `compile.py`:
1. Write a pure function `mutator(spec, ...args) -> (new_spec, dirty_ids)`. Deep-copy at the top with `json.loads(json.dumps(spec))`.
2. Add an endpoint under `/edit/compile/{id}/…` that resolves the clip_ref, wraps the mutator, and calls `_do_edit(folder, mutator, compilation_id, action=..., details=...)`. `_do_edit` handles load-mutate-save-render-journal.
3. Add a matching MCP tool per the coverage policy in `api/CLAUDE.md`. Naming: `compilation_…`, `intro_…`, `clip_…` prefix; verb matches user phrasing.
4. Add a mutation hook in `web/src/hooks/useCompilation.ts` if the webapp needs it.

**Adding a new effect kind (e.g. speed ramp).** Extend `_build_clip_filterchain` (`compile.py:272-355`) with a new branch in the effects loop. Add a request body class + endpoint routing in `routers/edits.py`. Add TS types in `web/src/types/clip.ts`.

**Adding a new render pass.** Insert it in `render_spec` between concat and thumbnail (`compile.py:595-640`). Preserve the "cleanup only after successful concat" invariant — a failed pass shouldn't nuke the cache.

**Adding a new gallery filter.** `web/src/pages/AssetsList.tsx` for sources, `CompilationsList.tsx` for compilations. Filters are client-side today (small dataset); if you push filtering server-side, add query params to `GET /assets` and `GET /edit/compile`.

## Anti-patterns

- **Writing to source files.** Violates the immutability rule. Even a "harmless" rename breaks re-runs. All output goes to `WORKSPACE_DIR` (or, for split children, beside the parent in `OUTPLAYED_MEDIA_DIR` as new files — never as edits to existing files).
- **Bypassing the journal.** A mutator that saves the spec without calling `append_journal` breaks revert alignment. `_do_edit` calls `append_journal` after `save_spec` + `render_spec`; any custom flow must do the same.
- **Re-encoding when concat could copy.** The concat pass uses `-c copy` because per-clip parts share codec/timebase (guaranteed by `_render_clip_part`). A concat that re-encodes would multiply render time by ~N.
- **Ignoring existing `_parts/` cache.** `render_spec` cache-hits on `part_path.exists() AND clip.id not in dirty`. A new pass that re-renders every clip regardless would murder iteration speed.
- **Deleting `spec.json` on cleanup.** `_PART_FILENAME_RE` refuses to match `spec.json` (`compile_cleanup.py:63`). Keep it that way. If you extend the cleanup to sweep new file kinds, use a sibling function with its own strict regex — don't loosen the part-file one.
- **Skipping the MCP mirror on new compilation-editing endpoints.** The coverage policy in `api/CLAUDE.md` is not optional: every mutating endpoint owes an MCP tool. The user drives editing through Claude/MCP; unmirrored endpoints are invisible to them.
- **Adding a mutator that doesn't re-render.** Spec drift from `compilation.mp4` is a bug — the user opens the reel and sees the OLD render even though the spec has moved on. `_do_edit` calls `render_spec` between save and journal. Preserve that order.
- **Storing computed derived data on the compilation row.** `compilations.params` carries the compile-request body only. Everything else (clip order, effects, captions) is in `spec.json`. Duplicating derived data invites drift.
- **Duration heuristics that double-count intros.** Intro clips have `event_type="intro"` and their own duration in `end_seconds - start_seconds`. Any planner that uses `settings.event_window_overrides` for intro clips would pad them with `("kill", 3.0, 8.0)` or similar — nonsense. `_spec_from_rankings` doesn't touch intros because they're not in the ranker output; they're inserted post-hoc via `spec_set_intro_clip`. Preserve that separation.
- **Running cleanup with `enforce_safety=False` outside tests.** The parameter exists for tests that use `tmp_path` outside the configured workspace (`compile_cleanup.py:246-247`). Any prod code path that sets it False bypasses the containment check and is one bug away from `rm -rf /`.
- **Assuming `output_path` is not null.** A failed initial-compile leaves the row with `output_path IS NULL` (`routers/edits.py:390-401`). `_load_compilation_folder` 404s that state. Downstream code that dereferences without a null check crashes on those rows.

## Journal action reference

`format_action_display` (`compile_journal.py:129-238`) turns raw action strings into human phrases. Full catalogue:

| Action key | Display |
|---|---|
| `initial_compile` | `Initial compile (<order> order[, limit N])` |
| `add_effect:zoom` | `Added zoom to clip #NN (Nx · <roi>)` |
| `add_effect:focus` | `Added focus to clip #NN` |
| `add_effect:caption` | `Added caption to clip #NN · '<preview>'` |
| `extend_clip` | `Extended clip #NN (+Ns before · +Ns after)` |
| `caption_mode:tiktok` | `Clip #NN → tiktok captions` |
| `edit_captions` | `Edited captions on clip #NN · N segments` |
| `add_caption` | `Added caption to clip #NN · '<preview>'` |
| `remove_caption` | `Removed caption from clip #NN (segment #<i>)` |
| `tiktokify` | `Clip #NN → TikTok captions (word-by-word)` |
| `labels:on\|off` | `Iteration labels on\|off` |
| `insert_clip` | `Inserted manual clip at position N` |
| `remove_clip` | `Removed clip #NN` |
| `set_intro` | `Set intro to '<name>'` |
| `clear_intro` | `Removed intro` |
| `insert_intro_at_position` | `Inserted intro '<name>' after clip #NN` |
| `reorder:<mode>` | `Reordered clips by <mode>` |
| `revert` | `Reverted to v<N>` |

New action keys need a matching branch or they fall through to the raw-action fallback (`compile_journal.py:238`). Keep this table in sync when adding mutators.

## Clip reference resolution

`resolve_clip_ref(spec, ref)` (`compile.py:770-813`) is how endpoints translate user input into a 0-based clip index. Priority order:

1. **Int or numeric string** → 1-based index (`"2"` is clip #2). Out-of-range raises `KeyError`.
2. **UUID prefix** (≥ 4 chars) → clip whose `id` starts with that prefix.
3. **Time string** (`M:SS`, `H:MM:SS`, or plain seconds) → first try REEL time (via `reel_positions`), then SOURCE time (matched against `[start_seconds, end_seconds)`).

Endpoints catch the `KeyError` and 400 (`routers/edits.py:637-638` and similar). Keep this contract stable — the MCP tools rely on it, the webapp relies on it, the ranker's history rail uses the same phrasing.

## Testing patterns

Backend tests live in `api/tests/`. Split by concern:
- **Pure mutators** — test `spec_from_rankings`, `plan_clips`, `intervals_to_segments`, `valid_part_filenames`, etc. with dict fixtures. No ffmpeg, no filesystem, no DB.
- **Cleanup guardrails** — `test_compile_cleanup.py` exercises the safety model with `tmp_path`. Pass `enforce_safety=False` to bypass the workspace containment when the test root isn't a real workspace.
- **End-to-end compile** — `test_compile_end_to_end.py` runs `build_compilation` against a real (short) fixture video. Slow, gated by an ffmpeg availability check.

332 tests currently pass. Run with `.venv/Scripts/python.exe -m pytest -q` from `api/`.

## Where to look next

- **Ranker + candidate architecture** → the [[ai-expert]] skill.
- **Per-clip effect primitives (zoom, caption, focus internals)** → the [[editing-expert]] skill.
- **How MCP tools wrap these endpoints** → the [[mcp-expert]] skill.
- **Frontend visual language** → the [[frontend-design]] skill.
