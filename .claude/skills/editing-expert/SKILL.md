# Editing Expert

---
name: editing-expert
description: Use when adding new per-clip effects (zoom / caption / focus / etc.), tuning ROI presets, wiring MCP + backend + frontend for a new editing op, or reasoning about spec.json + journal + revert.
---

## When to invoke

Reach for this skill when the task touches ANY of:

- The three ffmpeg-backed effect primitives (`apply_zoom`, `apply_caption`, `apply_focus`) in `api/src/ai_video_editor/edits.py`
- ROI presets (League HUD regions) or the `resolve_roi()` dict-vs-preset contract
- Caption rendering — the unified `caption_filters()` drawtext chain, the `STYLE_PRESETS` recipes, or the tiktok data transformation
- Aspect handling (`16:9` passthrough vs `9:16` mobile crop)
- Adding a new effect kind (needs backend + spec reader + MCP tool + frontend)
- The `effects[]` list on clips in `spec.json`
- Iterative compile editing — the journal (`spec_history.jsonl`), revert (`revert_steps` / `revert_to_version`), and the `_do_edit` mutator+render+journal pattern in `routers/edits.py`
- Extend / reorder / insert / remove / intro placement
- `ClipActionsPanel.tsx` preset rows, `useCompilation` mutations, or the caption editor

If the task is about compilation lifecycle (build → mutate → render → revert) more broadly, see [[compilation-expert]]. If it's about the MCP tool surface, see [[mcp-expert]].

## Recent changes (as of 2026-07-04) — read before editing

Three things landed after the initial skill was written; anyone touching related code should know:

- **Rampage windowing fix** — `highlights.py::_window_anchor` now unions the anchor rule (`anchor - pre .. anchor + post`) with the ranker's `suggested_start_seconds` / `suggested_end_seconds`. Capped at `settings.highlight_max_seconds` (default 120s). When the ranker clusters multiple kills into one candidate (a rampage), its window is used to extend outward — no more chopped nexus rampages. `_window_anchor` used to be pure anchor-rule; do NOT revert without reading the tests at `tests/test_highlights.py::test_window_anchor_extends_to_ranker_window_when_wider`.

- **Label promotion for audio-derived candidates** — `candidates/service.py::promote_audio_event_types` runs at end of `compute_candidates`. Any `audio_peak` or `transcript_keyword` candidate whose window is within `_AUDIO_PROMOTION_TOLERANCE_SECONDS` (20s) of a Riot/cv_kda kill/death/assist gets its `event_type` promoted to the visible label. Old `metadata.promoted_from` preserves the original label for trace visibility. The historical `funny_audio` event_type is now `audio_peak` at emit time; `event_window_overrides` in `config.py` keeps both keys for backward compat with pre-2026-07 DB rows.

- **Chronological highlights folder** — `highlights.py::build_highlights` sorts kept rankings by `suggested_start_seconds` (was: `hype_score` descending). The folder reads as a log of the game; filenames `01_..., 02_...` walk play time. If a caller needs a hype-first order, that's the `compile_highlights(order="hook"|"hype")` layer's job, not the highlights folder.

## Recent: Shorts pipeline (informal, MVP)

There is no `compile_shorts` endpoint or MCP tool yet — Shorts are being hand-generated via direct ffmpeg from validated clips (see the Notion doc for the pro-tier rubric). When productizing:

- 9:16 aspect (`aspect="9:16"` in existing compile path — passes through `aspect_filter()`)
- Blur-fill background pattern (16:9 gameplay centered vertically, blurred+scaled version fills top/bottom bars). Filter graph shape:
  ```
  [0:v]split=2[a][b];
  [a]scale=720:-2:flags=lanczos,setsar=1[fg];
  [b]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,boxblur=25:2,setsar=1[bg];
  [bg][fg]overlay=(W-w)/2:(H-h)/2
  ```
- ~30-55s duration cap (Shorts algorithm sweet spot)
- Output goes to `WORKSPACE_DIR/shorts/<game>_<date>/`
- Pro-tier follow-ups (not in MVP): hook-edit (payoff-first), kill-moment zoom via `apply_zoom` at the peak, music bed (royalty-free), dynamic KDA overlay from cv_kda data, face-cam bubble (blocked on user recording it)

## Mental model

Two layers, cleanly separated.

**Layer 1 — pure filter builders.** `resolve_roi()`, `aspect_filter()`, `caption_filters()`, `resolve_caption_style()`, `explode_segments_to_words()`. No I/O. Take dicts / strings, return ffmpeg filter fragments. Unit-testable without ffmpeg.

**Layer 2 — ffmpeg wrappers.** `apply_zoom()`, `apply_caption()`, `apply_focus()`. Each is exactly ONE `ffmpeg` subprocess call with `-c:a copy` (audio stream-copy) and `-y` overwrite. Filter graph built entirely from validated Pydantic inputs (`ZoomRequest` / `CaptionRequest` / `FocusRequest` in `api/src/ai_video_editor/routers/edits.py:140-153`). No `shell=True`. Uses `_run_ffmpeg()` which keeps only the tail of stderr because ffmpeg writes its ~2KB banner FIRST and the real error last (`edits.py:297-307`).

