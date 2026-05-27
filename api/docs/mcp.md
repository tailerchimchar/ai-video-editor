# MCP Server

Drive the whole pipeline by *talking to Claude* instead of clicking
Swagger. The MCP server is a thin, decoupled adapter over the HTTP
API — it only needs the API URL, never the app settings.

> **The MCP lives in a separate repo.** As of v0.2 the MCP server
> moved to its own repo
> [`ai-video-editor-mcp`](https://github.com/tailerchimchar/ai-video-editor-mcp).
> Both repos sit as siblings under a shared `ai-video-editor/` parent
> folder (locally: `C:\Users\taile\source\repos\ai-video-editor\mcp`).
> Reasons in the [project README](../README.md#how-the-mcp-works) and
> the sibling repo's README. This backend repo no longer ships the MCP
> code or depends on the `mcp[cli]` package.

## Architecture

```
Claude (Desktop / Code)
   │  MCP stdio
   ▼
ai-video-editor-mcp  (FastMCP)         ← separate repo, separate process
   │  HTTP (localhost:8000)
   ▼
FastAPI app (uv run dev)               ← this repo, must be running
   │
   ▼
candidates / ranker / Riot / ffmpeg
```

The API server is the stable contract; MCP is just an adapter. Both
must be running: the API in one terminal, the MCP server spawned by
Claude.

## Tools exposed

| Tool | What it does |
|---|---|
| `scan_assets()` | Index new recordings |
| `list_assets(game?, limit?)` | List recordings, optional game filter |
| `generate_candidates(asset_id)` | Generate candidates, **waits**, returns per-source counts |
| `list_candidates(asset_id)` | Raw candidates |
| `rank_asset(asset_id)` | LLM rank, **waits**, returns kept suggestions by hype |
| `transcribe_asset(asset_id)` | Local Whisper STT, **waits**, returns segment count + preview |
| `get_transcript(asset_id)` | Stored transcript segments for a recording |
| `index_asset(asset_id)` | Embed transcript chunks for semantic search (local, $0) |
| `search_clips(query, limit?, asset_id?)` | Natural-language clip search across indexed transcripts |
| `zoom_clip(asset_id, start, end, factor?, roi?, aspect?)` | Crop+scale around an ROI preset or box; renders a new .mp4 |
| `caption_clip(asset_id, start, end, text?, aspect?)` | Burn captions; defaults to auto-pull from the transcript |
| `focus_clip(asset_id, start, end, x, y, radius, dim?, aspect?)` | Spotlight: dim the frame, keep a soft circle highlighted |
| `compile_highlights(asset_id, aspect?, order?, limit?, fade_seconds?, music_path?, music_volume?)` | Stitch kept rankings into one polished reel: per-clip auto-captions + fades + optional music mix |
| `list_compilations(asset_id?, limit?)` | Find existing `compilation_id`s to iterate on |
| `list_compilation_clips(compilation_id)` | Per-clip map: reel & source timestamps + current effects |
| `zoom_compilation_clip(comp_id, clip_ref, factor?, roi?)` | Zoom one clip in a rendered compilation; re-renders that clip |
| `focus_compilation_clip(comp_id, clip_ref, x?, y?, radius?, dim?)` | Spotlight effect on one clip |
| `caption_compilation_clip(comp_id, clip_ref, text)` | Overlay an extra caption ("CLUTCH", "PENTAKILL") |
| `extend_compilation_clip(comp_id, clip_ref, before?, after?)` | Grow a clip's source window |
| `insert_compilation_clip(comp_id, asset_id, start, end, position?, event_type?, text?)` | Add a NEW clip from an arbitrary source range; default chronological by source time |
| `remove_compilation_clip(comp_id, clip_ref)` | Drop a clip from the reel |

`clip_ref` accepts: 1-based index (`"2"`), UUID prefix (`"c-bbb"`), or
a time string `"0:32"` (tried as reel time first, then source time).
| `cut_highlights(asset_id)` | Cut kept suggestions into the organized folder, **waits**, returns path + clips |
| `extract_sfx(asset_id, game, sound_name, start_seconds, end_seconds)` | Cut an audio span into `WORKSPACE/media_library/<game>/sfx/<name>.wav` (mono 44.1 kHz). Sourcing per-game announcer/cue templates. Aliases resolve to canonical game dir; undeclared sound names save with a warning. |
| `batch_clip_highlights(game, limit)` | Organize a game's short Outplayed clips into one folder (no LLM, $0) |
| `analyze_asset(asset_id, cut=False)` | One-shot: candidates → rank (→ cut if `cut=True`) |
| `get_job(job_id)` | Poll any job |

Job-based tools poll internally, so one tool call = one finished step —
Claude doesn't have to manage polling.

## Setup

1. **Clone both repos** as siblings under a shared parent folder.
   Then `uv sync` each:

   ```
   ~/source/repos/ai-video-editor/
   ├── api/   (this repo — the backend; GH: ai-video-editor-api)
   └── mcp/   (sibling — the MCP server; GH: ai-video-editor-mcp)
   ```

2. Start the API (separate terminal, keep running):

   ```bash
   uv run dev          # in ai-video-editor/api/
   ```

3. The backend ships a `.mcp.json` at its root pointing at the sibling
   `../mcp/`. Claude Code auto-discovers it when you open this project
   (approve it when prompted). To register manually instead:

   ```bash
   claude mcp add ai-video-editor -- uv run --directory \
     C:\Users\taile\source\repos\ai-video-editor\mcp ai-video-editor-mcp
   ```

   For Claude Desktop, add the same `mcpServers` block from `.mcp.json`
   to its config file.

3. Ask Claude naturally:

   > "Scan my recordings, then analyze the most recent League game and
   > show me the best moments."

   Claude will call `scan_assets` → `list_assets(game="League")` →
   `analyze_asset(id)` and summarize the ranked highlights.

## Notes

- No secrets in `.mcp.json` — keys stay in `.env`, read by the API
  process only. The MCP server just needs `AI_VIDEO_EDITOR_URL`.
- If tools error with connection refused, the API server (`uv run dev`)
  isn't running.
- Long jobs (audio on a full recording) can take a minute; the tool
  waits up to 5 minutes before returning a timeout status.
