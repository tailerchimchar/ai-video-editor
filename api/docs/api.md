# API Reference

Base path: `/api/v1`. Interactive docs at `http://localhost:8000/docs`.

## Assets

| Method | Path | Description |
|---|---|---|
| POST | `/assets/scan` | Walk `OUTPLAYED_MEDIA_DIR`, index `.mp4` files into `assets` |
| GET | `/assets` | List indexed assets |

## Clips (Phase 1)

| Method | Path | Body | Description |
|---|---|---|---|
| POST | `/clips` | `{asset_id, start_seconds, end_seconds}` | Trim a clip via ffmpeg (background job) → returns `job_id` |

## Projects / timeline / render (Phase 1)

| Method | Path | Description |
|---|---|---|
| POST | `/projects` | Create a project |
| POST | `/projects/{id}/timeline/items` | Add a clip to the timeline at a position |
| POST | `/projects/{id}/render` | Concat timeline → `rough_cut.mp4` (job) |

## Analyzer (Phase 2)

| Method | Path | Description |
|---|---|---|
| POST | `/assets/{id}/candidates` | Generate `HighlightCandidate`s (job) |
| GET | `/assets/{id}/candidates` | List raw candidates for the asset |
| POST | `/assets/{id}/rank` | LLM-rank candidates (job, 1 Anthropic call) |
| GET | `/assets/{id}/rankings` | Ranked suggestions JSON |
| POST | `/assets/{id}/highlights` | Cut every kept suggestion into an organized folder (job) |
| GET | `/assets/{id}/highlights` | The folder's `index.json` (path + per-clip results) |
| POST | `/clips/batch-highlights?game=&limit=` | Organize a game's short Outplayed clips into one folder (job, **no LLM, $0**) |
| POST | `/assets/{id}/transcribe` | Local Whisper STT → store transcript segments (job, heavy, CPU; run once) |
| GET | `/assets/{id}/transcript` | Stored transcript segments (start/end/text) |
| POST | `/assets/{id}/index` | Embed transcript chunks into the vector store (sqlite-vec, ~25s rolling windows). Requires `/transcribe` first. |
| GET | `/search?q=&limit=&asset_id=` | Semantic search over indexed transcripts → matched clips (asset/start/end/text/distance) |

## Edits (Phase 3, Milestone A)

Per-clip post-production primitives. Each operates on an asset + sub-range
and renders a new `.mp4` into `WORKSPACE/edits/<asset-stem>/`.

| Method | Path | Description |
|---|---|---|
| POST | `/edit/zoom` | Crop+scale on an ROI (`center`, `scoreline_lol`, `minimap_lol`, `full`, or explicit fractional box). Background job. |
| POST | `/edit/caption` | Burn timestamped transcript text (TikTok-style). When `text` is omitted, auto-pulls segments overlapping the clip's window. |
| POST | `/edit/focus` | Spotlight: dim the frame, keep a soft circle at `(x, y, radius)` (all fractions of `min(w, h)`). |
| GET | `/edit/{edit_id}` | The persisted edit row (kind, params, output_path). |

All three accept an `aspect` parameter: `"16:9"` (default, passthrough)
or `"9:16"` (center-crop + scale to 720x1280 for TikTok/Reels).

## Compile (Phase 3, Milestone B)

Stitch a recording's kept ranked highlights into one polished video.

| Method | Path | Description |
|---|---|---|
| POST | `/edit/compile` | Per-clip render (caption + 0.3s fade-in/out + aspect) → concat → optional music mix. Background job. |
| GET | `/edit/compile/{id}` | Compilation row + inlined index.json |

Body: `{asset_id, aspect="16:9", order="chronological"|"hype", limit?, fade_seconds=0.3, music_path?, music_volume=0.25}`.

Requires `/assets/{id}/rank` to have been run; pulls transcript
segments from the DB for auto-captioning each clip. Outputs to
`WORKSPACE/compilations/<asset-stem>_<ts>/compilation.mp4` (plus
`_parts/` and `spec.json` and `index.json`).

`POST /edit/compile` now returns `{job_id, compilation_id}` — use
`compilation_id` for the iterative-editing endpoints below.

### Iterative compilation editing (Milestone B+)

`spec.json` is the **source of truth**. Each mutation re-renders only
the affected clip and re-concats (instant), so edits feel sub-5s.

| Method | Path | Description |
|---|---|---|
| GET | `/edit/compile` | List rendered compilations (optional `asset_id` filter) |
| GET | `/edit/compile/{id}/clips` | Per-clip listing with reel + source timestamps + current effects |
| POST | `/edit/compile/{id}/effect` | Add a zoom/focus/caption effect to one clip; re-render that clip |
| POST | `/edit/compile/{id}/extend` | Grow a clip's source window (`before`/`after` seconds) |
| POST | `/edit/compile/{id}/remove` | Drop a clip from the reel (re-concat only) |

All mutating bodies share `clip_ref` — accepts `"2"` (1-based index),
a UUID prefix, or a `"M:SS"` time string (reel time first, then source).

`transcribe` is a separate explicit job (CPU STT is slow on long VODs).
Once stored, `candidates` reuses the transcript and the
`transcript_keyword` source turns spoken hype/funny cues into ranked
candidates — no re-transcription.

`/highlights` requires `/rank` to have run. Output layout:

```
WORKSPACE/highlights/<game>/<MM-DD-YYYY>_<champion|HHhMM>/
    01_kill_4m18s.mp4 …   index.md   index.json
```

The folder name is a deterministic function of the recording + its
candidates (champion comes from Riot data), so GET re-derives it.

## SFX (Sprint #2)

Sourcing audio templates for per-game stingers (used by sprint #3's
`add_sfx` and sprint #4's audio-event detector).

| Method | Path | Description |
|---|---|---|
| POST | `/sfx/extract` | Cut an audio span out of an asset into `WORKSPACE/media_library/<game>/sfx/<file>.wav` (mono 44.1 kHz). Background job. |

Body: `{asset_id, game, sound_name, start_seconds, end_seconds}`. The
`game` value can be any alias declared in the profile (`"League of
Legends"`, `"lol"`, `"league"` — all land in `media_library/league/`).
If `sound_name` isn't declared in the profile, the file is still
saved but the job output warns you to add the entry.

## Jobs

| Method | Path | Description |
|---|---|---|
| GET | `/jobs/{id}` | Poll status: `pending`/`running`/`completed`/`failed`; includes a human `summary` |

## Typical flow

```bash
curl -X POST localhost:8000/api/v1/assets/scan
ID=$(curl -s localhost:8000/api/v1/assets | jq -r '.[0].id')

# generate + inspect candidates
J=$(curl -s -X POST localhost:8000/api/v1/assets/$ID/candidates | jq -r .job_id)
# poll /jobs/$J until completed
curl -s localhost:8000/api/v1/assets/$ID/candidates | jq

# rank (one LLM call) + read suggestions
J=$(curl -s -X POST localhost:8000/api/v1/assets/$ID/rank | jq -r .job_id)
# poll /jobs/$J until completed
curl -s localhost:8000/api/v1/assets/$ID/rankings | jq
```

### Ranked suggestion shape

```json
{
  "candidate_id": "…",
  "keep": true,
  "funny_score": 0.2,
  "hype_score": 0.9,
  "story_score": 0.6,
  "suggested_start_seconds": 124.0,
  "suggested_end_seconds": 133.5,
  "reason": "Triple kill securing the objective."
}
```