The one-pass optimisation matters: `_src_args()` puts `-ss` BEFORE `-i` and passes `-t` for duration (`edits.py:310-314`) so trim + filter happen in a single ffmpeg invocation — no separate cut step.

**Two rendering surfaces.** The primitives serve two callers:

1. **Standalone edit endpoints** (`POST /edit/zoom`, `/edit/caption`, `/edit/focus` in `routers/edits.py:209-297`) — each produces one .mp4 in `WORKSPACE/edits/<asset-stem>/<kind>_<ts>.mp4` via a background job. Used before there's a compilation.
2. **Per-clip filter chain in compile** (`_build_clip_filterchain` in `compile.py:272-355`) — reads `clip["effects"]` from the spec and stacks the same filter fragments inline (no per-effect subprocess). Effects render in order: `effects → captions → fade → aspect tail → label`.

The pure filter builders are shared between both surfaces. That's the point of the split — `resolve_roi()` and `caption_filters()` are one implementation, two consumers.

## The three primitives

### `apply_zoom(...)`

`edits.py:317-342`. Filter chain:

```
crop={w}:{h}:{x}:{y}, scale=iw*2:ih*2 [, {aspect_tail}]
```

The `crop` fragment comes straight from `resolve_roi(roi, factor)`. Post-crop `scale=iw*2:ih*2` upscales 2x so the zoomed content stays sharp on the target canvas. The `aspect_tail` (empty for `16:9`, `crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=720:1280` for `9:16`) runs last.

Audio is stream-copied. Duration is `end - start`.

### `apply_caption(...)`

`edits.py:345-369`. Filter chain:

```
drawtext=fontfile=...:text=...:enable='between(t,a,b)' [, drawtext=... , ...] [, {aspect_tail}]
```

One `drawtext` filter per input segment (see `caption_filters()` below). Segments with no text are skipped (`edits.py:200-202`). When all segments are empty the chain falls back to `null` (`edits.py:358`) so ffmpeg doesn't error out on empty vf.

`clip_start_offset` — passed as the clip's source-VOD start — is subtracted from each segment's `start_seconds` / `end_seconds` so the `between(t,a,b)` times are relative to the clip's own timeline starting at 0 (`edits.py:184-186`, `caption_filters()` at `edits.py:205-206`).

### `apply_focus(...)`

`edits.py:372-407`. Filter chain:

```
geq=lum='if(lt(hypot(X-{cx},Y-{cy}),{rr}),lum(X,Y),lum(X,Y)*{dim})':cb=cb:cr=cr [, {aspect_tail}]
```

Luma-only mask: inside the circle luma is preserved (`lum(X,Y)`), outside it's multiplied by `dim` (0..1). Chroma planes pass through untouched (`cb=cb:cr=cr`) so colours don't shift, only brightness. Slow per-frame (`geq` runs on the CPU per pixel) but accurate and needs no extra deps — noted in the docstring at `edits.py:384-387`.

`cx` / `cy` are `iw*x_frac` / `ih*y_frac`. `rr` is `min(iw,ih)*r_frac` — using `min` so a wide 16:9 frame doesn't make the circle wider than tall.

## ROI presets

`_ROI_PRESETS: dict[str, tuple[str, str, str, str]]` at `edits.py:23-49`. Each maps a preset name to `(out_w, out_h, x, y)` as ffmpeg expression fragments in `iw` / `ih` units so they auto-scale to any source resolution.

The center preset uses `{f}` for the zoom factor — `resolve_roi()` at `edits.py:63-77` substitutes via `p.format(f=factor)`. All others hard-code fractional dimensions.

Current presets:

| Preset | `(w, h, x, y)` fragments | What it targets |
|---|---|---|
| `full` | `(iw, ih, 0, 0)` | Whole frame — sentinel for zoom-only effects |
| `center` | `(iw/{f}, ih/{f}, (iw-iw/{f})/2, (ih-ih/{f})/2)` | Center crop scaled by factor |
| `scoreline_lol` | `(iw*0.20, ih*0.05, iw*0.78, 0)` | League top-right HUD strip: team kills, personal KDA, CS, clock. Updated 2026-05-27 from a real screenshot — modern League HUD is TOP-RIGHT, not top-center |
| `minimap_lol` | `(ih*0.22, ih*0.22, iw-ih*0.23, ih*0.77)` | Bottom-right minimap. Square (both `ih*0.22`) so it stays square regardless of source aspect |
| `champion_portrait_lol` | `(iw*0.07, ih*0.14, iw*0.16, ih*0.86)` | Bottom-left champion icon + summoner spells. Mirrors `profiles/league.toml [regions.champion_portrait]` |
| `killfeed_lol` | `(iw*0.22, ih*0.28, iw*0.78, ih*0.08)` | Top-right kill announcements. Mirrors `profiles/league.toml [regions.killfeed]`. Also drives CV killfeed detection |
| `item_bar_lol` | `(iw*0.10, ih*0.08, iw*0.55, ih*0.88)` | Bottom-center six items + trinket. Mirrors `profiles/league.toml [regions.item_bar]` |
| `streamcam_lol` | `(iw*0.19, ih*0.22, iw*0.66, ih*0.78)` | Twitch stream-overlay cam region for League streamers. Cam sits LEFT of the minimap; right edge stops at x=0.85 (minimap left edge). Tune per your OBS layout |

