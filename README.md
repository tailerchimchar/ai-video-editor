# AI Video Editor

Local-first AI video editor that turns gameplay recordings (League of
Legends, Valorant, Twitch VODs) into ranked highlight reels — without
uploading source footage anywhere.

**Status:** alpha / personal project. Solo dev. Things will move.

## How it works

```
recording.mp4 ──> candidate generators (cheap, deterministic, no LLM)
                   ├─ audio_peak       (loud regions)
                   ├─ transcript_keyword (Whisper STT + hype-word match)
                   ├─ riot_api         (League: exact kill timestamps)
                   └─ outplayed_clip   (Outplayed already cut it)
                          │
                          ▼
              HighlightCandidate rows  (one table, source-tagged)
                          │
                          ▼
              LLM ranker  (one Claude call/video; ~$0.005)
                          │
                          ▼
              kept + (hype | funny | story) scores + reason
                          │
                          ▼
              ffmpeg compile  (per-clip render + captions + intro + reel)
```

The LLM never watches video — it ranks structured metadata. ~100× cheaper
than vision-based pipelines and fully traceable.

## Repository layout

Monorepo with three components:

| Dir | What | Stack |
|---|---|---|
| [`api/`](./api) | FastAPI backend: pipeline, ranker, ffmpeg, jobs DB, intro/feedback systems | Python 3.10 · FastAPI · faster-whisper · sqlite · pydantic |
| [`mcp/`](./mcp) | FastMCP server: exposes every editing op as a tool so the pipeline is drivable conversationally from Claude | Python 3.10 · FastMCP · stdio |
| [`web/`](./web) | Compilation viewer + editor (filmstrip, captions, zoom/focus, history rail) | TypeScript · React 19 · Vite · Tailwind · TanStack Query |

Each has its own README with setup instructions.

## Quick start

Prereqs: Python 3.10+, [uv](https://github.com/astral-sh/uv), Node 20+ /
[bun](https://bun.sh), and ffmpeg on PATH.

```bash
# 1. Backend
cd api && uv sync && cp .env.example .env  # edit .env with your keys
uv run dev                                   # http://localhost:8000

# 2. Webapp (new terminal)
cd web && bun install && bun run dev         # http://localhost:5173

# 3. MCP (optional — drives the pipeline from Claude)
cd mcp && uv sync
# point Claude Desktop / claude-code at .mcp.json
```

Drop gameplay `.mp4` files in your configured `OUTPLAYED_MEDIA_DIR`, then
hit `POST /api/v1/assets/scan` to index them. From there: candidates →
rank → cut → compile. The webapp lets you review + edit the output.

## What's distinctive

- **Local-first.** Source video never leaves your machine. The only
  network call is one Anthropic API request per recording for ranking.
- **Candidate-first architecture.** Hundreds of cheap deterministic
  signals → one tiny LLM call to reduce + rank. Vision-free.
- **Per-event clip windows.** A kill milks the celebration; a teamfight
  needs buildup time; a funny moment gets balanced pre/post. Configured
  per event type, applied to both the highlight-cuts pipeline and the
  compile pipeline.
- **Narrative compile mode.** Three sections (intro / main / outro) in
  recording order — built for long Twitch VODs that benefit from a story
  arc, not just hype-density.
- **Editor that's a thin shortcut over the API.** Every webapp action
  hits the same HTTP surface the MCP uses. No client-side editing logic.
- **User-edit feedback loop.** Every extend / remove / revert is logged
  with the affected clip's event_type. Future jobs aggregate per-event
  medians to auto-tune the default windows from your actual usage.

## License

[MIT](./LICENSE) — do whatever, just keep the notice.

## Acknowledgments

Built with Claude (Anthropic). Game profiles for League / Valorant.
Riot API for ground-truth kill timestamps where available.
