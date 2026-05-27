# ai-video-editor-api

> Part of the **ai-video-editor** project. Sibling repos live under the
> same parent folder:
>
> ```
> ai-video-editor/
> ├── api/    ← this repo (FastAPI backend)
> ├── mcp/    ← MCP server (ai-video-editor-mcp on GH)
> └── (future) web/, utils/
> ```

A **local-first, deterministic** video automation API that turns raw
Outplayed gameplay recordings into ranked highlight suggestions and
polished, iteratively-editable compilation reels — without uploading
source footage anywhere.

FastAPI backend + SQLite + ffmpeg. Source videos are **never modified**;
every output goes to a separate workspace directory. The LLM never
watches video — it ranks structured candidate metadata. This makes the
whole pipeline ~100x cheaper than a vision-based approach and fully
auditable.

## At a glance

```
recordings ─> candidate generators (cheap, deterministic, no LLM)
                ├─ outplayed_clip   (Outplayed already cut it)
                ├─ riot_api         (exact kill/death timestamps)
                ├─ audio_peak       (loud regions in full VODs)
                └─ transcript_keyword (local Whisper cues)
                         │
                         ▼
              HighlightCandidate rows (one table, source-tagged)
                         │
                         ▼
              LLM ranker (one Claude call per video, ~$0.005)
                         │
                         ▼
              ranked suggestions ─> highlights folder
                                 ─> compilation reel (spec-driven,
                                                       iteratively
                                                       editable)
```

## Architecture

Layers (Single Responsibility — each depends only on the one below):

```
HTTP            routers/{assets,clips,projects,jobs,analyze,edits}.py
                  ↓  (validation, job lifecycle)
Orchestration   candidates/service.py · ranker.py · highlights.py
                · compile.py (spec mutators + render)
                  ↓
I/O shells      candidates/{audio,riot,transcript} · edits.py
                · editing.trim_clip (ffmpeg / numpy / Riot HTTP)
                  ↓
Persistence     database.py (aiosqlite, raw SQL, Postgres-portable)
Cross-cutting   config.py (pydantic-settings) · tracing.py (Langfuse)
```

Long-running work (ffmpeg, audio decode, LLM) runs in
`asyncio.to_thread` background tasks tracked in the `jobs` table — the
client polls; nothing blocks a request.

See [`docs/architecture.md`](docs/architecture.md) for the full
breakdown, including the data flow and the open/closed extension points
for adding candidate sources, clip-windowing strategies, and effects.

## How the MCP works

The MCP server lives in its own sibling repo at `../mcp/`
(GH: `ai-video-editor-mcp`). It's a **thin stdio
adapter over the HTTP API** — no logic, no secrets, no DB. It only
needs `AI_VIDEO_EDITOR_URL`.

```
Claude Code / Desktop
   │  MCP stdio
   ▼
ai-video-editor-mcp   (separate repo — FastMCP, mcp + httpx only)
   │  HTTP localhost:8000
   ▼
FastAPI app (this repo — uv run dev)   ← must be running
```

**Why a separate repo:** decoupled deps (the MCP doesn't pull in
FastAPI/sqlite/anthropic/Whisper/etc.), independent PRs, OSS-friendly.
Both repos sit as siblings under one parent dir.

Job-based MCP tools **poll internally**, so one tool call = one
finished step. The full tool list is in
[`docs/mcp.md`](docs/mcp.md); for **editing** specifically see
[`docs/editing-tools.md`](docs/editing-tools.md).

Setup: clone both repos as siblings, `uv sync` in each, then either
rely on this repo's `.mcp.json` (auto-discovered by Claude Code) or
`claude mcp add` manually. Talk to Claude naturally:

> "Scan my recordings, analyze the most recent League game, then
> compile the top 10 hype clips."

Claude picks the tools in order; you watch.

## Setup

Prerequisites: **uv** (Python package manager), **ffmpeg + ffprobe**
on PATH (`winget install Gyan.FFmpeg`), and an **Anthropic API key**
for the rank step.

```bash
uv sync                  # install deps
cp .env.example .env     # then edit OUTPLAYED_MEDIA_DIR / WORKSPACE_DIR / keys
uv run dev               # serve :8000
uv run pytest -q         # ~50 tests, no real ffmpeg/Anthropic
uv run ruff check src/   # lint
```

Swagger UI at `http://localhost:8000/docs`. Full env-var reference and
troubleshooting in [`docs/setup.md`](docs/setup.md).

In another terminal, **clone the sibling MCP repo** as `../mcp/`:

```bash
git clone https://github.com/tailerchimchar/ai-video-editor-mcp ../mcp
cd ../mcp && uv sync && cd -
claude mcp add ai-video-editor -- uv run --directory \
  C:\Users\taile\source\repos\ai-video-editor\mcp ai-video-editor-mcp
```

## LLMs used

| Step | Where | Model | Cost |
|---|---|---|---|
| **Rank candidates** | `ranker.py` (`messages.parse`) | `claude-haiku-4-5` (default; override via `RANKER_MODEL`) | ~$0.005–0.026 per video (typical) |
| Everything else | local | — | $0 |