### The two-source-of-truth problem

The League ROI values in `edits.py` are HAND-MIRRORED from `api/src/ai_video_editor/profiles/league.toml` — the profile TOML is CV configuration (used by killfeed / scoreboard detection), and the edits.py entries are used by the zoom filter. There's no runtime import between them by design (`profiles/league.toml` isn't a runtime dep of `edits.py`).

That means: when you tune a region in `profiles/league.toml`, you MUST hand-update the matching entry in `_ROI_PRESETS`. Grep both files:

```
api/src/ai_video_editor/edits.py:_ROI_PRESETS
api/src/ai_video_editor/profiles/league.toml:[regions.*]
```

The comments in `edits.py` explicitly call out this mirror relationship on the entries where it applies (`edits.py:36`, `:38`, `:41`).

### Custom (non-preset) ROI

`resolve_roi()` accepts either a preset name OR a dict `{"x": 0.5, "y": 0.5, "w": 0.4, "h": 0.4}` (`edits.py:71-73`). Each value is a fraction 0..1 of the source dimensions. The Pydantic type on the endpoint is `roi: str | dict = "center"` (`routers/edits.py:142`). No factor injection for dicts — the caller is expected to size the ROI themselves.

Endpoint validation: unknown preset names raise `ValueError` (`edits.py:76`), which bubbles up as an unhandled 500. If you add a new preset, keep the name kebab-cased with a `_<game>` suffix for game-specific regions (`scoreline_lol`, `minimap_lol`, etc.) so it's obvious which game the coords are calibrated for.

## Aspect handling

`aspect_filter(aspect: str)` at `edits.py:52-60`. Only two aspects supported today:

- `"16:9"` (default) — returns `""` (empty, passthrough)
- `"9:16"` — returns `"crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=720:1280"` (centered crop to 9:16, then scale to canonical 720x1280 mobile canvas for TikTok/Reels)

