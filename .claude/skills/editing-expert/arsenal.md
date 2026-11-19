# Arsenal: what this pipeline can render today

STATUS: GENERATED-PENDING. Hand-written 2026-11-19 from the MCP tool list
(87 tools). Once `api/src/ai_video_editor/registry.py` exists this file is
generated from it and must not be edited by hand.

## Formats (edit plan `format`)
`hype_montage` · `story_short` · `vo_short` · `funny_short` · `arc_montage` (planned, not renderable yet)

## Per-clip effects (edit plan `effects[].kind`)

| kind | params | scope | MCP tool |
|---|---|---|---|
| `zoom` | `preset` (ROI preset name) or `region` {x,y,w,h fractions} | whole clip | `zoom_clip`, `zoom_compilation_clip` |
| `focus` | `region` | whole clip | `focus_clip`, `focus_compilation_clip` |
| `caption` | `text` or auto from transcript; `style` classic/tiktok | whole clip | `caption_clip`, `caption_compilation_clip`, `tiktok_caption_clip`, `tiktokify_clip_captions`, `segment_caption_clip`, `edit_clip_captions`, `add_clip_caption`, `remove_clip_caption` |
| `caption_video` | burn transcript captions on a whole video | whole video | `caption_video` |
| `beat_flash` | automatic on detected beats when music is set | whole short | inside `compile_shorts` / montage tools |

Not renderable yet (reject or substitute): `slow_mo`, `speed_ramp`,
`transition` (cross-clip), `time_scoped` versions of zoom/focus/caption,
`pulse_zoom` (exists in code, not wired), `beat_snap` (exists, not wired).

## Sound (edit plan `sfx[].sound`, `music`)

| op | MCP tool |
|---|---|
| one-shot SFX at a timestamp, ducks source | `add_sfx_at` |
| extract an SFX from footage into the library | `extract_sfx` |
| download / cache a song (YouTube URL or local file) | `download_music` |
| music bed on a short or montage (flat mix, no envelope) | inside `compile_shorts` / montage tools |

SFX names come from `WORKSPACE/media_library/<game>/sfx/`. Ask the session
to list the folder before naming one.

## Cards and intros (edit plan `cards[]`)

| op | MCP tool |
|---|---|
| text intro card | `create_text_intro`, `render_intro` |
| logo intro (Noodlz preset) | `create_intro`, `get_default_intro`, `set_default_intro`, `list_intro_presets`, `update_intro` |
| place an intro in a compilation | `set_compilation_intro`, `insert_compilation_intro_at`, `insert_compilation_intro_after`, `clear_compilation_intro` |

## Structure (edit plan `order`, `source_clips`)

| op | MCP tool |
|---|---|
| cut a highlight from a source by window | `cut_highlights`, `batch_clip_highlights`, `trim_source_to_clip` |
| cut on ground-truth kill anchors (zero LLM cost, Outplayed log) | `auto_cut_kills_from_source`, `list_kill_anchors` |
| compile a reel from ranked candidates | `compile_highlights` |
| compile shorts by topic | `compile_shorts`, `list_shorts_topics`, `list_shorts` |
| montages | `compile_outplayed_montage` (+ `preview_`), `compile_cross_asset_montage` (+ `preview_`), `compile_custom_montage` |
| reorder / insert / remove / extend clips in a compilation | `reorder_compilation_clips`, `reorder_compilation_clips_explicit`, `insert_compilation_clip`, `remove_compilation_clip`, `extend_compilation_clip` |
| undo | `list_compilation_history`, `revert_compilation` |
| finalize / clean up | `finalize_compilation`, `cleanup_compilation`, `set_compilation_labels` |
| split a long VOD into games | `split_vod_into_games` |

## Review (not effects, but the panel uses them)

`watch_montage` (VLM critic over the whole montage, ~$0.02/60s) ·
`vlm_review_compilation` · `list_session_buckets` (content-type router) ·
`mentor_record_correction` / `mentor_recent_corrections` ·
`get_transcript`, `transcribe_asset`, `search_clips` · `analyze_asset`,
`rank_asset`, `generate_candidates`, `list_candidates`.