Ranking is the **only** paid Anthropic call. It's gated by
`ANTHROPIC_MAX_RANK_CALLS` (default 25/process — refuses further calls
with a clear error and no HTTP request when capped). The system prompt
is frozen with `cache_control: ephemeral`; volatile candidate JSON is
in the user turn. Token/cost are captured to Langfuse via manual
`update_current_generation(model=, usage_details=)` since
`AnthropicInstrumentor` doesn't cover `messages.parse`. Full breakdown
in [`docs/cost-and-tracing.md`](docs/cost-and-tracing.md).

Local models used (no network, no cost):

- **Whisper** (`faster-whisper`, CPU int8) for transcripts. Default
  `base`; bumps to `small` / `medium` are config-only.
- **FastEmbed** (`BAAI/bge-small-en-v1.5`, 384-dim) for transcript
  chunk embeddings, stored in **sqlite-vec** for semantic
  `search_clips`.

## What's built today

- **Phase 1** — scan → trim clips → projects/timeline → render rough cut.
- **Phase 2** — candidate-first analyzer (`outplayed_clip`,
  `audio_peak`, `riot_api`, `transcript_keyword`) + Haiku ranker +
  organized highlights folder (`highlights/<game>/<date>_<champ>/`).
- **Phase 2.5** — local Whisper transcripts + RAG semantic search.
- **Phase 3A** — per-clip editing primitives: `zoom`, `caption`,
  `focus`. Configurable 16:9 / 9:16 aspect.
- **Phase 3B** — `/edit/compile`: per-clip ffmpeg + 0.3s fades + concat
  + optional music mix into one reel.
- **Phase 3B+** — iterative editing: a compilation's `spec.json` is the
  mutable source of truth. Add/remove effects, extend windows, drop
  clips, toggle clip-number labels — each mutation re-renders only the
  affected clip and re-concats (sub-5s).

## Roadmap

The living backlog is in [`docs/roadmap.md`](docs/roadmap.md). Each
item there has **open questions** to discuss *before* writing code.

Active sprint (locked order — design questions discussed before code):

1. **Clustering** — merge ranked candidates within ~30 s into one long
   fight clip before compile (eliminates teamfight duplication).
2. **Game profile system** — TOML per game (regions + sound names +
   event vocabulary) under `profiles/<game>.toml`; user-supplied audio
   under `WORKSPACE/media_library/<game>/sfx/`.
3. **`zoom_region` / `add_sfx` / `add_card` primitives + per-game MCP
   wrappers** — auto-generated `league_*` / `valorant_*` tools from the
   profile (e.g. `league_zoom_minimap`, `valorant_play_headshot`).
4. **Audio event detection** — new `audio_event` candidate source via
   mel cross-correlation against the profile's reference templates.
   Same detector feeds both the ranker and auto-sfx-overlay.
5. **CV killfeed detection** — frame-based kill detection (template
   match vs Data Dragon portraits / agent icons). Sidesteps the Riot
   offset class and gives Valorant a real kill timeline.
6. **LLM compilation planner** — pre-compile spec generation: LLM
   picks effects from the profile's bounded menu, outputs a full
   `spec.json` the existing renderer consumes.

Each has open design questions in [`docs/roadmap.md`](docs/roadmap.md)
we settle before writing code.

## Project conventions

- **Single Responsibility per module.** HTTP (routers) ≠ orchestration
  (`compile.py`, `candidates/service.py`) ≠ I/O (`edits.py`,
  `candidates/*`). Don't put ffmpeg or HTTP calls in routers.
- **Open/closed** for candidate sources (add a `candidates/<name>.py`),
  clip-windowing (add a `_window_*` strategy), and effects (extend the
  spec mutator list).
- **Source files are immutable.** Only read/probe inputs; all writes
  go to `WORKSPACE_DIR`.
- **No `shell=True`, no user-supplied raw args.** ffmpeg commands are
  built from validated Pydantic inputs only.
- **UUID4 ids, ISO-8601 UTC timestamps, everywhere.**
- Secrets live in `.env` (gitignored) — never commit keys.

## Where to read next

| Doc | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Layered design, data flow, extension points |
| [docs/codebase-tour.md](docs/codebase-tour.md) | Plain-language tour of every file |
| [docs/api.md](docs/api.md) | Full HTTP endpoint reference |
| [docs/candidate-sources.md](docs/candidate-sources.md) | How each candidate source works |
| [docs/mcp.md](docs/mcp.md) | MCP server — tool catalogue + setup |
| [docs/editing-tools.md](docs/editing-tools.md) | Editing tools catalogue + per-game profiles |
| [docs/cost-and-tracing.md](docs/cost-and-tracing.md) | Anthropic spend, guards, Langfuse |
| [docs/setup.md](docs/setup.md) | Install, env vars, troubleshooting |
| [docs/roadmap.md](docs/roadmap.md) | Backlog + open design questions |
| [CLAUDE.md](CLAUDE.md) | High-level picture for Claude / contributors |