Applied as the LAST filter fragment in every primitive so all upstream effects see the source aspect (crop matters at the end, not before the zoom's ROI). See `apply_zoom` at `edits.py:329-331` and the same pattern in `apply_caption` at `edits.py:355-357` and `apply_focus` at `edits.py:394-396`.

The `CompileRequest.aspect` and per-primitive request literal is `Literal["16:9", "9:16"]` (`routers/edits.py:137, 303`) — Pydantic rejects any other value at request-parse time.

## Caption styles

### The unified renderer

`caption_filters(segments, clip_start_offset, *, fontfile=None)` at `edits.py:176-216`. ONE renderer for ALL captions — emits one `drawtext` filter per segment.

For each segment:

1. Sanitise `text` via `_escape_drawtext()` (`edits.py:80-100`) — this dodges ffmpeg's filter-graph quoting hell by:
   - Replacing straight apostrophes with the curly `’` (U+2019) — needs no escaping
   - Backslash-escaping `:` and `,` (filter-graph separators)
   - Dropping backslashes wholesale (assumed to be OCR-ish artifacts)

2. Resolve the segment's style via `resolve_caption_style(s.get("style"))` — merges the preset's defaults with any per-segment overrides.

3. Emit the drawtext filter with the resolved fontfile (via `_escape_fontfile_path()`, see below), text, fontsize, colors, border, center-x position (`x=(w-text_w)/2`), y-position expression, and `enable='between(t,a,b)'` where `a`/`b` are segment times minus `clip_start_offset`, clamped so `b >= a + min_dur` so no segment vanishes faster than `min_duration_seconds`.

### The two current presets

`STYLE_PRESETS` at `edits.py:152-155` — flat dict from preset name to defaults.

**`default`** (`edits.py:133-140`):

```python
{"fontsize": 36, "y_position": "h-th-60", "color": "white",
 "border_width": 4, "border_color": "black", "min_duration_seconds": 0.10}
```

Bottom-center Netflix-style captions. `y_position="h-th-60"` = 60px above the bottom edge of the frame (`h` is frame height, `th` is text height).

**`tiktok`** (`edits.py:143-150`):

```python
{"fontsize": 80, "y_position": "h/8", "color": "white",
 "border_width": 6, "border_color": "black", "min_duration_seconds": 0.05}
```

Top-center hero text — big, thick border, high on the frame (`h/8` = 1/8 down from top). Shorter `min_duration_seconds` because word-by-word segments are typically < 0.5s each.

### Per-segment overrides

`resolve_caption_style(style: dict | None)` at `edits.py:158-173`:

- `None` → returns `dict(_DEFAULT_STYLE)`
- Otherwise: look up `style["preset"]` (defaults to `"default"`), copy that preset's fields, then overlay any non-None keys from `style` on top. `preset` and `None` values are skipped.

So a segment `{"style": {"preset": "tiktok", "color": "yellow"}}` renders as tiktok defaults with the color swapped.

### TikTok mode is a DATA TRANSFORMATION

Not a separate code path. `explode_segments_to_words(segments, style_preset="tiktok")` at `edits.py:250-278` walks each segment, pulls per-word timings via `_segment_words_with_fallback()` (Whisper `words[]` when available, else even-split across the segment's span at `edits.py:219-247`), and emits ONE segment per word tagged with `style={"preset": style_preset}`.

The renderer treats these tagged word-segments exactly like any other segments — same `caption_filters` code path, no branching.

The compile pipeline handles two legacy migration cases in `_build_clip_filterchain` at `compile.py:302-319`:

- If `clip.caption_mode == "tiktok"` AND no segment carries a style, explode+tag at read time (legacy compat, doesn't touch persisted spec).
- Otherwise render segments as-is.

The current mutator for going INTO tiktok mode is `spec_tiktokify_clip(spec, clip_idx)` at `compile.py:1183-1205` — calls `explode_segments_to_words` and stores the exploded segments into `caption_segments`, then clears the legacy `caption_mode` field so the new data model is the single source of truth going forward.

The legacy `caption_filters_tiktok()` alias at `edits.py:284-294` is deprecated — new callers should pass already-exploded + styled segments to `caption_filters` directly.

### Windows fontfile quirk

`_escape_fontfile_path(path)` at `edits.py:103-112` forward-slashes backslashes AND backslash-escapes colons. The docstring calls out why: ffmpeg's parser strips single-quotes BEFORE applying colon-as-separator, so single-quoting alone leaves `fontfile=C` looking like a bare option and the rest gets misparsed. Both quotes AND colon-escaping are needed on Windows.

If you build a new caption-adjacent filter that consumes a font path, use `_escape_fontfile_path()` — don't reinvent this.

## The `effects[]` array

Each clip in `spec.json` carries `effects: list[dict]` — initialised empty by `spec_from_rankings` at `compile.py:511`. Effects apply to the WHOLE clip today (there's no time scoping yet).

Renderer reads from `_build_clip_filterchain` at `compile.py:286-299`:

```python
for eff in clip.get("effects", []) or []:
    kind = eff.get("kind")
    if kind == "zoom":
        w, h, x, y = resolve_roi(eff.get("roi", "center"), eff.get("factor", 2.0))
        chain.append(f"crop={w}:{h}:{x}:{y}")
        chain.append("scale=iw*2:ih*2")
    elif kind == "focus":
        cx = f"iw*{eff.get('x', 0.5)}"
        cy = f"ih*{eff.get('y', 0.5)}"
        rr = f"(min(iw,ih)*{eff.get('radius', 0.2)})"
        dim = eff.get("dim", 0.3)
        mask = f"if(lt(hypot(X-{cx},Y-{cy}),{rr}),lum(X,Y),lum(X,Y)*{dim})"
        chain.append(f"geq=lum='{mask}':cb=cb:cr=cr")
```

Caption effects are handled separately at `compile.py:321-338` — for `kind == "caption"` with a `text` field, the effect is upgraded into a synthetic overlay segment spanning the whole clip and appended to the drawtext chain AFTER any transcript-segment captions. That's how MCP `caption_compilation_clip(...)` ends up burning "CLUTCH" or "PENTAKILL" over an already-captioned clip.

The exact shape of each effect entry:

```json
{"kind": "zoom", "factor": 1.5, "roi": "minimap_lol"}
{"kind": "focus", "x": 0.5, "y": 0.5, "radius": 0.2, "dim": 0.3}
{"kind": "caption", "text": "PENTAKILL"}
```

`_effect_from_body()` at `routers/edits.py:607-626` picks only the fields relevant to each kind so the body of `POST /edit/compile/{id}/effect` can send a superset — extra fields are dropped, missing required fields (caption `text`) raise 400.

### Adding an effect via HTTP

`AddEffectBody` at `routers/edits.py:560-571` accepts one `clip_ref` (see [[compilation-expert]] for clip_ref semantics) plus a `kind` literal and every possible field. `add_clip_effect()` at `routers/edits.py:629-653` resolves the clip index via `resolve_clip_ref()`, picks the effect dict, mutates the spec via `spec_add_effect()`, then runs the standard render+journal flow via `_do_edit()`.

## Extend and reorder

### `extend_clip`

Mutator: `spec_extend_clip(spec, clip_idx, before, after)` at `compile.py:827-833`. Pure — deep-copies the spec, adjusts `start_seconds` and `end_seconds` on the target clip, returns the new spec + `{clip_id}` as the dirty set. `before`/`after` are seconds ADDED to the current start / end. Start is clamped to `>= 0`; end is left unclamped (ffmpeg tolerates end past EOF).

Endpoint: `POST /edit/compile/{compilation_id}/extend` at `routers/edits.py:656-701`. Body is `ExtendBody(clip_ref, before, after)`. In addition to the standard `_do_edit` flow, this endpoint ALSO logs a feedback event (`_log_feedback` with `action="extend"`, `delta_before`, `delta_after`, and the clip's `event_type` snapshot) — user extensions are training signal for tuning per-event windows.

Frontend surface: the filmstrip's edge-drag handles land here after the drag commits. See `web/src/hooks/useCompilation.ts::extend` mutation which calls `extendClip(compilationId, args)`.

### `reorder_clips_explicit`

Mutator: `spec_reorder_clips_explicit(spec, clip_ids)` at `compile.py:1001-1042`. Takes the FULL new order as `list[str]`. Server-side validation refuses to silently lose clips:

- Duplicate ids in `clip_ids` → `ValueError` (checked before set comparison so dupes don't collapse and look like missing)
- Set of `clip_ids` != set of existing clip ids → `ValueError` with both `missing:` and `unknown:` breakdowns

Returns new spec + ALL `clip_ids` as dirty. When `show_clip_numbers` is on, every clip's `#NN` label changed and needs re-encoding; when labels are off, cached parts are reused and only concat order changes.

Endpoint: `POST /edit/compile/{compilation_id}/reorder_explicit` at `routers/edits.py:1219-1250`. `ValueError` from the mutator is caught and converted to 400 (`routers/edits.py:1247-1248`).

### `reorder_clips` (by mode)

`spec_reorder_clips(spec, mode)` at `compile.py:1045-1117`. Modes: `chronological`, `hype`, `funny`, `story`, `hook`. Preserves intro positions (event_type=="intro" clips stay put — they're chapter markers, not gameplay). See [[compilation-expert]] for the ordering semantics.

## Intros

Full intro lifecycle:

1. **Create** — `POST /intros` (creates config + renders) or `POST /intros/text` (text-based intro). Landing page: `WORKSPACE/intros/<name>/{intro.json, intro.mp4}`.
2. **Set default** — `POST /intros/default {intro_name}` or clear with `null`.
3. **Apply to a compilation** — three variants:
   - `POST /compile/{id}/intro {intro_name}` = prepend at position 1 (REPLACES any existing intro at slot 0). Uses `spec_set_intro_clip()` at `compile.py:894-940`.
   - `POST /compile/{id}/insert_intro {after_clip: "3"}` = insert AFTER clip #3 (no replace).
   - `POST /compile/{id}/insert_intro {position: 5}` = insert AT position 5.
4. **Remove** — `DELETE /compile/{id}/intro` calls `spec_clear_intro_clip()` at `compile.py:987-995`.

`_resolve_intro_or_404()` at `routers/edits.py:905-926` is the shared preamble: falls back to the workspace default, 404s on unknown, 409s on "exists but no rendered mp4" (with a hint to `POST /intros/{name}/render` first).

Intro clips are inserted as regular clips with:
- `asset_id: None` (not sourced from an indexed asset)
- `asset_path: <intro.mp4 absolute path>`
- `asset_filename: "intro:<name>"`
- `event_type: "intro"`
- `intro_name: <name>` carried on the clip so a later "what intro is on this reel?" lookup doesn't need to introspect asset_filename

`_render_clip_part` at `compile.py:358-402` only reads `clip["asset_path"]`, not `asset_id` — so intros render through the same code path as gameplay clips.

MCP tools (all in `mcp/src/ai_video_editor_mcp/server.py`):

- `list_intros()` — line 702
- `set_default_intro(intro_name)` — line 990
- `set_compilation_intro(compilation_id, intro_name=None)` — line 767
- `insert_compilation_intro_after(compilation_id, after_clip, intro_name=None)` — line 787
- `insert_compilation_intro_at(compilation_id, position, intro_name=None)` — line 817
- `clear_compilation_intro(compilation_id)` — line 1012
- `list_intro_presets()`, `create_intro`, `create_text_intro`, `get_intro`, `render_intro`, `update_intro` — the intro authoring surface

Intro configs (`intro.json`) live under `WORKSPACE/intros/<name>/` and are patched via `update_intro(...)` which is a PATCH-and-re-render in one shot. Common tweaks called out in the docstring: `logo_scale` (default 0.55), `bounce_pixels` (default 40), `pulse_factor` (default 1.15).

## Iterative editing pattern

`_do_edit(folder, mutator, compilation_id, *, action, details)` at `routers/edits.py:579-604` is the shared flow for every mutator:

1. `load_spec(folder)` — reads `spec.json`
2. `new_spec, dirty = mutator(spec)` — pure mutation, returns dirty clip-id set
3. `save_spec(folder, new_spec)` — persists `spec.json`
4. `render_spec(new_spec, folder, dirty_clip_ids=dirty)` — re-renders ONLY the dirty parts, concat is fast because non-dirty parts are cached in `_parts/`
5. `append_journal(folder, new_spec, action=action, details=details)` — appends to `spec_history.jsonl`

Journal append is best-effort — the edit has already succeeded by the time we're called (`compile_journal.py:59-74`). An OS error here MUST NOT fail the edit. That's why every mutator's `append_journal` call is a plain function, not raised or bubbled.

## The journal + revert

`api/src/ai_video_editor/compile_journal.py`.

**Storage**: `<compilation>/spec_history.jsonl`, one JSON line per snapshot. Append-only. Each line has:

```json
{
  "ts": "2026-05-25T20:00:00+00:00",
  "action": "add_effect:zoom",
  "details": {"clip_ref": "3", "effect": {"kind": "zoom", "factor": 1.5, "roi": "minimap_lol"}},
  "spec": {full spec at that point}
}
```

The full spec is duplicated every entry. Sizes are ~tens of KB per spec so even a 100-edit history is 1-2 MB (`compile_journal.py:30-31`).

Why JSONL: each line is a complete record so a partial write during a crash doesn't corrupt earlier history. Append-only via `open('a')` means no seeking, no rewrites, no lock coordination (`compile_journal.py:8-11`).

**Read helpers**:

- `read_journal(folder) -> list[dict]` — oldest-first. Missing / unreadable / malformed lines are SKIPPED (never raise) — journal is supplementary, never load-bearing (`compile_journal.py:77-99`).
- `summarise_journal(folder) -> list[dict]` — compact view used by MCP `list_compilation_history` and the frontend history rail. Each entry has `version`, `ts`, `action`, `details`, `display` (via `format_action_display`), and `clip_count`.

**Human-readable phrasing**: `format_action_display(action, details)` at `compile_journal.py:129-238` is the SINGLE SOURCE OF TRUTH for how each action shows up in the history rail. Every action key handled today:

- `initial_compile` → `"Initial compile (hook order, limit 12)"`
- `add_effect:zoom` → `"Added zoom to clip #03 (1.5x · minimap)"` (strips `_lol` suffix)
- `add_effect:focus` → `"Added focus to clip #03"`
- `add_effect:caption` → `"Added caption to clip #03 · 'PENTAKILL...'"`
- `extend_clip` → `"Extended clip #03 (+2.0s before · +3.0s after)"`
- `caption_mode:<mode>` → `"Clip #03 → tiktok captions"`
- `edit_captions` → `"Edited captions on clip #03 · 4 segments"`
- `add_caption` → `"Added caption to clip #03 · 'CLUTCH'"`
- `remove_caption` → `"Removed caption from clip #03 (segment #2)"`
- `tiktokify` → `"Clip #03 → TikTok captions (word-by-word)"`
- `labels:on` / `labels:off` → `"Iteration labels on"`
- `insert_clip` → `"Inserted manual clip at position 3"` or `"(chronological)"`
- `remove_clip` → `"Removed clip #03"`
- `set_intro` → `"Set intro to 'noodlz_v1'"`
- `clear_intro` → `"Removed intro"`
- `insert_intro_at_position` → `"Inserted intro 'noodlz_v1' after clip #03"` or `at position N`
- `reorder:<mode>` → `"Reordered clips by hype"` (or `explicit`)
- `revert` → `"Reverted to v04"`
- Unknown → falls back to `action.replace("_", " ").replace(":", " · ")` — always shows something readable

`_format_clip_ref(ref)` at `compile_journal.py:241-249` styles numeric refs as `#NN` and passes UUID prefixes / time strings through as-is.

**Revert**: two entry points at `compile_journal.py:256-298`.

- `revert_to_version(folder, version)` — restores spec at 1-based version. Raises `RevertError` when version is out of range or the journal is empty.
- `revert_steps(folder, steps=1)` — walks back N entries from current state (steps=1 = the most recent undoable action, which is the second-most-recent journal entry because the most-recent is the current state). Returns `(restored_spec, version_restored)`.

Both write the restored spec back to `spec.json` via `spec_path(folder).write_text(json.dumps(restored, indent=2), ...)`.

Endpoint: `POST /compile/{compilation_id}/revert` at `routers/edits.py:1351-1396`. Body accepts `to_version` OR `steps` (steps default 1). `to_version` wins if both provided. Re-renders with ALL clips marked dirty (cached parts may not match the restored spec). Journals the revert itself so the revert is also undoable. Logs a feedback event with `action="revert"` — strong negative signal.

## Frontend surface

`web/src/components/ClipActionsPanel.tsx`. The right-column primary editing surface: caption editor on top, League preset rows in the middle, stub buttons for future work at the bottom.

`ZOOM_PRESETS` at `web/src/components/ClipActionsPanel.tsx:30-40` — 6 one-click buttons: `scoreboard`, `minimap`, `champion`, `killfeed`, `cam`, `center`. Every click fires `onAddZoom({ roi, factor: ZOOM_FACTOR })` where `ZOOM_FACTOR = 1.5` (`:23`).

`FOCUS_PRESETS` at `:48-57` — 2 buttons: `on champion` (`x=0.2, y=0.93, radius=0.18, dim=0.4`) and `on center` (`x=0.5, y=0.5, radius=0.25, dim=0.4`). Defaults match the MCP tool defaults so behaviour is consistent across surfaces.

Both button rows use `disabled={effectsBusy}` so double-clicks during an in-flight render are absorbed.

`useCompilation` hook: mutations set — `extend`, `revert`, `editCaptions`, `addCaption`, `removeCaption`, `tiktokify`, `addZoom`, `addFocus`, `reorder`. Each one is a `useMutation` wrapping a fetcher from `web/src/api/edits.ts` with `onSuccess: invalidate` where `invalidate` clears the entire `["compilation", compilationId]` query subtree (metadata + clips + history). Renders take 5-15s per clip; the button's disabled state comes from the mutation's own `isPending`.

The caption editor (`web/src/components/CaptionEditor.tsx`) shows every segment with editable text, per-segment delete, an "add caption" row for inserting a new segment at a specific time, and a "tiktokify" button. The tiktokify button is one-shot destructive — the docstring on `tiktokifyClipCaptions()` (`api/edits.ts`) reminds callers that there's no clean "un-tiktokify" because original segment groupings aren't preserved — you revert via the journal.

There's no time-range UI for effects yet. Every zoom / focus applies to the whole clip. Adding time scoping means: extend the `effects[]` schema with `start_seconds` / `end_seconds` fields, teach `_build_clip_filterchain` to wrap each effect's filter fragment in `enable='between(t,a,b)'`, and add a time picker to `ClipActionsPanel`.

## Adding a new effect kind — checklist

Every new effect owes ALL of the following. Skipping any leaves the feature half-shipped.

1. **Pure filter builder** in `api/src/ai_video_editor/edits.py` — the piece that turns effect params into an ffmpeg filter fragment. Unit-testable without ffmpeg. E.g. for a hypothetical `apply_letterbox`:
   ```python
   def letterbox_filter(top_frac: float, bottom_frac: float) -> str:
       return f"pad=iw:ih:0:0:color=black@1,crop=iw:ih*{1-top_frac-bottom_frac}:0:ih*{top_frac}"
   ```
   Guard against invalid inputs with `ValueError`. Reuse `_escape_*` helpers for any string-that-touches-ffmpeg.

2. **Standalone primitive** `apply_<kind>()` in the same file, following the shape of `apply_zoom` / `apply_focus` — takes source path, out path, start/end, effect-specific kwargs, and `aspect`. Builds the filter chain via the pure builder + `aspect_filter()`, invokes `_run_ffmpeg()`, returns `(ok, err)`. Never raises.

3. **Reader branch** in `_build_clip_filterchain()` at `compile.py:286-299` — the per-clip filter chain in the compile pipeline. Reads the effect from `clip["effects"]` and appends the SAME filter fragment your pure builder emits. Don't call the standalone `apply_<kind>()` here — that spawns a subprocess; the compile path is one big `-vf` chain in a single ffmpeg call.

4. **Pydantic request model** in `routers/edits.py` — a new subclass of `_BaseEditRequest` for the standalone edit endpoint (`ZoomRequest` at `:140` is the template). Fields with `Field(...)` bounds so invalid values 400 at parse time.

5. **Standalone edit endpoint** `@router.post("/<kind>")` in `routers/edits.py` — thin wrapper: validate `end_seconds > start_seconds`, call `_enqueue_edit`, define a `work()` closure that calls `apply_<kind>(...)`, track the background job. See `zoom()` at `routers/edits.py:209-228`.

6. **AddEffectBody kind literal** at `routers/edits.py:561` — extend `Literal["zoom", "focus", "caption"]` to include your kind so `POST /edit/compile/{id}/effect` accepts it.

7. **`_effect_from_body()` branch** at `routers/edits.py:607-626` — pick only the fields relevant to your kind so the persisted spec entry is clean.

8. **Journal display** in `format_action_display()` at `compile_journal.py:147-167` — add a branch under `if action.startswith("add_effect:")` for your kind so the history rail shows something meaningful (not just `"Added <kind> effect to clip #NN"`).

9. **MCP tool** `<kind>_compilation_clip(compilation_id, clip_ref, ...)` in `mcp/src/ai_video_editor_mcp/server.py` — follows the shape of `zoom_compilation_clip` at `mcp/server.py:379-391`. Docstring is the LLM's user manual — write it that way (see [[mcp-expert]]).

10. **MCP tool for the standalone variant** `<kind>_clip(asset_id, start_seconds, end_seconds, ...)` if it makes sense standalone. See `zoom_clip()` at `mcp/server.py:223-245`.

11. **Frontend API client** `add<Kind>Effect(compilationId, args)` in `web/src/api/edits.ts` — mirrors `addZoomEffect` / `addFocusEffect`. Same POST target, `kind: "<name>"` body.

12. **Frontend hook mutation** `add<Kind>` in `web/src/hooks/useCompilation.ts` — one more `useMutation` wired to `invalidate` on success.

13. **Frontend UI** — a new preset row in `ClipActionsPanel.tsx` OR a new panel section. Follow the ZOOM_PRESETS / FOCUS_PRESETS ReadonlyArray pattern with `disabled={effectsBusy}` on every button.

14. **Backend tests** — parse-only tests for the pure filter builder (asserting the filter string is what you expect for representative inputs). An end-to-end test hitting the endpoint with a fixture asset is nice-to-have but not required — the ffmpeg call is well-covered by manual smoke.

**Rule of thumb**: if you can invoke the operation only via raw HTTP, it doesn't exist from the user's perspective. The MCP coverage policy in `api/CLAUDE.md` is not optional — see [[mcp-expert]] for the full text.

## ffmpeg safety

Absolute rules (from `api/CLAUDE.md` "Conventions"):

- **No `shell=True`** on any subprocess call. Every ffmpeg invocation goes through `_run_ffmpeg()` (in `edits.py`) or `_run()` (in `compile.py`) with a list-of-args `cmd`.
- **No user-supplied raw args.** ffmpeg commands are built from validated Pydantic inputs only. That means: no user string ever ends up as an ffmpeg CLI flag. `roi` on the standalone zoom endpoint is either a preset name (whitelisted by `_ROI_PRESETS`) or a dict of floats — never a raw string that gets inlined into the filter graph.
- **Filter-graph escaping.** Use `_escape_drawtext()` for any user text and `_escape_fontfile_path()` for any font path. Don't hand-quote — ffmpeg's parser is famously hard to escape correctly (the module comments at `edits.py:80-100` and `:103-112` document the specific gotchas).
- **Source files are immutable.** Only READ from `OUTPLAYED_MEDIA_DIR`. All writes go to `WORKSPACE_DIR`. This is enforced by convention — the primitives take `asset_path` (source) and `out_path` (workspace) as separate arguments and never mutate the source. The delete-source flow (`POST /assets/{id}/delete_source`) is the only place a source file is touched, and it has 5 guardrails documented at `routers/assets.py:130-148`.

Stderr handling: ffmpeg writes a ~2KB banner FIRST and the real error message LAST. `_run_ffmpeg` / `_run` keep only the TAIL (last 1500 chars) so error strings surface the actual problem (`edits.py:305-306`, `compile.py:251-252`).

## Anti-patterns

- **Effects that touch source files.** Source recordings are immutable. Every write goes to `WORKSPACE_DIR`. If your effect needs a temporary file, put it under `settings.workspace_dir / "edits" / <asset-stem>/` (the pattern `_out_path()` at `routers/edits.py:124-129` already uses).

- **Re-encoding when stream-copy would work.** For structural cuts (splitting a VOD into games, concatenating cached parts) use `-c copy`. See `split_segment()` at `api/src/ai_video_editor/splitter.py` and `_concat()` at `compile.py:405-431`. Only use encode when the video content is actually changing (filter chain non-empty). Stream-copies snap to the nearest keyframe (0-2s drift) — acceptable for game boundaries, not for effect start/end.

- **Adding a new caption preset without updating `STYLE_PRESETS`.** `resolve_caption_style()` falls back to `_DEFAULT_STYLE` for unknown preset names (`edits.py:168`) — silent, no warning. If a caller passes `{"preset": "your_new_thing"}` and you never registered it, the segment renders as default and you'll debug the "why does it look wrong" for hours.

- **Hand-quoting ffmpeg filter args.** Use `_escape_drawtext()` and `_escape_fontfile_path()`. `_escape_drawtext` deliberately swaps straight apostrophes for curly (`’`) instead of escaping — the escape doesn't work inside a single-quoted value. Don't try to be clever.

- **Forgetting the journal append after a mutation.** The `_do_edit` helper handles this for every existing mutator. If you add a new endpoint that bypasses `_do_edit`, you owe `append_journal(folder, new_spec, action=..., details=...)` yourself. Missing journal entries mean the history rail lies and revert can't roll back the change.

- **Adding a mutator without the MCP mirror.** The coverage policy from `api/CLAUDE.md`: every mutating endpoint gets a matching MCP tool. Read-only inspection endpoints get tools too when they're part of an iterative workflow. See [[mcp-expert]] for naming conventions.

- **New ROI preset that isn't mirrored from `profiles/league.toml`.** The two files are hand-mirrored on purpose (no runtime import). If you add a preset that ISN'T in the profile too, the CV detection and the zoom filter will drift out of sync. Either update both, or don't add it.

- **Skipping the frontend surface when an editing op exists only in MCP.** The user drives editing visually. If a new mutator only reaches through Claude / MCP, the visual editor doesn't know about it and iterative feedback loops break. Wire the button.

- **Applying an effect to more than one clip in a single mutator.** Every current mutator returns a `dirty` set of clip ids (usually just one). If you write a bulk mutator, be sure to mark ALL affected clip ids so `render_spec` re-encodes them. Missing a clip in the dirty set means the cached part serves stale content.

- **Assuming word-level timings exist.** `_segment_words_with_fallback()` at `edits.py:219-247` even-splits when `seg.words` is missing. If you write new caption logic and rely on `seg.words[i].start`, guard against the empty case or route through `_segment_words_with_fallback` — pre-word-timestamps transcripts still exist in the DB.

- **Editing `caption_mode` directly on a persisted spec.** The current model is per-segment `style`. `caption_mode` is a LEGACY field kept alive by a migration branch at `compile.py:310-319`. Any new caption feature should tag segments with `style`, not toggle `caption_mode`. The `spec_tiktokify_clip()` mutator clears `caption_mode` explicitly (`compile.py:1204`) to keep the new model as the single source of truth going forward.
