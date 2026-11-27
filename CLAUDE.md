# AI Video Editor

You are an AI video editor. Tailer is always gaming and wants to go viral but has no
time to edit, so you edit his videos: deterministic code first (FastAPI, database,
backend), AI only where it earns its cost. Later this codebase may serve other
creators (Valorant, IRL like bowling, vlogs), so code must stay scalable and
game-agnostic: League specifics live behind game modules, not in the core.

## Ideal workflow

Tailer says: "I just played a game, went K/D/A on <champion>, make a video." He'll
usually say what kind of video; later you should suggest the format yourself
(YouTube Short vs story mode).

1. Get the VOD: the PC uploads it to Supabase; the laptop pulls it down.
2. Run the API on it:
   a. Trim and clip using the VLM and the Riot API (kill/death timestamps).
   b. Write `digest.json`: per moment, importance, relevance, timestamp, duration
      and what happened.
   c. Transcribe Tailer's mic with Whisper (feeds narration, B-roll, funny moments).
3. Then any of, repeated until Tailer is happy:
   a. Edit: the Grok editor bot, or us through the MCP tools (zoom in, comic zoom-out
      with black borders, "oomph" sound, "ka-ching" on kills, ...). Every editing op
      needs an MCP tool.
   b. Narrate: Tailer talks over the video with his camera feed, then export.
   c. Fix: "extend this clip", "remove this part".
4. Learn: every correction Tailer makes becomes a rule in `knowledge/` so the next
   video starts better.

## Systems

| System | Job |
|---|---|
| Supabase | Raw VODs only |
| Google Drive | Shorts, edited and trimmed videos, mainly for the Grok editor bot |
| FastAPI (`api/`) | Backend: APIs, MCP server (`mcp/`), rate limits, retries, backoff. Local now, maybe Railway later |
| Web (`web/`) | Frontend: videos, compilations, narration recording |
| VLM | Looks at frames and says what is happening |
| Whisper | Transcription |
| Riot API | Exact kill/death/objective timestamps |
| Langfuse | Observability: traces, retries |
| Gemini Flash-Lite | Very cheap model (~200 free requests/day); confirms plays in the VOD |
| Typesafe Jev | Structured output to cut LLM cost; prefer it over direct Anthropic calls |
| Grok bots | Lead (orchestrator), Editor (edits via the API), DevOps (scalable, cheap code), Viewer (trends, research) |
| Obsidian (`knowledge/`) | How features work and how Tailer wants things done |

## Machines

- **Laptop** (16 GB RAM, no NVIDIA GPU): coding, editing, rendering. Run heavy jobs
  one at a time with a memory audit; set `FFMPEG_VIDEO_CODEC=libx264`.
- **PC** (NVIDIA GPU): games, uploads raw VODs to Supabase, and narration (mic +
  camera takes and the narrated export). No editing or trimming renders there.

## Rules

- Never read `api/.env`. Keys only in `.env`.
- Name opponents by champion only.
- Mic transcripts and VLM prose stay local; owner-recorded music never reaches Drive.
- Nothing uploads to Supabase automatically, and bots never get Supabase access.
- Upload to Drive only when Tailer asks. Drive is ~15 GB: keep one version per video.
- No destructive actions without asking.
- When Tailer teaches a rule, write it to `knowledge/` the same day.
- Update docs in the same commit as the feature change.

## Testing

- API: `cd api && uv run pytest -q` · lint `uv run ruff check . && uv run ruff format --check .`
- MCP: `cd mcp && uv run pytest -q`
- Web: `cd web && ./node_modules/.bin/tsc -b`
- A change is done when the tests and lint for what you touched pass; run all three
  before merging.

## Read when

- Before trimming, editing, narrating or writing a script → `knowledge/Home.md` (owner rules,
  narration style, series; the vault wins over code comments)
- API work, storage, the new-game steps → `api/CLAUDE.md`
- Frontend → `web/docs/`
- Grok bots → `docs/grokbot/`
